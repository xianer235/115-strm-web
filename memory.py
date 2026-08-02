import ctypes
import gc
import logging
import os
import time
from typing import Dict


MEMORY_TRIM_MIN_INTERVAL_SECONDS = max(
    0,
    int(os.environ.get("MEMORY_TRIM_MIN_INTERVAL_SECONDS", "60") or 60),
)
MEMORY_TRIM_ENABLED = str(os.environ.get("MEMORY_TRIM_ENABLED", "1") or "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
    "disabled",
}
_memory_trim_runtime: Dict[str, float] = {"last_trim_ts": 0.0}


def release_process_memory(reason: str = "", force: bool = False) -> bool:
    """Collect unreachable objects and return free glibc pages when supported."""
    if not MEMORY_TRIM_ENABLED:
        return False

    now_ts = time.monotonic()
    last_trim_ts = float(_memory_trim_runtime.get("last_trim_ts", 0.0) or 0.0)
    if (not force) and MEMORY_TRIM_MIN_INTERVAL_SECONDS > 0 and now_ts - last_trim_ts < MEMORY_TRIM_MIN_INTERVAL_SECONDS:
        return False

    _memory_trim_runtime["last_trim_ts"] = now_ts
    gc.collect()
    if os.name != "posix" or not str(os.uname().sysname).lower().startswith("linux"):
        return True

    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except Exception as exc:
        logging.debug("Process memory trim skipped after %s: %s", reason or "job", exc)
    return True
