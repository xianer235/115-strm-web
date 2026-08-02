import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main


REQUIRED_TREE_HELPERS = (
    "TREE_SYNC_PATH_BATCH_SIZE",
    "TREE_SYNC_SQLITE_SELECT_CHUNK_SIZE",
    "_mark_local_files_seen_batch",
    "_replay_tree_cache",
    "_stream_tree_matches_to_cache",
)


class TreeStreamingSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        missing = [name for name in REQUIRED_TREE_HELPERS if not hasattr(main, name)]
        self.assertEqual(missing, [], f"missing streaming tree helpers: {missing}")
        main.task_status["running"] = False

    def tearDown(self) -> None:
        main.task_status["running"] = False

    def test_ensure_db_migrates_existing_local_files_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "data.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE local_files (path_hash TEXT PRIMARY KEY, relative_path TEXT)")
            conn.execute("INSERT INTO local_files VALUES ('old-hash', 'Old/Show.mkv')")
            conn.commit()
            conn.close()

            with patch.object(main, "DB_PATH", db_path):
                main.ensure_db()

            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(local_files)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(local_files)")}
                rows = conn.execute(
                    "SELECT path_hash, relative_path, scan_token FROM local_files"
                ).fetchall()
            finally:
                conn.close()

            self.assertIn("scan_token", columns)
            self.assertIn("idx_local_files_scan_token", indexes)
            self.assertEqual(rows, [("old-hash", "Old/Show.mkv", "")])

    def test_stream_tree_matches_to_atomic_cache_and_replays(self) -> None:
        tree_text = "\n".join(
            [
                "根目录",
                "| 电视剧",
                "| | Test.Show.S01E01.mkv",
                "| | Test.Show.S01E02.mkv",
                "| | README.txt",
            ]
        )
        matched_paths = []

        with tempfile.TemporaryDirectory() as tmpdir:
            tree_path = os.path.join(tmpdir, "tree.txt")
            cache_path = os.path.join(tmpdir, "cache.txt")
            with open(tree_path, "w", encoding="utf-8") as stream:
                stream.write(tree_text)

            matched_count, lines_total, nodes_total = main._stream_tree_matches_to_cache(
                cache_path,
                tree_path,
                {"mkv"},
                "TV",
                1,
                matched_paths.append,
            )
            replayed_paths = []
            replayed_count = main._replay_tree_cache(cache_path, replayed_paths.append)

            self.assertFalse(os.path.exists(cache_path + ".tmp"))
            with open(cache_path, "r", encoding="utf-8") as stream:
                cache_lines = stream.read().splitlines()

        expected = ["TV/电视剧/Test.Show.S01E01.mkv", "TV/电视剧/Test.Show.S01E02.mkv"]
        self.assertEqual((matched_count, lines_total, nodes_total), (2, 5, 5))
        self.assertEqual(matched_paths, expected)
        self.assertEqual(cache_lines, expected)
        self.assertEqual(replayed_count, 2)
        self.assertEqual(replayed_paths, expected)

    def test_failed_stream_preserves_previous_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tree_path = os.path.join(tmpdir, "tree.txt")
            cache_path = os.path.join(tmpdir, "cache.txt")
            with open(tree_path, "w", encoding="utf-8") as stream:
                stream.write("根目录\n| New.Show.S01E01.mkv\n")
            with open(cache_path, "w", encoding="utf-8") as stream:
                stream.write("Old/Show.mkv\n")

            def fail_on_match(_rel_path: str) -> None:
                raise RuntimeError("database write failed")

            with self.assertRaisesRegex(RuntimeError, "database write failed"):
                main._stream_tree_matches_to_cache(
                    cache_path, tree_path, {"mkv"}, "", 0, fail_on_match
                )

            with open(cache_path, "r", encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "Old/Show.mkv\n")
            self.assertFalse(os.path.exists(cache_path + ".tmp"))

    def test_batch_deduplicates_within_and_across_batches(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE local_files (
                path_hash TEXT PRIMARY KEY,
                relative_path TEXT,
                scan_token TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cursor = conn.cursor()
        try:
            fresh, duplicates = main._mark_local_files_seen_batch(
                cursor,
                ["Show/E01.mkv", "Show/E01.mkv", "Show/E02.mkv"],
                "run-1",
            )
            self.assertEqual(fresh, ["Show/E01.mkv", "Show/E02.mkv"])
            self.assertEqual(duplicates, 1)

            fresh, duplicates = main._mark_local_files_seen_batch(
                cursor, ["Show/E01.mkv", "Show/E03.mkv"], "run-1"
            )
            self.assertEqual(fresh, ["Show/E03.mkv"])
            self.assertEqual(duplicates, 1)
        finally:
            conn.close()

    def test_batch_splits_large_selects_below_sqlite_parameter_limit(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE local_files (
                path_hash TEXT PRIMARY KEY,
                relative_path TEXT,
                scan_token TEXT NOT NULL DEFAULT ''
            )
            """
        )
        statements = []
        conn.set_trace_callback(statements.append)
        paths = [
            f"Show/Episode-{index:04d}.mkv"
            for index in range(main.TREE_SYNC_SQLITE_SELECT_CHUNK_SIZE + 5)
        ]
        try:
            fresh, duplicates = main._mark_local_files_seen_batch(
                conn.cursor(), paths, "run-1"
            )
        finally:
            conn.close()

        selects = [
            statement
            for statement in statements
            if statement.startswith("SELECT path_hash, relative_path, scan_token")
        ]
        self.assertEqual(fresh, paths)
        self.assertEqual(duplicates, 0)
        self.assertEqual(len(selects), 2)

    def _run_sync_fixture(self, sync_clean: bool) -> tuple:
        tmpdir = tempfile.TemporaryDirectory()
        root = tmpdir.name
        tree_dir = os.path.join(root, "trees")
        strm_root = os.path.join(root, "strm")
        log_dir = os.path.join(root, "logs")
        db_path = os.path.join(root, "data.db")
        os.makedirs(tree_dir)
        os.makedirs(strm_root)
        tree_path = os.path.join(tree_dir, "tree_0.txt")
        with open(tree_path, "w", encoding="utf-8") as stream:
            stream.write("根目录\n| 剧集\n| | New.Show.S01E01.mkv\n| | New.Show.S01E02.mkv\n")

        cfg = {
            **main.default_config(),
            "alist_url": "http://alist.example",
            "mount_path": "/115",
            "trees": [{"url": "local-tree", "prefix": "Library", "exclude": 1}],
            "sync_mode": "incremental",
            "sync_clean": sync_clean,
            "check_hash": False,
        }

        patches = patch.multiple(
            main,
            DB_PATH=db_path,
            TREE_DIR=tree_dir,
            STRM_ROOT=strm_root,
            LOG_DIR=log_dir,
            MAIN_LOG_PATH=os.path.join(log_dir, "task.log"),
            get_config=Mock(return_value=cfg),
            save_config=Mock(),
            write_log=AsyncMock(),
            update_progress=AsyncMock(),
            schedule_ui_state_push=Mock(),
        )
        return tmpdir, db_path, strm_root, cfg, patches

    def test_run_sync_marks_scan_token_and_cleans_stale_files(self) -> None:
        tmpdir, db_path, strm_root, _cfg, patches = self._run_sync_fixture(True)
        try:
            with patch.object(main, "DB_PATH", db_path):
                main.ensure_db()
            stale_rel = "Old/Old.Show.S01E01.mkv"
            stale_target = os.path.join(strm_root, stale_rel + ".strm")
            os.makedirs(os.path.dirname(stale_target), exist_ok=True)
            with open(stale_target, "w", encoding="utf-8") as stream:
                stream.write("stale")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO local_files(path_hash, relative_path, scan_token) VALUES (?, ?, ?)",
                (hashlib.md5(stale_rel.encode()).hexdigest(), stale_rel, "old-run"),
            )
            conn.commit()
            conn.close()

            with patches, patch.object(main, "release_process_memory", Mock()):
                asyncio.run(main.run_sync(use_local=True))

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT relative_path, scan_token FROM local_files ORDER BY relative_path"
                ).fetchall()
            finally:
                conn.close()

            self.assertFalse(os.path.exists(stale_target))
            self.assertEqual(
                [row[0] for row in rows],
                ["Library/剧集/New.Show.S01E01.mkv", "Library/剧集/New.Show.S01E02.mkv"],
            )
            self.assertEqual(len({row[1] for row in rows}), 1)
        finally:
            tmpdir.cleanup()

    def test_sync_clean_false_keeps_stale_file_but_removes_stale_index(self) -> None:
        tmpdir, db_path, strm_root, _cfg, patches = self._run_sync_fixture(False)
        try:
            with patch.object(main, "DB_PATH", db_path):
                main.ensure_db()
            stale_rel = "Old/Keep.Me.mkv"
            stale_target = os.path.join(strm_root, stale_rel + ".strm")
            os.makedirs(os.path.dirname(stale_target), exist_ok=True)
            with open(stale_target, "w", encoding="utf-8") as stream:
                stream.write("stale")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO local_files(path_hash, relative_path, scan_token) VALUES (?, ?, ?)",
                (hashlib.md5(stale_rel.encode()).hexdigest(), stale_rel, "old-run"),
            )
            conn.commit()
            conn.close()

            with patches, patch.object(main, "release_process_memory", Mock()):
                asyncio.run(main.run_sync(use_local=True))

            conn = sqlite3.connect(db_path)
            try:
                stale_count = conn.execute(
                    "SELECT COUNT(*) FROM local_files WHERE relative_path = ?", (stale_rel,)
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertTrue(os.path.exists(stale_target))
            self.assertEqual(stale_count, 0)
        finally:
            tmpdir.cleanup()

    def test_old_json_cache_is_reparsed_and_removed(self) -> None:
        tmpdir, db_path, _strm_root, cfg, patches = self._run_sync_fixture(True)
        try:
            tree = cfg["trees"][0]
            tree_key = main.build_tree_cache_key(tree)
            legacy_path = os.path.join(tmpdir.name, "trees", f"cache_{tree_key}.json")
            with open(legacy_path, "w", encoding="utf-8") as stream:
                json.dump(["Legacy/Old.mkv"], stream)

            with patches, patch.object(main, "release_process_memory", Mock()):
                asyncio.run(main.run_sync(use_local=True))

            cache_path = os.path.join(tmpdir.name, "trees", f"cache_{tree_key}.txt")
            self.assertTrue(os.path.exists(cache_path))
            self.assertFalse(os.path.exists(legacy_path))
        finally:
            tmpdir.cleanup()

    def test_failed_scan_rolls_back_tokens_and_skips_stale_cleanup(self) -> None:
        tmpdir, db_path, strm_root, _cfg, patches = self._run_sync_fixture(True)
        try:
            with patch.object(main, "DB_PATH", db_path):
                main.ensure_db()
            stale_rel = "Old/Must.Stay.mkv"
            stale_target = os.path.join(strm_root, stale_rel + ".strm")
            os.makedirs(os.path.dirname(stale_target), exist_ok=True)
            with open(stale_target, "w", encoding="utf-8") as stream:
                stream.write("stale")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO local_files(path_hash, relative_path, scan_token) VALUES (?, ?, ?)",
                (hashlib.md5(stale_rel.encode()).hexdigest(), stale_rel, "old-run"),
            )
            conn.commit()
            conn.close()

            def fail_after_full_batch(
                _cache_path, _tree_path, _extensions, _prefix, _exclude, on_match
            ):
                on_match("Library/New/E01.mkv")
                on_match("Library/New/E02.mkv")
                raise RuntimeError("synthetic parse failure")

            with patches, patch.object(main, "TREE_SYNC_PATH_BATCH_SIZE", 2), patch.object(
                main, "_stream_tree_matches_to_cache", side_effect=fail_after_full_batch
            ), patch.object(main, "release_process_memory", Mock()):
                asyncio.run(main.run_sync(use_local=True))

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT relative_path, scan_token FROM local_files ORDER BY relative_path"
                ).fetchall()
            finally:
                conn.close()

            self.assertTrue(os.path.exists(stale_target))
            self.assertEqual(rows, [(stale_rel, "old-run")])
        finally:
            tmpdir.cleanup()

    def test_md5_cache_replay_repairs_missing_strm(self) -> None:
        tmpdir, _db_path, strm_root, cfg, patches = self._run_sync_fixture(True)
        try:
            cfg["check_hash"] = True
            tree_text = "根目录\n| 剧集\n| | New.Show.S01E01.mkv\n| | New.Show.S01E02.mkv\n"
            raw_path = os.path.join(tmpdir.name, "trees", "tree_0.raw")
            with open(raw_path, "wb") as stream:
                stream.write(tree_text.encode("utf-16le"))

            with patches, patch.object(main, "release_process_memory", Mock()):
                asyncio.run(main.run_sync(use_local=True))
                missing_target = os.path.join(
                    strm_root, "Library/剧集/New.Show.S01E01.mkv.strm"
                )
                self.assertTrue(os.path.exists(missing_target))
                os.remove(missing_target)

                asyncio.run(main.run_sync(use_local=True))

            self.assertTrue(os.path.exists(missing_target))
            self.assertTrue(str(cfg.get("last_hash", "")).strip())
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
