"""TUI 应用：跟随事件日志、状态条、输入框与按键循环。"""

from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

from ..archive import Archive, load_kernel_state
from ..config import load_config
from ..control import Control
from ..events import Event, read_events
from ..jobs import JobManager
from .editor import LineEditor
from .render import (
    STYLE_DIM,
    STYLE_HINT,
    Line,
    render_event,
    render_frame,
    sanitize,
    status_line,
)
from .terminal import Terminal, is_tty

MAX_FEED_LINES = 5000
MAX_EVENTS = 2000
STATUS_INTERVAL = 0.5
JOBS_INTERVAL = 1.0


class FeedBuffer:
    def __init__(self, max_lines: int = MAX_FEED_LINES):
        self.lines: list[Line] = []
        self.max_lines = max_lines

    def append(self, lines: list[Line]) -> None:
        if not lines:
            return
        self.lines.extend(lines)
        overflow = len(self.lines) - self.max_lines
        if overflow > 0:
            del self.lines[:overflow]

    def reset(self) -> None:
        self.lines.clear()

    def visible(self, height: int, scroll: int) -> list[Line]:
        height = max(1, height)
        scroll = max(0, min(scroll, max(0, len(self.lines) - height)))
        end = len(self.lines) - scroll
        return self.lines[max(0, end - height) : end]

    def max_scroll(self, height: int) -> int:
        return max(0, len(self.lines) - max(1, height))


class EventTail:
    """增量读取 events.jsonl，容忍末尾半行与文件被截断。"""

    def __init__(self, path: Path, initial_bytes: int = 262_144):
        self.path = Path(path)
        self.initial_bytes = initial_bytes
        self.offset = 0
        self.partial = b""

    def prime(self) -> list[Event]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        start = max(0, size - self.initial_bytes)
        with open(self.path, "rb") as handle:
            handle.seek(start)
            raw = handle.read()
        if start > 0:
            newline = raw.find(b"\n")
            if newline < 0:
                self.offset = size
                return []
            raw = raw[newline + 1 :]
            self.offset = start + newline + 1
        else:
            self.offset = len(raw)
        return self._parse(raw)

    def poll(self) -> list[Event]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
            self.partial = b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            raw = handle.read()
        self.offset = size
        return self._parse(raw)

    def _parse(self, raw: bytes) -> list[Event]:
        data = self.partial + raw
        lines = data.split(b"\n")
        self.partial = lines.pop()
        events: list[Event] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            event = Event.from_json(line.decode("utf-8", errors="replace"))
            if event is not None:
                events.append(event)
        return events


class TuiApp:
    def __init__(
        self,
        directory: Path | str,
        *,
        plain: bool = False,
        snapshot: bool = False,
        color: bool = True,
        width: int | None = None,
        height: int | None = None,
        refresh: float = 0.2,
    ):
        self.archive = Archive(Path(directory).expanduser().resolve())
        self.archive.validate()
        self.archive.ensure_runtime()
        self.config = load_config(self.archive.root)
        self.control = Control(self.archive)
        self.jobs = JobManager(self.archive)
        self.editor = LineEditor()
        self.tail = EventTail(self.archive.events_path)
        self.feed = FeedBuffer()
        self.events: deque[Event] = deque(maxlen=MAX_EVENTS)
        self.scroll = 0
        self._stop = False
        self._dirty = True
        self._flash: tuple[str, float] = ("", 0.0)
        self._prompt_total = 0
        self._pressure = 0
        self._jobs_running = 0
        self._next_status = 0.0
        self._next_jobs = 0.0
        self._old_signals: dict[int, object] = {}
        self._primed = False
        self.plain = plain
        self.snapshot = snapshot
        self.color = color and os.environ.get("NO_COLOR") is None
        self.refresh = max(0.05, float(refresh))
        self._fixed_width = width
        self._fixed_height = height
        self._cur_width = max(24, width or 100)
        self._cur_height = max(6, height or 30)
        if width is None or height is None:
            term_width, term_height = Terminal().size
            self._cur_width = max(24, width or term_width)
            self._cur_height = max(6, height or term_height)

    # ---------------- 入口 ----------------
    def run(self) -> int:
        self._ensure_primed()
        if self.snapshot:
            return self._run_snapshot()
        if self.plain:
            return self._run_plain()
        if not is_tty():
            return self._run_follow()
        return self._run_interactive()

    def _ensure_primed(self) -> None:
        if self._primed:
            return
        self._primed = True
        for event in read_events(self.archive.events_path):
            if event.type == "assistant/message":
                usage = event.data.get("usage") or {}
                prompt = usage.get("prompt_tokens")
                if isinstance(prompt, int):
                    self._prompt_total += prompt
                pressure = event.data.get("pressure_tokens")
                if isinstance(pressure, int):
                    self._pressure = pressure
        primed = self.tail.prime()
        for event in primed:
            self.events.append(event)
        self._rebuild(self._cur_width)

    # ---------------- 渲染 ----------------
    def _rebuild(self, width: int) -> None:
        self.feed.reset()
        lines: list[Line] = []
        for event in self.events:
            lines.extend(render_event(event, width))
        self.feed.append(lines)
        self.scroll = 0

    def _status(self, width: int) -> Line:
        state = self.control.read_state()
        kernel = load_kernel_state(self.archive)
        run_word = "运行中"
        if state["run"] == "pause":
            run_word = "已暂停"
        elif state["run"] == "stop":
            run_word = "已停止"
        elif kernel.get("status") in {"stopped", "error"}:
            run_word = "已停止"
        elif not self._kernel_alive(kernel):
            run_word = "未运行"
        return status_line(
            name=self.config.name,
            run_word=run_word,
            turn=int(kernel.get("turn") or 0),
            model=self.config.model,
            pressure=self._pressure,
            threshold=self.config.budget.threshold,
            prompt_total=self._prompt_total,
            jobs_running=self._jobs_running,
            width=width,
        )

    def _hint(self) -> Line:
        flash, expires = self._flash
        if flash and time.monotonic() < expires:
            return Line(flash, STYLE_HINT)
        if self.scroll > 0:
            return Line(f"已向上滚动 {self.scroll} 行 · End 回到底部 · PgDn 下翻", STYLE_HINT)
        return Line(
            "Enter 发送 · :pause :resume :stop :wake :restart · PgUp/PgDn 滚动 · Ctrl-C 退出",
            STYLE_HINT,
        )

    def _set_flash(self, message: str) -> None:
        self._flash = (message, time.monotonic() + 2.5)

    def _paint(self, line: Line) -> str:
        text = sanitize(line.text)
        if self.color and sys.stdout.isatty() and line.style:
            return f"\x1b[{line.style}m{text}\x1b[0m"
        return text

    # ---------------- 三种模式 ----------------
    def _snapshot_frame(self) -> str:
        self._ensure_primed()
        self._rebuild(self._cur_width)
        feed_height = max(1, self._cur_height - 4)
        feed = self.feed.visible(feed_height, 0)
        hint = Line("快照模式 · 去掉 --snapshot 进入交互界面", STYLE_HINT)
        frame, _, _ = render_frame(
            width=self._cur_width,
            height=self._cur_height,
            status=self._status(self._cur_width),
            feed=feed,
            input_text="",
            cursor=0,
            hint=hint,
            color=self.color,
        )
        return frame

    def _run_snapshot(self) -> int:
        sys.stdout.write(self._snapshot_frame() + "\n")
        return 0

    def _run_follow(self) -> int:
        """非终端环境：持续按行打印新事件，直到被停止（适合 nohup / 管道 / CI）。"""
        feed_height = max(1, self._cur_height - 4)
        for line in self.feed.visible(feed_height, 0):
            print(self._paint(line), flush=True)
        try:
            while not self._stop:
                self._poll(emit=True)
                time.sleep(self.refresh)
        except KeyboardInterrupt:
            pass
        return 0

    def _run_plain(self) -> int:
        feed_height = max(1, self._cur_height - 4)
        for line in self.feed.visible(feed_height, 0):
            print(self._paint(line), flush=True)
        print(self._paint(Line("输入消息后回车发送；:q 退出；:pause/:resume/:stop 控制内核", STYLE_HINT)), flush=True)

        lines: "queue.Queue[str]" = queue.Queue()

        def reader() -> None:
            while True:
                line = sys.stdin.readline()
                if not line:
                    lines.put("\x04")
                    return
                lines.put(line.rstrip("\n"))

        threading.Thread(target=reader, daemon=True).start()
        try:
            while not self._stop:
                self._poll(emit=True)
                try:
                    text = lines.get(timeout=self.refresh)
                except queue.Empty:
                    continue
                if text == "\x04":
                    break
                self._submit(text)
        except KeyboardInterrupt:
            pass
        return 0

    def _run_interactive(self) -> int:
        try:
            with Terminal(color=self.color) as term:
                self._install_signals()
                last_size = (0, 0)
                while not self._stop:
                    raw_size = term.size
                    size = (
                        self._fixed_width or raw_size[0],
                        self._fixed_height or raw_size[1],
                    )
                    if size != last_size:
                        last_size = size
                        self._cur_width, self._cur_height = size
                        self._rebuild(size[0])
                        self._dirty = True
                    if self._poll():
                        self._dirty = True
                    if self._dirty:
                        self._draw(term)
                        self._dirty = False
                    for key in term.read_keys(self.refresh):
                        self._handle_key(key)
                        self._dirty = True
        except KeyboardInterrupt:
            pass
        finally:
            self._restore_signals()
        return 0

    def _draw(self, term: Terminal) -> None:
        width, height = self._cur_width, self._cur_height
        feed_height = max(1, height - 4)
        frame, row, column = render_frame(
            width=width,
            height=height,
            status=self._status(width),
            feed=self.feed.visible(feed_height, self.scroll),
            input_text=self.editor.text,
            cursor=self.editor.cursor,
            hint=self._hint(),
            color=self.color,
        )
        term.write(f"\x1b[?25l\x1b[H{frame}\x1b[{row};{column}H\x1b[?25h")

    # ---------------- 事件与状态 ----------------
    def _poll(self, *, emit: bool = False) -> bool:
        changed = False
        new_lines: list[Line] = []
        events = self.tail.poll()
        if events:
            for event in events:
                self.events.append(event)
                if event.type == "assistant/message":
                    usage = event.data.get("usage") or {}
                    prompt = usage.get("prompt_tokens")
                    if isinstance(prompt, int):
                        self._prompt_total += prompt
                    pressure = event.data.get("pressure_tokens")
                    if isinstance(pressure, int):
                        self._pressure = pressure
            for event in events:
                new_lines.extend(render_event(event, self._cur_width))
            if new_lines:
                self.feed.append(new_lines)
                if self.scroll > 0:
                    self.scroll = min(
                        self.scroll + len(new_lines),
                        self.feed.max_scroll(max(1, self._cur_height - 4)),
                    )
                changed = True
        if emit:
            for line in new_lines:
                print(self._paint(line), flush=True)

        now = time.monotonic()
        if now >= self._next_status:
            self._next_status = now + STATUS_INTERVAL
            changed = True
        if now >= self._next_jobs:
            self._next_jobs = now + JOBS_INTERVAL
            try:
                self._jobs_running = sum(
                    1 for job in self.jobs.list() if job.status in {"running", "stopping"}
                )
            except OSError:
                self._jobs_running = 0
        return changed

    # ---------------- 按键 ----------------
    def _handle_key(self, key: str) -> None:
        if key in {"CTRL_C", "EOF"}:
            self._stop = True
            return
        if key == "CTRL_L":
            return
        if key in {"HOME", "END"}:
            if self.editor.text:
                self.editor.handle(key)
            else:
                feed_height = max(1, self._cur_height - 4)
                self.scroll = self.feed.max_scroll(feed_height) if key == "HOME" else 0
            return
        if key in {"PGUP", "PGDN"}:
            feed_height = max(1, self._cur_height - 4)
            if key == "PGUP":
                self.scroll = min(
                    self.feed.max_scroll(feed_height), self.scroll + max(1, feed_height - 1)
                )
            else:
                self.scroll = max(0, self.scroll - max(1, feed_height - 1))
            return
        if key == "ESC":
            if self.editor.text:
                self.editor.clear()
            else:
                self.control.request_wake()
                self._set_flash("已请求唤醒")
            return
        submitted = self.editor.handle(key)
        if submitted is not None:
            self._submit(submitted)

    def _submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.scroll = 0
        if text.startswith(":"):
            self._command(text[1:].strip())
            return
        self.control.push_message(text)
        self._set_flash(f"已发送（{len(text)} 字），等待内核读取")

    def _command(self, command: str) -> None:
        parts = command.split()
        if not parts:
            return
        name = parts[0].lower()
        if name in {"q", "quit", "exit"}:
            self._stop = True
        elif name == "pause":
            self.control.write_state(run="pause")
            self._set_flash("已暂停（内核不再调用 API）")
        elif name == "resume":
            self.control.write_state(run="run")
            self._set_flash("已恢复")
        elif name == "stop":
            self.control.write_state(run="stop")
            self._set_flash("已请求停止，内核会收尾退出")
        elif name in {"wake", "w"}:
            self.control.request_wake()
            self._set_flash("已请求唤醒，内核会提前结束当前等待")
        elif name in {"restart", "reload"}:
            self.control.request_restart("tui")
            self._set_flash("已请求交班重启，新进程会在旧进程退出后接管")
        elif name == "delay":
            try:
                value = float(parts[1])
            except (IndexError, ValueError):
                self._set_flash("用法：:delay 5")
                return
            self.control.write_state(delay=value)
            self._set_flash(f"每轮延时已设为 {value:g} 秒")
        elif name == "help":
            self.feed.append(
                [
                    Line("※ 命令：:pause  :resume  :stop  :delay N  :quit", STYLE_DIM),
                    Line("※ 按键：Enter 发送 · PgUp/PgDn 滚动 · End 回到底部 · Ctrl-C 退出", STYLE_DIM),
                ]
            )
        else:
            self._set_flash(f"未知命令 {name}（:help 查看）")

    # ---------------- 信号与工具 ----------------
    def _kernel_alive(self, kernel: dict) -> bool:
        pid = kernel.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        if os.name == "windows":
            try:
                import ctypes

                synchronize = 0x00100000
                handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)  # type: ignore[attr-defined]
                if not handle:
                    return False
                try:
                    code = ctypes.c_ulong()
                    ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))  # type: ignore[attr-defined]
                    return bool(ok) and code.value == 259  # STILL_ACTIVE
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def _install_signals(self) -> None:
        def handler(signum, frame):  # noqa: ANN001, ARG001
            if signum == getattr(signal, "SIGWINCH", -1):
                self._dirty = True
            else:
                self._stop = True

        candidates = [signal.SIGTERM, signal.SIGINT]
        winch = getattr(signal, "SIGWINCH", None)
        if winch is not None:
            candidates.append(winch)
        for signum in candidates:
            try:
                self._old_signals[signum] = signal.getsignal(signum)
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    def _restore_signals(self) -> None:
        for signum, old in self._old_signals.items():
            try:
                signal.signal(signum, old)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        self._old_signals.clear()


def run_tui(
    directory: Path | str,
    *,
    plain: bool = False,
    snapshot: bool = False,
    color: bool = True,
    width: int | None = None,
    height: int | None = None,
    refresh: float = 0.2,
) -> int:
    app = TuiApp(
        directory,
        plain=plain,
        snapshot=snapshot,
        color=color,
        width=width,
        height=height,
        refresh=refresh,
    )
    return app.run()
