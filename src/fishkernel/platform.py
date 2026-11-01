"""Operating-system boundary. Runtime support currently targets Linux only."""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path


class UnsupportedPlatformError(RuntimeError):
    pass


def require_runtime() -> None:
    if not sys.platform.startswith("linux"):
        raise UnsupportedPlatformError("fishKernel runtime currently supports Linux only")


def lock_file(handle, *, unlock: bool = False) -> None:
    require_runtime()
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def process_identity(pid: int | None, *, include_zombie: bool = False) -> str | None:
    """Linux boot ID and process start ticks; never probe a PID using a signal."""
    require_runtime()
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z" and not include_zombie:
            return None
        return f"{boot}:{pid}:{fields[19]}"
    except (OSError, IndexError):
        return None


def process_alive(pid: int | None, identity: str | None = None) -> bool:
    current = process_identity(pid)
    return current is not None and (identity is None or current == identity)


def terminate_group(pid: int, identity: str | None) -> bool:
    require_runtime()
    if not identity or process_identity(pid, include_zombie=True) != identity:
        return False
    try:
        if pid == os.getpid() or os.getpgid(pid) != pid:
            return False
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True
