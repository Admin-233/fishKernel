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
from ..config import load_config, ConfigError
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
    truncate,
    wrap_text,
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
        self.identity = None
        self.reset = False

    def prime(self) -> list[Event]:
        try:
            with self.path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                self.identity = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                self.partial = b""
                start = max(0, stat.st_size - self.initial_bytes) if self.initial_bytes > 0 else 0
                handle.seek(start)
                raw = handle.read(stat.st_size - start)
                self.offset = handle.tell()
        except FileNotFoundError:
            return []
        if start > 0:
            newline = raw.find(b"\n")
            if newline < 0:
                return []
            raw = raw[newline + 1:]
        return self._parse(raw)

    def poll(self) -> list[Event]:
        try:
            with self.path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                identity = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                self.reset = bool(self.identity and (self.identity[0] != identity[0] or stat.st_size < self.offset
                                  or (stat.st_size == self.offset and self.identity[1] != identity[1])))
                self.identity = identity
                if self.reset:
                    self.offset = 0
                    self.partial = b""
                if stat.st_size == self.offset:
                    return []
                handle.seek(self.offset)
                raw = handle.read(stat.st_size - self.offset)
                self.offset = handle.tell()
        except FileNotFoundError:
            return []
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
        self._chat_ids = [e.seq for e in self.events
                          if e.type in {"user/message", "assistant/message"} and str(e.data.get("text", "")).strip()]
        # 右栏：完整记录（工具调用 / 工具结果 / 思维链）。
        # 只取「过程类」事件，对话正文已经由左栏承担，避免两边重复。
        detail: list[Line] = []
        detail_ids = []
        total = len(self.events)
        recent_events = list(itertools.islice(self.events, 0, total))
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
            detail_ids.extend((event.seq, i) for i in range(len(lines)))
        self._detail_lines = detail
        self._detail_ids = detail_ids

    def _pane(self, entries, height, name):
        page_name = "_" + name + "_page"
        anchor_name = "_" + name + "_anchor"
        offset = getattr(self, page_name, 0)
        anchor = getattr(self, anchor_name, None)
        if offset and anchor is not None:
            candidates = [i for i, (_, key) in enumerate(entries)
                          if key[:2] == anchor[:2] and key[2] <= anchor[2]]
            if candidates:
                offset = max(0, len(entries) - candidates[-1] - height)
        offset = max(0, min(offset, max(0, len(entries) - height)))
        setattr(self, page_name, offset)
        end = len(entries) - offset
        shown = entries[max(0, end - height):end]
        setattr(self, anchor_name, shown[0][1] if offset and shown else None)
        setattr(self, "_" + name + "_total", len(entries))
        return [line for line, _ in shown]

    def _chat_lines(self, width: int, height: int | None = None) -> list[Line]:
        left_w = max(8, int(width * self.CHAT_SPLIT) - 1)
        right_w = max(8, width - left_w - 1)
        rows_n = height or max(1, self._cur_height - 4)
        left = []
        ids = getattr(self, "_chat_ids", [])
        for index, (label, text) in enumerate(getattr(self, "_chat_turns", [])):
            identity = ids[index] if index < len(ids) else index
            prefix = f"{label}│"
            body_w = max(1, left_w - display_width(prefix))
            style = STYLE_USER if label == "你" else STYLE_ASSISTANT
            first = True
            for logical_line, raw in enumerate(str(text).splitlines() or [""]):
                position = 0
                for segment in wrap_text(raw.expandtabs(4), body_w) or [""]:
                    head = prefix if first else " " * display_width(prefix)
                    left.append((Line(head + segment, style), (identity, logical_line, position)))
                    position += len(segment)
                    first = False
        right = []
        detail_ids = getattr(self, "_detail_ids", [])
        for index, line in enumerate(getattr(self, "_detail_lines", [])):
            identity, logical_line = detail_ids[index] if index < len(detail_ids) else (index, 0)
            position = 0
            for segment in wrap_text(line.text, right_w) or [""]:
                right.append((Line(segment, line.style), (identity, logical_line, position)))
                position += len(segment)
        left = self._pane(left, rows_n, "left")
        right = self._pane(right, rows_n, "right")
        result = []
        for i in range(rows_n):
            l = left[i].text if i < len(left) else ""
            rr = right[i].text if i < len(right) else ""
            result.append(Line(l + " " * max(0, left_w - display_width(l)) + "│" + rr,
                               left[i].style if i < len(left) else STYLE_DIM))
        return result

    def _rebuild(self, width: int) -> None:  # noqa: D401
        self._refresh_chat()
        self.feed.reset()
        lines: list[Line] = []
        for event in self.events:
            lines.extend(render_event(event, width))
        self.feed.append(lines)
        self.scroll = 0

    def _status(self, width: int) -> Line:
        from ..plugins import read_status
        from ..tokens import fold_usage
        from ..usage import usage_events
        try:
            self.config = load_config(self.archive.root)
        except ConfigError:
            pass
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
        if kernel.get("status") == "config_blocked":
            run_word = "配置阻塞"
        plugins = read_status(self.archive)
        totals = fold_usage(usage_events(self.archive))
        failures = sum(bool(p.get("last_error")) for p in plugins)
        mode = kernel.get("thinking", self.config.thinking)
        thinking = "服务端默认" if mode == "auto" else str(mode)
        return status_line(
            name=self.config.name,
            run_word=run_word,
            turn=int(kernel.get("turn") or 0),
            model=kernel.get("model", self.config.model),
            pressure=self._pressure,
            threshold=self.config.budget.threshold,
            prompt_total=totals.prompt_tokens,
            jobs_running=self._jobs_running,
            width=width,
            thinking=thinking,
            plugin_status=f"插件 {len(plugins)} / 异常 {failures}",
            cost_text=totals.cost_text(),
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
        # 这里的 chat 必须按 body_h 计算并原样传下去。
        # 曾经写成「算出 chat 却不用，render_frame 里再 _chat_lines(width) 一次」：
        # 后者拿不到 height，_chat_lines 退化成 CHAT_ROWS or 8 行，
        # 而 render_frame 又按 height-4 去切片 —— 窗口高 30 行时，
        # 快照只能显示 8 行消息区，其余 18 行是黑的。
        # 交互路径（见下方 chat=self._chat_lines(width, body_h)）一直是对的，
        # 只有快照路径漏了，所以肉眼只在 --snapshot 下看出问题。
        chat = self._chat_lines(self._cur_width, body_h)
        hint = Line("快照模式 · 去掉 --snapshot 进入交互界面", STYLE_HINT)
        frame, _, _ = render_frame(
            width=self._cur_width,
            height=self._cur_height,
            status=self._status(self._cur_width),
            feed=[],
            input_text="",
            cursor=0,
            hint=hint,
            color=self.color,
            chat=chat,
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
        if self.tail.reset:
            self.events.clear()
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
            if events:
                # 关键：分栏数据必须跟着事件刷新。
                # 之前只把事件塞进 feed（下方事件流），_chat_turns / _detail_lines
                # 停在启动那一刻，所以左右两栏看起来"不实时"。
                self._refresh_chat()
                # 有事件就必须重绘，否则新内容要等到下一次状态栏刷新才出现
                changed = True
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
        return max(1, self._cur_height - 5)

    def _page_by(self, delta: int) -> None:
        self._chat_lines(self._cur_width, max(1, self._cur_height - 4))
        name = self._focus
        total = getattr(self, "_" + name + "_total", 0)
        height = max(1, self._cur_height - 4)
        offset = getattr(self, "_" + name + "_page", 0)
        setattr(self, "_" + name + "_page", max(0, min(offset + delta, max(0, total - height))))
        setattr(self, "_" + name + "_anchor", None)

    def _page_to_start(self) -> None:
        self._page_by(10 ** 12)

    def _page_to_end(self) -> None:
        setattr(self, "_" + self._focus + "_page", 0)
        setattr(self, "_" + self._focus + "_anchor", None)

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
            if key in {"UP", "DOWN"}:
                self._page_by(1 if key == "UP" else -1)
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
        from ..lock import LockError
        try:
            self.control.push_message(text)
        except (ValueError, OSError, LockError) as exc:
            self.editor.set_text(text)
            self._set_flash(f"投递失败，内容已保留：{exc}")
            return
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
        from ..platform import process_alive, UnsupportedPlatformError
        try:
            return process_alive(kernel.get("pid"), kernel.get("process_identity"))
        except UnsupportedPlatformError:
            return False

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
