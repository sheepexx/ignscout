"""Keep the computer from falling asleep while a scan runs.

Nothing is changed permanently: on Windows the request is tied to this process
(``SetThreadExecutionState``) and on macOS to a ``caffeinate`` child process, so it
ends automatically when the scan stops, even after a crash. The screen may still
turn off; only *system* sleep is prevented. Closing a laptop lid can still sleep it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator

logger = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080
_STD_INPUT_HANDLE = -10


@contextlib.contextmanager
def no_quick_edit() -> Iterator[bool]:
    """Stop a mouse click from freezing the program in the classic Windows console.

    In that window a click starts "QuickEdit" selection, which pauses all output; a long
    scan then looks frozen and stalls as soon as it needs to print. Selection is turned off
    while the scan runs and restored afterwards. Yields whether the setting was changed.
    """
    if sys.platform != "win32":
        yield False
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    mode = wintypes.DWORD()
    if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        yield False  # not attached to a console (pipe, IDE, tests)
        return
    original = mode.value
    changed = bool(original & _ENABLE_QUICK_EDIT_MODE) and bool(
        kernel32.SetConsoleMode(handle, (original | _ENABLE_EXTENDED_FLAGS) & ~_ENABLE_QUICK_EDIT_MODE)
    )
    try:
        yield changed
    finally:
        if changed:
            kernel32.SetConsoleMode(handle, original)


def supported() -> bool:
    return sys.platform == "win32" or (sys.platform == "darwin" and shutil.which("caffeinate") is not None)


@contextlib.contextmanager
def keep_awake(enabled: bool = True) -> Iterator[bool]:
    """Context manager; yields whether sleep prevention is actually active."""
    if not enabled or not supported():
        yield False
        return
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        active = bool(kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED))
        logger.info("keep-awake %s", "enabled" if active else "unavailable")
        try:
            yield active
        finally:
            kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        return
    # macOS: caffeinate exits by itself when this process exits (-w PID).
    process = subprocess.Popen(
        ["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        yield True
    finally:
        process.terminate()
