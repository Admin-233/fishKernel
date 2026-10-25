"""工具集：同步读取、异步进程、延时、长期记忆。

返回值统一是 JSON 字符串：
  成功 {"ok": true, ...}
  失败 {"ok": false, "error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .archive import Archive
from .constants import (
    JOB_OUTPUT_CHUNK,
    MAX_INLINE_RESULT_CHARS,
    MAX_LIST_ENTRIES,
    MAX_MEMORY_BYTES,
    MAX_READ_BYTES,
    MAX_SLEEP_SECONDS,
)
from .control import Control
from .jobs import JobManager
from .spill import preview, save_text

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件，按行分段。大文件请用 offset/limit 逐段读，不要一次读完整份。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径；相对路径相对于存档根目录"},
                    "offset": {"type": "integer", "description": "起始行号，从 0 开始", "default": 0},
                    "limit": {"type": "integer", "description": "最多返回多少行，默认 200", "default": 200},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容（名称、类型、大小）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认存档根目录", "default": "."},
                    "limit": {"type": "integer", "description": "最多返回多少项，默认 200", "default": 200},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn",
            "description": "启动一个异步进程，立即返回 job_id。输出写到任务目录，用 job_output / wait_job 取结果，用 job_kill 终止。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "cwd": {"type": "string", "description": "工作目录，默认存档根目录"},
                    "label": {"type": "string", "description": "给这个任务起个短标签，便于识别"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时后自动终止；0 表示不限",
                        "default": 0,
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_list",
            "description": "列出所有异步任务的状态。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_output",
            "description": "按字节偏移读取异步任务的输出。一次最多返回 32KB，用 next_offset 继续读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "offset": {"type": "integer", "default": 0},
                    "max_bytes": {"type": "integer", "default": JOB_OUTPUT_CHUNK},
                    "stream": {"type": "string", "enum": ["stdout", "stderr"], "default": "stdout"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_kill",
            "description": "终止异步任务（连同它的子进程）。",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_job",
            "description": "有界等待异步任务结束（最多 timeout_seconds 秒），返回任务状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "timeout_seconds": {"type": "number", "default": 30},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sleep",
            "description": "等待指定秒数。等待期间用户仍可暂停/停止，被停止时提前返回。",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory",
            "description": "写入长期记忆（Memory/LongTermMemory.txt）。append 追加一段，replace 整份替换。用于跨压缩也要保留的信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["append", "replace"], "default": "append"},
                    "content": {"type": "string"},
                },
                "required": ["content"],
            },
        },
    },
]


@dataclass
class ToolResult:
    ok: bool
    payload: dict[str, Any]
    job_id: str | None = None
    duration_ms: int = 0
    name: str = ""
    call_id: str = ""

    @property
    def text(self) -> str:
        rendered = json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= MAX_INLINE_RESULT_CHARS:
            return rendered
        # 兜底：极端大的结果也走溢出
        return rendered[:MAX_INLINE_RESULT_CHARS] + "...[结果过长已截断]"


class ToolBox:
    def __init__(self, archive: Archive, jobs: JobManager, control: Control):
        self.archive = archive
        self.jobs = jobs
        self.control = control

    # ---- 元信息 ----
    def specs(self) -> list[dict[str, Any]]:
        return TOOL_SPECS

    # ---- 分发 ----
    def dispatch(self, name: str, arguments: str | dict[str, Any], *, call_id: str = "") -> ToolResult:
        started = time.monotonic()
        try:
            args = self._parse_arguments(arguments)
            handler: Callable[[dict[str, Any]], dict[str, Any]] = getattr(self, f"_tool_{name}", None)  # type: ignore[assignment]
            if handler is None:
                raise _ToolError("unknown_tool", f"没有名为 {name} 的工具")
            payload = handler(args)
            result = ToolResult(ok=True, payload=payload, name=name, call_id=call_id)
            if isinstance(payload.get("job_id"), str):
                result.job_id = payload["job_id"]
        except _ToolError as exc:
            result = ToolResult(
                ok=False,
                payload={"ok": False, "error": {"code": exc.code, "message": exc.message}},
                name=name,
                call_id=call_id,
            )
        except Exception as exc:  # noqa: BLE001 - 工具错误必须成为模型可读的返回值
            result = ToolResult(
                ok=False,
                payload={"ok": False, "error": {"code": "tool_crash", "message": f"{type(exc).__name__}: {exc}"}},
                name=name,
                call_id=call_id,
            )
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    @staticmethod
    def _parse_arguments(arguments: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        if not arguments or not arguments.strip():
            return {}
        try:
            parsed = json.loads(arguments)
        except ValueError as exc:
            raise _ToolError("invalid_arguments", f"参数不是合法 JSON：{exc}") from None
        if not isinstance(parsed, dict):
            raise _ToolError("invalid_arguments", "参数必须是 JSON 对象")
        return parsed

    # ---- 路径 ----
    def _resolve(self, raw: str | None, *, default: str = ".") -> Path:
        value = (raw or default).strip()
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.archive.root / path
        return path.resolve()

    def _spill_payload(self, text: str, name: str) -> dict[str, Any]:
        info = save_text(self.archive.spill_dir, text, name)
        body, truncated = preview(text)
        return {"data": body, "truncated": True, "spill": info}

    # ---- 同步工具 ----
    def _tool_read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(args.get("path"))
        offset = _as_int(args.get("offset"), 0, minimum=0)
        limit = _as_int(args.get("limit"), 200, minimum=1, maximum=2000)
        if not path.exists():
            raise _ToolError("not_found", f"文件不存在：{path}")
        if path.is_dir():
            raise _ToolError("is_dir", f"{path} 是目录，请用 list_dir")
        try:
            with open(path, "rb") as handle:
                head = handle.read(8192)
        except OSError as exc:
            raise _ToolError("io_error", f"读取失败：{exc}") from None
        if b"\x00" in head:
            raise _ToolError("binary_file", f"{path} 看起来是二进制文件")

        lines: list[str] = []
        bytes_used = 0
        truncated_by_bytes = False
        line_truncated = False
        has_more = False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for _ in range(offset):
                    if handle.readline() == "":
                        break
                for _ in range(limit):
                    line = handle.readline()
                    if line == "":
                        break
                    encoded = len(line.encode("utf-8"))
                    if bytes_used + encoded > MAX_READ_BYTES:
                        if not lines:
                            # 单行就超过字节上限：给出截断预览，避免死循环
                            lines.append(line[: MAX_READ_BYTES // 4])
                            line_truncated = True
                            has_more = True
                        else:
                            truncated_by_bytes = True
                            has_more = True
                        break
                    bytes_used += encoded
                    lines.append(line.rstrip("\n"))
                else:
                    if handle.readline() != "":
                        has_more = True
        except OSError as exc:
            raise _ToolError("io_error", f"读取失败：{exc}") from None

        text = "\n".join(lines)
        next_offset = offset + len(lines)
        payload: dict[str, Any] = {
            "path": str(path),
            "offset": offset,
            "returned_lines": len(lines),
            "next_offset": next_offset,
            "has_more": has_more,
            "data": text,
        }
        if truncated_by_bytes:
            payload["note"] = f"本次返回达到 {MAX_READ_BYTES} 字节上限，请用 offset={next_offset} 继续读"
        if line_truncated:
            payload["note"] = "有单行超过字节上限，已截断；建议用 spawn 配合 head/awk 等命令处理"
        return payload

    def _tool_list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(args.get("path"))
        limit = _as_int(args.get("limit"), 200, minimum=1, maximum=MAX_LIST_ENTRIES)
        if not path.exists():
            raise _ToolError("not_found", f"目录不存在：{path}")
        if not path.is_dir():
            raise _ToolError("not_dir", f"{path} 不是目录")
        entries: list[dict[str, Any]] = []
        try:
            for entry in sorted(path.iterdir(), key=lambda item: item.name):
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "bytes": stat.st_size if entry.is_file() else None,
                    }
                )
        except OSError as exc:
            raise _ToolError("io_error", f"列目录失败：{exc}") from None
        shown = entries[:limit]
        return {
            "path": str(path),
            "total": len(entries),
            "entries": shown,
            "truncated": len(entries) > len(shown),
        }

    # ---- 异步任务 ----
    def _tool_spawn(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command") or "")
        cwd = args.get("cwd")
        label = args.get("label")
        timeout = _as_int(args.get("timeout_seconds"), 0, minimum=0)
        job = self.jobs.start(command, cwd=cwd, label=label, timeout_seconds=timeout)
        return {
            "job_id": job.id,
            "command": job.command,
            "cwd": job.cwd,
            "status": job.status,
            "hint": "用 job_output(job_id) 取输出，wait_job(job_id) 等它结束，job_kill(job_id) 终止。",
        }

    def _tool_job_list(self, args: dict[str, Any]) -> dict[str, Any]:
        jobs = [job.to_dict() for job in self.jobs.list()]
        return {"jobs": jobs, "count": len(jobs)}

    def _tool_job_output(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id") or "")
        offset = _as_int(args.get("offset"), 0, minimum=0)
        max_bytes = _as_int(args.get("max_bytes"), JOB_OUTPUT_CHUNK, minimum=1, maximum=MAX_READ_BYTES)
        stream = str(args.get("stream") or "stdout")
        if stream not in {"stdout", "stderr"}:
            raise _ToolError("invalid_arguments", "stream 只能是 stdout 或 stderr")
        try:
            return self.jobs.output(job_id, offset=offset, max_bytes=max_bytes, stream=stream)
        except FileNotFoundError:
            raise _ToolError("not_found", f"任务不存在：{job_id}") from None
        except ValueError as exc:
            raise _ToolError("invalid_job", str(exc)) from None

    def _tool_job_kill(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id") or "")
        try:
            job = self.jobs.kill(job_id)
        except FileNotFoundError:
            raise _ToolError("not_found", f"任务不存在：{job_id}") from None
        return {"job": job.to_dict()}

    def _tool_wait_job(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id") or "")
        timeout = float(_as_int(args.get("timeout_seconds"), 30, minimum=0, maximum=MAX_SLEEP_SECONDS))
        deadline = time.monotonic() + timeout
        try:
            while True:
                job = self.jobs.load(job_id)
                if job.status not in {"running", "stopping"}:
                    return {"job": job.to_dict(), "waited": True}
                state = self.control.read_state()
                if state["run"] == "stop":
                    return {"job": job.to_dict(), "waited": False, "interrupted": "stop"}
                if state["run"] == "pause":
                    return {"job": job.to_dict(), "waited": False, "interrupted": "pause"}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"job": job.to_dict(), "waited": False, "timeout": True}
                self.jobs.wait(job_id, min(0.2, remaining))
        except FileNotFoundError:
            raise _ToolError("not_found", f"任务不存在：{job_id}") from None

    # ---- 延时 ----
    def _tool_sleep(self, args: dict[str, Any]) -> dict[str, Any]:
        seconds = _as_float(args.get("seconds"), 0.0, minimum=0.0, maximum=MAX_SLEEP_SECONDS)
        slept, reason = self.control.interruptible_sleep(seconds)
        return {"requested_seconds": seconds, "slept_seconds": round(slept, 2), "interrupted": reason}

    # ---- 记忆 ----
    def _tool_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        mode = str(args.get("mode") or "append")
        content = str(args.get("content") or "")
        if mode not in {"append", "replace"}:
            raise _ToolError("invalid_arguments", "mode 只能是 append 或 replace")
        if not content.strip():
            raise _ToolError("invalid_arguments", "content 不能为空")
        current = self.archive.read_text(self.archive.long_term_memory_path)
        updated = content if mode == "replace" else (current.rstrip() + "\n\n" + content.strip() + "\n")
        encoded = len(updated.encode("utf-8"))
        if encoded > MAX_MEMORY_BYTES:
            raise _ToolError(
                "memory_too_large",
                f"长期记忆会达到 {encoded} 字节，超过上限 {MAX_MEMORY_BYTES}；请先精简或替换整份内容",
            )
        self.archive.write_long_term_memory(updated)
        return {"mode": mode, "bytes": encoded}


class _ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _as_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if value is None:
        result = default
    elif isinstance(value, bool):
        result = int(value)
    elif isinstance(value, (int, float)):
        result = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            result = int(float(value.strip()))
        except ValueError:
            raise _ToolError("invalid_arguments", f"需要整数，收到 {value!r}") from None
    else:
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_float(value: Any, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if value is None:
        result = default
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            result = float(value.strip())
        except ValueError:
            raise _ToolError("invalid_arguments", f"需要数字，收到 {value!r}") from None
    else:
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result
