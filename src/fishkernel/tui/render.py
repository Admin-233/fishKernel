"""TUI 的纯渲染层：宽度计算、事件着色、工具压缩、整帧拼装。

这里不碰终端、不读文件，全部是可直接单测的纯函数。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..events import Event

# ---- 颜色（SGR 参数）----
STYLE_USER = "1;36"
STYLE_ASSISTANT = "32"
STYLE_CALL = "33"
STYLE_TOOL_OK = "36"
STYLE_TOOL_FAIL = "1;31"
STYLE_JOB = "34"
STYLE_SYSTEM = "90"
STYLE_COMPACT = "35"
STYLE_ERROR = "1;31"
STYLE_DIM = "90"
STYLE_STATUS = "1;97;44"
STYLE_INPUT = "1;37"
STYLE_HINT = "90"

MAX_USER_LINES = 200
MAX_ASSISTANT_LINES = 400
MAX_SYSTEM_LINES = 100
MAX_ERROR_LINES = 60
MAX_UNKNOWN_LINES = 40


@dataclass(frozen=True)
class Line:
    text: str
    style: str = ""


# ---------------- 宽度 ----------------
def cell_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(cell_width(ch) for ch in text)


def truncate(text: str, width: int, suffix: str = "…") -> str:
    """按显示宽度截断，超出时以 suffix 结尾。"""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    suffix_width = display_width(suffix)
    if suffix_width >= width:
        return suffix[:width]
    target = width - suffix_width
    out: list[str] = []
    used = 0
    for ch in text:
        width_ch = cell_width(ch)
        if used + width_ch > target:
            break
        out.append(ch)
        used += width_ch
    return "".join(out) + suffix


def wrap_text(text: str, width: int) -> list[str]:
    """按显示宽度折行，保留原有换行。"""
    width = max(1, width)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current: list[str] = []
        used = 0
        for ch in paragraph:
            width_ch = cell_width(ch)
            if used + width_ch > width and current:
                lines.append("".join(current))
                current = []
                used = 0
            current.append(ch)
            used += width_ch
        if current:
            lines.append("".join(current))
    return lines or [""]


def pad_to(text: str, width: int) -> str:
    text = truncate(text, width)
    return text + " " * max(0, width - display_width(text))


def sanitize(text: str) -> str:
    """去掉控制字符，避免模型输出里的转义序列破坏终端。"""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in "\n\t":
            out.append(ch)
        elif code < 32 or code == 127:
            continue
        else:
            out.append(ch)
    return "".join(out)


# ---------------- 事件渲染 ----------------
def _ts(event: Event) -> str:
    raw = event.ts or ""
    try:
        return datetime.fromisoformat(raw).strftime("%H:%M:%S")
    except ValueError:
        return raw[-8:] if len(raw) >= 8 else ""


def _fmt_int(value: int | None) -> str:
    if not value:
        return "0"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _one_line(text: str, limit: int = 80) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def compress_args(arguments: str, limit: int = 72) -> str:
    """把工具调用的 JSON 参数压成 key=value, ... 的一行。"""
    raw = (arguments or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return _one_line(raw, limit)
    if not isinstance(payload, dict):
        return _one_line(raw, limit)
    parts: list[str] = []
    for key, value in payload.items():
        if isinstance(value, str):
            rendered = _one_line(value, 40)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
            if len(rendered) > 40:
                rendered = rendered[:39] + "…"
        parts.append(f"{key}={rendered}")
    return _one_line(", ".join(parts), limit)


def summarize_tool_result(name: str, output: str) -> str:
    """把工具结果压成一句话；解析失败就压原始文本。"""
    try:
        payload = json.loads(output) if isinstance(output, str) else output
    except (ValueError, TypeError):
        return _one_line(str(output), 90)
    if not isinstance(payload, dict):
        return _one_line(json.dumps(payload, ensure_ascii=False), 90)
    if not payload.get("ok", True):
        error = payload.get("error") or {}
        code = _one_line(str(error.get("code", "error")), 24)
        message = _one_line(str(error.get("message", "")), 70)
        return f"{code} · {message}"
    if name == "read_file":
        more = "，还有更多" if payload.get("has_more") else "，已到末尾"
        return f"返回 {payload.get('returned_lines', '?')} 行（offset {payload.get('offset', 0)}{more}）"
    if name == "list_dir":
        suffix = "（已截断）" if payload.get("truncated") else ""
        return f"{payload.get('total', '?')} 项{suffix}"
    if name == "spawn":
        return f"启动 {payload.get('job_id', '?')}"
    if name == "job_output":
        data = payload.get("data") or ""
        tail = "EOF" if payload.get("eof") else "还有更多"
        return f"读取 {len(data)} 字符 · offset→{payload.get('next_offset', '?')} · {tail}"
    if name == "job_list":
        return f"{payload.get('count', '?')} 个任务"
    if name == "job_kill":
        return f"{payload.get('job', {}).get('status', '?')}"
    if name == "wait_job":
        job = payload.get("job") or {}
        if payload.get("timeout"):
            return f"{job.get('id', '?')} 仍在运行（等待超时）"
        return f"{job.get('id', '?')} → {job.get('status', '?')}（exit {job.get('exit_code')}）"
    if name == "sleep":
        reason = payload.get("interrupted")
        return f"睡了 {payload.get('slept_seconds', 0)} 秒" + (f"（{reason}）" if reason else "")
    if name == "memory":
        return f"{payload.get('mode', '?')} · {payload.get('bytes', '?')} 字节"
    return _one_line(json.dumps(payload, ensure_ascii=False), 90)


def _block(prefix: str, text: str, width: int, style: str, max_lines: int) -> list[Line]:
    prefix_width = display_width(prefix)
    available = max(1, width - prefix_width)
    wrapped = wrap_text(text.strip(), available)
    lines = [Line(prefix + wrapped[0], style)]
    for continuation in wrapped[1:]:
        lines.append(Line(" " * prefix_width + continuation, style))
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines + 1
        lines = lines[: max_lines - 1]
        lines.append(Line(" " * prefix_width + f"…（还有 {omitted} 行，完整内容见 events.jsonl）", STYLE_DIM))
    return lines


def render_event(event: Event, width: int) -> list[Line]:
    """一条事件 → 若干显示行。"""
    width = max(20, width)
    stamp = _ts(event)
    prefix = f"{stamp} " if stamp else ""
    data: dict[str, Any] = event.data or {}

    if event.type == "session/start":
        name = data.get("model_name") or data.get("model") or "?"
        window = _fmt_int(data.get("context_window") if isinstance(data.get("context_window"), int) else None)
        return [Line(f"══ 内核启动 · {name} · 窗口 {window} ══", STYLE_DIM)]
    if event.type == "session/end":
        return [Line(f"══ 内核退出：{data.get('reason', '?')} · 共 {data.get('turns', '?')} 轮 ══", STYLE_DIM)]
    if event.type == "turn/start":
        return [Line(f"── 第 {data.get('turn', '?')} 轮 ──", STYLE_DIM)]
    if event.type == "turn/end":
        return []
    if event.type == "user/message":
        return _block(f"{prefix}你   ", str(data.get("text", "")), width, STYLE_USER, MAX_USER_LINES)
    if event.type == "assistant/message":
        lines: list[Line] = []
        text = str(data.get("text") or "")
        if text.strip():
            lines.extend(_block(f"{prefix}助手 ", text, width, STYLE_ASSISTANT, MAX_ASSISTANT_LINES))
        else:
            lines.append(Line(f"{prefix}助手 （无文字）", STYLE_DIM))
        for call in data.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name", "?"))
            args = compress_args(str(call.get("arguments", "")))
            lines.append(Line(truncate(f"{prefix}  → {name}({args})", width), STYLE_CALL))
        return lines
    if event.type == "tool/result":
        ok = bool(data.get("ok"))
        name = str(data.get("name", "?"))
        duration = data.get("duration_ms")
        summary = summarize_tool_result(name, str(data.get("output", "")))
        icon = "✓" if ok else "✗"
        tail = f" · {duration}ms" if isinstance(duration, int) and duration > 0 else ""
        style = STYLE_TOOL_OK if ok else STYLE_TOOL_FAIL
        return [Line(truncate(f"{prefix}{icon} {name}{tail} · {summary}", width), style)]
    if event.type == "system/message":
        if data.get("kind") == "wake":
            return []  # 空转提醒是噪音，事件日志里仍有
        return _block(f"{prefix}系统 ", str(data.get("text", "")), width, STYLE_SYSTEM, MAX_SYSTEM_LINES)
    if event.type == "job/settle":
        status = data.get("status", "?")
        exit_code = data.get("exit_code")
        suffix = f" · exit {exit_code}" if exit_code is not None else ""
        return [Line(truncate(f"{prefix}⚙ 任务 {data.get('job_id', '?')} {status}{suffix}", width), STYLE_JOB)]
    if event.type == "session/handover":
        return [Line(truncate(f"⇄ 交班 → 新进程 {data.get('target', '?')}（旧 pid {data.get('pid', '?')}）", width), STYLE_JOB)]
    if event.type == "delay":
        return []  # 每轮都有，属于噪音；时间流逝由状态条体现
    if event.type == "compaction/start":
        return [Line(f"{prefix}◆ 压缩开始（{data.get('trigger', '?')}）", STYLE_COMPACT)]
    if event.type == "compaction/summary":
        freed = data.get("freed_tokens", 0)
        archived = data.get("archived") or ""
        return [Line(truncate(f"{prefix}◆ 压缩完成 · 释放约 {freed} tokens · {archived}", width), STYLE_COMPACT)]
    if event.type == "compaction/end":
        return []
    if event.type == "compaction/abort":
        return [Line(truncate(f"{prefix}◆ 压缩放弃：{data.get('reason', '?')}", width), STYLE_COMPACT)]
    if event.type == "error":
        return _block(
            f"{prefix}! 错误 ",
            f"{data.get('stage', '?')}：{data.get('message', '')}",
            width,
            STYLE_ERROR,
            MAX_ERROR_LINES,
        )
    return _block(
        f"{prefix}· {event.type} ",
        json.dumps(data, ensure_ascii=False),
        width,
        STYLE_DIM,
        MAX_UNKNOWN_LINES,
    )


# ---------------- 状态条 / 整帧 ----------------
def status_line(
    *,
    name: str,
    run_word: str,
    turn: int,
    model: str,
    pressure: int,
    threshold: int,
    prompt_total: int,
    jobs_running: int,
    width: int,
    cost_yuan: float | None = None,
) -> Line:
    parts = [
        f" fishKernel · {name} · {run_word} · 第 {turn} 轮 · {model}",
        f"压力 {_fmt_int(pressure)}/{_fmt_int(threshold)}",
        f"用量 {_fmt_int(prompt_total)}",
    ]
    # 成本比 token 数直观：用户真正关心的是余额还剩多少
    if cost_yuan is not None:
        parts.append(f"¥{cost_yuan:.2f}")
    parts.append(f"任务 {jobs_running}")
    text = " · ".join(parts) + " "
    return Line(text, STYLE_STATUS)


def visible_input(text: str, cursor: int, available: int) -> tuple[str, int]:
    """输入行的横向滚动：保证光标可见，返回 (显示文本, 光标前字符数)。"""
    available = max(1, available)
    cursor = max(0, min(cursor, len(text)))
    # 从后往前留出 1 格余量
    start = 0
    while start < cursor and display_width(text[start:cursor]) > available - 1:
        start += 1
    shown = text[start:]
    shown = truncate(shown, available, suffix="")
    before = text[start:cursor]
    if display_width(before) > available:
        before = truncate(before, available, suffix="")
    return shown, display_width(before)


def render_frame(
    *,
    width: int,
    height: int,
    status: Line,
    feed: list[Line],
    input_text: str,
    cursor: int,
    hint: Line,
    color: bool = True,
    chat: list[Line] | None = None,
) -> tuple[str, int, int]:
    """拼出整帧。返回 (帧文本, 光标行, 光标列)，行列从 1 开始。

    chat 非空时，它会被"钉"在顶部（固定行数，不随滚动移动），
    下方才是可滚动的完整事件流。用来把"你和我之间的对话"从
    高速刷屏的工具输出里摘出来。
    """
    width = max(24, width)
    height = max(6, height)
    chat_rows = list(chat or [])
    # 状态(1) + 分隔(1) + 输入(1) + 提示(1) = 4 行固定开销
    feed_height = max(1, height - 4)
    rows: list[Line] = [status]

    if chat_rows:
        # 左右分屏：chat 行本身已含「左 │ 右」，占满整个消息区
        rows.extend(chat_rows[:feed_height])
    else:
        rows.extend(feed[-feed_height:])

    while len(rows) < 1 + feed_height:
        rows.append(Line(""))
    rows.append(Line("─" * width, STYLE_DIM))
    rows.append(Line("> " + input_text, STYLE_INPUT))
    rows.append(hint)

    painted: list[str] = []
    for line in rows:
        text = truncate(sanitize(line.text), width)
        padded = text + " " * max(0, width - display_width(text))
        if color and line.style:
            painted.append(f"\x1b[{line.style}m{padded}\x1b[0m")
        else:
            painted.append(padded)

    # 光标：输入行是倒数第二行
    shown_before = 0
    if len(input_text) > 0:
        _, shown_before = visible_input(input_text, cursor, width - 2)
    cursor_row = height - 1
    cursor_col = min(width, 3 + shown_before)
    return "\r\n".join(painted), cursor_row, cursor_col
