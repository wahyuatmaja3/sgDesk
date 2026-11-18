"""Windows UAC elevation helpers for sgDesk."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def wait_for_pid_exit(pid: int, timeout: float = 6.0) -> None:
    """Wait briefly for a previous sgDesk instance to exit and release its port."""
    if pid <= 0:
        return
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def request_self_elevation(extra_args: Sequence[str] = ()) -> bool:
    """Spawn an elevated copy of sgDesk via UAC 'runas' and return True if accepted."""
    if is_admin():
        return True

    args = [a for a in extra_args if not a.startswith("--wait-pid")]
    args.extend(["--wait-pid", str(os.getpid())])

    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = subprocess.list2cmdline(args)
    else:
        exe = sys.executable
        script = str(Path(__file__).resolve().parent.parent / "sgdesk.py")
        params = subprocess.list2cmdline([script, *args])

    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return rc > 32
