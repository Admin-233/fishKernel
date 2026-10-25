"""交班式重启：旧进程 spawn 新进程，释放锁，然后自己正常退出。

全程不给自身发任何信号。新进程启动后会阻塞在锁上，等旧进程释放后接管。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Sequence

from .archive import Archive


def successor_argv(archive: Archive, *, python: str | None = None, extra_args: Sequence[str] = ()) -> list[str]:
    """新内核的启动命令（便于测试，不真正启动）。"""
    return [python or sys.executable, "-m", "fishkernel", "run", str(archive.root), *extra_args]


def spawn_successor(archive: Archive, *, python: str | None = None, extra_args: Sequence[str] = ()) -> int:
    """启动接班进程，返回它的 pid。stdout/stderr 丢弃（内核自己写 kernel.log）。"""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(archive.root),
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    process = subprocess.Popen(successor_argv(archive, python=python, extra_args=extra_args), **kwargs)
    return process.pid
