import importlib
import importlib.util
import types
import unittest
from unittest.mock import Mock, patch


MEMORY_SPEC = importlib.util.find_spec("memory")
memory = importlib.import_module("memory") if MEMORY_SPEC is not None else None


class MemoryReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(memory, "memory.py must provide release_process_memory()")
        self.original_last_trim = memory._memory_trim_runtime.get("last_trim_ts", 0.0)

    def tearDown(self) -> None:
        if memory is not None:
            memory._memory_trim_runtime["last_trim_ts"] = self.original_last_trim

    def test_force_release_runs_gc_before_malloc_trim(self) -> None:
        calls = []
        fake_libc = types.SimpleNamespace(malloc_trim=Mock(side_effect=lambda _value: calls.append("trim")))
        memory._memory_trim_runtime["last_trim_ts"] = 100.0

        with patch.object(memory, "MEMORY_TRIM_ENABLED", True), patch.object(
            memory, "MEMORY_TRIM_MIN_INTERVAL_SECONDS", 60
        ), patch("memory.time.monotonic", return_value=120.0), patch(
            "memory.gc.collect", side_effect=lambda: calls.append("gc")
        ) as gc_collect, patch.object(
            memory.os, "name", "posix"
        ), patch.object(
            memory.os, "uname", return_value=types.SimpleNamespace(sysname="Linux")
        ), patch(
            "memory.ctypes.CDLL", return_value=fake_libc
        ):
            released = memory.release_process_memory("tree-sync", force=True)

        self.assertTrue(released)
        gc_collect.assert_called_once_with()
        self.assertEqual(calls, ["gc", "trim"])
        fake_libc.malloc_trim.assert_called_once_with(0)

    def test_non_linux_still_collects_python_objects(self) -> None:
        with patch.object(memory, "MEMORY_TRIM_ENABLED", True), patch(
            "memory.gc.collect"
        ) as gc_collect, patch.object(
            memory.os, "name", "posix"
        ), patch.object(
            memory.os, "uname", return_value=types.SimpleNamespace(sysname="Darwin")
        ), patch(
            "memory.ctypes.CDLL"
        ) as cdll:
            released = memory.release_process_memory("tree-sync", force=True)

        self.assertTrue(released)
        gc_collect.assert_called_once_with()
        cdll.assert_not_called()

    def test_disabled_release_does_nothing(self) -> None:
        with patch.object(memory, "MEMORY_TRIM_ENABLED", False), patch(
            "memory.gc.collect"
        ) as gc_collect:
            released = memory.release_process_memory("tree-sync", force=True)

        self.assertFalse(released)
        gc_collect.assert_not_called()

    def test_missing_glibc_is_a_safe_fallback(self) -> None:
        with patch.object(memory, "MEMORY_TRIM_ENABLED", True), patch(
            "memory.gc.collect"
        ) as gc_collect, patch.object(
            memory.os, "name", "posix"
        ), patch.object(
            memory.os, "uname", return_value=types.SimpleNamespace(sysname="Linux")
        ), patch(
            "memory.ctypes.CDLL", side_effect=OSError("libc unavailable")
        ):
            released = memory.release_process_memory("tree-sync", force=True)

        self.assertTrue(released)
        gc_collect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
