"""单实例锁：同一个存档只允许一个内核进程。

POSIX 用 flock，Windows 用 msvcrt.locking。两者都由操作系统在进程退出/崩溃时
自动释放，因此不会留下"假占用"把存档永久锁死。
"""

from __future__ import annotations

import time
from pathlib import Path
from .platform import lock_file, require_runtime


class LockError(Exception):
    """锁已被另一个实例持有。"""


class InstanceLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, *, timeout: float = 0.0, poll: float = 0.1) -> None:
        require_runtime()
        if self.held:
            raise LockError("lock is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._lock(handle)
                self._handle = handle
                return
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise LockError(f"存档已被另一个内核实例占用：{self.path}") from None
                time.sleep(max(0.01, poll))

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            self._unlock(handle)
        except OSError:
            pass
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    # ---- 平台差异 ----
    def _lock(self, handle) -> None:
        lock_file(handle)

    def _unlock(self, handle) -> None:
        lock_file(handle, unlock=True)
