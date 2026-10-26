"""TUI 应用：跟随事件日志、状态条、输入框与按键循环。"""

from __future__ import annotations

import itertools
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
    STYLE_ASSISTANT,
    STYLE_USER,
    STYLE_DIM,
    STYLE_HINT,
    Line,
    display_width,
    render_event,
    render_frame,
    sanitize,
    status_line,
)
from .terminal import Terminal, is_tty

MAX_FEED_LINES = 5000
# 事件缓存上限。左右栏要能翻历史，所以必须装得下完整会话；
# 50000 条事件约 30MB 内存，对本工具可接受。
MAX_EVENTS = 50_000
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

    def __init__(self, path: Path, initial_bytes: int = 0):
        # initial_bytes=0 表示「从头读」，翻页需要完整历史。
        self.path = Path(path)
        self.initial_bytes = initial_bytes
        self.offset = 0
        self.partial = b""

    def prime(self) -> list[Event]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        start = max(0, size - self.initial_bytes) if self.initial_bytes > 0 else 0
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
    # ---------------- 置顶聊天区 ----------------
    CHAT_SPLIT = 0.42      # 左栏（人机对话）占宽度的比例，其余给右栏（完整记录）
    # 左右栏各自的翻页偏移：0 = 停在最新，正数 = 往回翻多少轮/行
    _left_page = 0
    _right_page = 0
    _focus = "left"        # 当前用 PgUp/PgDn 翻哪一栏：left / right
    CHAT_ROWS = 0          # 运行时按终端高度计算，0 表示「占满消息区」
    CHAT_MAX_WIDTH = 68    # 单行最长多少字符，避免撑爆

    def _refresh_chat(self) -> None:
        """从事件里挑出"人的对话"，钉在顶部。

        只收 user/message 与 assistant/message 的**正文**；
        工具调用、任务结算、delay 这些噪音不进聊天区。
        """
        turns: list[tuple[str, str]] = []
        for event in self.events:
            if event.type == "user/message":
                source = event.data.get("source")
                text = str(event.data.get("text", "")).strip()
                if text:
                    label = "你" if source == "control" else "系统"
                    turns.append((label, text))
            elif event.type == "assistant/message":
                text = str(event.data.get("text", "")).strip()
                if text:
                    turns.append(("我", text))

        self._chat_turns = turns
        # 右栏：完整记录（工具调用 / 工具结果 / 思维链）。
        # 只取「过程类」事件，对话正文已经由左栏承担，避免两边重复。
        detail: list[Line] = []
        total = len(self.events)
        recent_events = list(itertools.islice(self.events, max(0, total - 300), total))
        for event in recent_events:
            if event.type in {"user/message", "delay", "turn/start", "turn/end",
                              "session/start", "session/end"}:
                continue
            lines = render_event(event, 200)
            if event.type == "tool/result":
                # 工具输出常常几十行，右栏只需要「调用了什么、成功没有」，
                # 全文留给下方的事件流区。
                data = event.data
                ok = "✓" if data.get("ok") else "✗"
                out_len = len(str(data.get("output", "")))
                detail.append(Line(f"  {ok} {data.get('name','?')} · {out_len} 字符", STYLE_DIM))
                continue
            detail.extend(lines)
        self._detail_lines = detail

    def _chat_lines(self, width: int, height: int | None = None) -> list[Line]:
        """构造顶部分栏区：左边是「人机对话」，右边是「完整记录（含思维链）」。

        左边的信息密度低但最重要（我说了什么、你说了什么）；
        右边的信息密度高（工具调用、工具输出、思维链），并且变化快。
        分成两栏后，左侧不会被工具刷屏冲掉。
        """
        left_w = max(20, int(width * self.CHAT_SPLIT) - 1)
        right_w = max(10, width - left_w - 1)
        # 占满消息区：高度 - 状态(1) - 分隔(1) - 输入(1) - 提示(1)
        rows_n = height if height and height > 0 else self.CHAT_ROWS or 8

        # ---- 左栏：人机对话（支持翻页）----
        left: list[Line] = []
        turns_all = getattr(self, "_chat_turns", [])
        # 每条消息占一行，按行数翻页；_left_page 表示从末尾往回偏移多少行
        end = max(0, len(turns_all) - self._left_page)
        start = max(0, end - rows_n)
        recent = turns_all[start:end]
        for label, text in recent:
            one_line = " ".join(text.split())
            clipped = one_line[: left_w - 2]
            if len(one_line) > left_w - 2:
                clipped += "…"
            style = STYLE_USER if label == "你" else STYLE_ASSISTANT
            left.append(Line(f"{label}│{clipped}", style))
        left = left[-rows_n:]

        # ---- 右栏：完整记录里的最近若干条（工具调用/结果/思维链）----
        right: list[Line] = []
        all_detail = getattr(self, "_detail_lines", [])
        end_r = max(0, len(all_detail) - self._right_page)
        start_r = max(0, end_r - rows_n * 2)   # 多取一些，跳过空行后仍能填满
        for line in all_detail[start_r:end_r]:
            txt = " ".join(line.text.split())
            if not txt:
                continue
            clipped = txt[: right_w - 1]
            if len(txt) > right_w - 1:
                clipped += "…"
            right.append(Line(clipped, line.style))
        right = right[-rows_n:]

        # ---- 拼成两栏 ----
        out: list[Line] = []
        for i in range(rows_n):
            l = left[i].text if i < len(left) else ""
            r = right[i].text if i < len(right) else ""
            pad = " " * max(0, left_w - display_width(l))
            style = left[i].style if i < len(left) else STYLE_DIM
            out.append(Line(f"{l}{pad}│{r}", style))
        return out

    def _rebuild(self, width: int) -> None:  # noqa: D401
        self._refresh_chat()
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
        body_h = max(1, self._cur_height - 4)
        chat = self._chat_lines(self._cur_width, body_h)
        feed = []
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
            chat=self._chat_lines(self._cur_width),
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
        # 整个消息区左右分屏：chat 自己占满高度，下方事件流不再单独显示
        body_h = max(1, height - 4)
        frame, row, column = render_frame(
            width=width,
            height=height,
            status=self._status(width),
            feed=[],
            input_text=self.editor.text,
            cursor=self.editor.cursor,
            hint=self._hint(),
            color=self.color,
            chat=self._chat_lines(width, body_h),
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

    # ---------------- 分栏翻页 ----------------
    def _page_step(self) -> int:
        """一页翻多少：取当前消息区可见行数的一半，避免一次翻过头。"""
        body = max(1, self._cur_height - 4)
        return max(1, body // 2)

    def _page_by(self, delta: int) -> None:
        """delta > 0 往回翻（更旧），delta < 0 往前翻（更新）。"""
        if self._focus == "left":
            total = len(getattr(self, "_chat_turns", []))
            self._left_page = max(0, min(self._left_page + delta, max(0, total - 1)))
            where = f"左栏 {self._left_page}/{max(0, total - 1)}"
        else:
            total = len(getattr(self, "_detail_lines", []))
            self._right_page = max(0, min(self._right_page + delta, max(0, total - 1)))
            where = f"右栏 {self._right_page}/{max(0, total - 1)}"
        self._set_flash(f"已翻页 · {where}（Tab 切换栏）")

    def _page_to_start(self) -> None:
        if self._focus == "left":
            self._left_page = max(0, len(getattr(self, "_chat_turns", [])) - 1)
        else:
            self._right_page = max(0, len(getattr(self, "_detail_lines", [])) - 1)
        self._set_flash("已跳到最早（End 回最新）")

    def _page_to_end(self) -> None:
        if self._focus == "left":
            self._left_page = 0
        else:
            self._right_page = 0
        self._set_flash("已回到最新")

    # ---------------- 按键 ----------------
    def _handle_key(self, key: str) -> None:
        if key in {"CTRL_C", "EOF"}:
            self._stop = True
            return
        if key == "CTRL_L":
            return
        if self.editor.text:
            # 输入框有内容时，方向/翻页键交给编辑器
            if key in {"HOME", "END", "PGUP", "PGDN"}:
                self.editor.handle(key)
                return
        else:
            # 分栏翻页：Tab 切换焦点栏，PgUp/PgDn 翻当前焦点，Home/End 跳首尾
            if key == "TAB":
                self._focus = "right" if self._focus == "left" else "left"
                name = "左栏（对话）" if self._focus == "left" else "右栏（记录）"
                self._set_flash(f"翻页焦点：{name}")
                return
            page = self._page_step()
            if key == "PGUP":
                self._page_by(page)
                return
            if key == "PGDN":
                self._page_by(-page)
                return
            if key == "HOME":
                self._page_to_start()
                return
            if key == "END":
                self._page_to_end()
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
