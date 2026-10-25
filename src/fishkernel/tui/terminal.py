"""跨平台终端后端：备用屏、尺寸、按键读取。

不依赖 curses / termios 的第三方包：POSIX 用 termios+select，
Windows 用 msvcrt + 开启 VT 转义支持。
"""

from __future__ import annotations

import os
import shutil
import sys
import time

_SEQUENCES = {
    "\x1b[A": "UP",
    "\x1b[B": "DOWN",
    "\x1b[C": "RIGHT",
    "\x1b[D": "LEFT",
    "\x1b[H": "HOME",
    "\x1b[F": "END",
    "\x1bOH": "HOME",
    "\x1bOF": "END",
    "\x1b[1~": "HOME",
    "\x1b[4~": "END",
    "\x1b[3~": "DELETE",
    "\x1b[5~": "PGUP",
    "\x1b[6~": "PGDN",
    "\x01": "CTRL_A",
    "\x05": "CTRL_E",
    "\x0b": "CTRL_K",
    "\x0c": "CTRL_L",
    "\x15": "CTRL_U",
    "\x17": "CTRL_W",
    "\x7f": "BACKSPACE",
    "\x08": "BACKSPACE",
    "\r": "ENTER",
    "\n": "ENTER",
    "\t": "TAB",
}

_WIN_CODES = {
    "H": "UP",
    "P": "DOWN",
    "K": "LEFT",
    "M": "RIGHT",
    "G": "HOME",
    "O": "END",
    "S": "DELETE",
    "I": "PGUP",
    "Q": "PGDN",
}


def is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class Terminal:
    def __init__(self, *, color: bool = True):
        self.color = color
        self.fd: int | None = None
        self._old_attrs = None
        self._pending = ""
        self._decoder = None

    # ---- 生命周期 ----
    def __enter__(self) -> "Terminal":
        self.enter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.exit()

    def enter(self) -> None:
        if os.name == "windows":
            self._enable_vt()
        else:
            import codecs
            import termios
            import tty

            self.fd = sys.stdin.fileno()
            self._old_attrs = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")

    def exit(self) -> None:
        try:
            self.write("\x1b[?25h\x1b[0m\x1b[?1049l")
        finally:
            if os.name != "windows" and self._old_attrs is not None and self.fd is not None:
                import termios

                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old_attrs)
                self._old_attrs = None

    # ---- 输出 ----
    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    @property
    def size(self) -> tuple[int, int]:
        fallback = (100, 30)
        try:
            size = shutil.get_terminal_size(fallback=fallback)
            return max(24, size.columns), max(6, size.lines)
        except (OSError, ValueError):
            return fallback

    # ---- 输入 ----
    def read_keys(self, timeout: float) -> list[str]:
        if os.name == "windows":
            return self._read_keys_windows(timeout)
        return self._read_keys_posix(timeout)

    def _read_keys_posix(self, timeout: float) -> list[str]:
        import os as _os
        import select

        if self.fd is None:
            return []
        try:
            readable, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            return []
        if not readable:
            return []
        try:
            data = _os.read(self.fd, 4096)
        except OSError:
            return []
        if not data:
            return ["EOF"]
        text = self._decoder.decode(data) if self._decoder is not None else data.decode("utf-8", "replace")
        self._pending += text
        return self._drain()

    def _drain(self) -> list[str]:
        keys: list[str] = []
        while self._pending:
            for sequence, name in _SEQUENCES.items():
                if self._pending.startswith(sequence):
                    self._pending = self._pending[len(sequence) :]
                    keys.append(name)
                    break
            else:
                char = self._pending[0]
                if char == "\x1b":
                    # 可能是没读完的转义序列，等下一次输入；超过 3 个字符就当单独的 ESC
                    if len(self._pending) < 3:
                        return keys
                    self._pending = self._pending[1:]
                    keys.append("ESC")
                elif ord(char) >= 32:
                    self._pending = self._pending[1:]
                    keys.append(char)
                else:
                    self._pending = self._pending[1:]
        return keys

    def _read_keys_windows(self, timeout: float) -> list[str]:
        import msvcrt

        deadline = time.monotonic() + max(0.0, timeout)
        keys: list[str] = []
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                keys.extend(self._read_one_windows())
                while msvcrt.kbhit():
                    keys.extend(self._read_one_windows())
                return keys
            time.sleep(0.01)
        return keys

    def _read_one_windows(self) -> list[str]:
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return [_WIN_CODES.get(code, "ESC")]
        if char == "\r":
            return ["ENTER"]
        if char == "\x08":
            return ["BACKSPACE"]
        if char == "\x1b":
            return ["ESC"]
        if ord(char) >= 32:
            return [char]
        return []

    @staticmethod
    def _enable_vt() -> None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:  # noqa: BLE001 - 老终端不支持就算了，退化成无色
            pass
