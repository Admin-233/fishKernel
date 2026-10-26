"""工具集：同步读取、异步进程、延时、长期记忆。

返回值统一是 JSON 字符串：
  成功 {"ok": true, ...}
  失败 {"ok": false, "error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

import base64
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
from .selfguard import check_pid, check_signal_command
from .spill import preview, save_text

_SPILL_REMAINDER_LIMIT = 16 * 1024 * 1024

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件，按行分段。文本文件请用 offset/limit 逐段读；图片（png/jpeg/gif/webp）会作为图像附上，你可以直接看，不需要 offset/limit。",
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
    spill_dir: Path | None = None
    # 图片附件：read_file 读到图片时填，投影时变成 image 块交给模型看
    images: list[dict[str, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        """永远返回合法 JSON；超长时把字符串字段换成溢出指针，而不是切断 JSON。"""
        rendered = json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= MAX_INLINE_RESULT_CHARS:
            return rendered
        shrunk = _shrink(self.payload, self.spill_dir, f"result-{self.name or 'tool'}")
        rendered = json.dumps(shrunk, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= MAX_INLINE_RESULT_CHARS:
            return rendered
        # 剪枝后仍然过大：整份落盘，只回一个极小但合法的对象
        if self.spill_dir is not None:
            info = save_text(self.spill_dir, rendered, f"result-{self.name or 'tool'}")
            minimal: dict[str, Any] = {"ok": self.ok, "truncated": True, "spill": info}
        else:
            minimal = {"ok": self.ok, "truncated": True, "bytes": len(rendered)}
        return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))


_INLINE_STRING_LIMIT = 8_000
_RESULT_PREVIEW_CHARS = 1_200


def _pointer(text: str, spill_dir: Path | None, name: str) -> dict[str, Any]:
    body, _ = preview(text, limit_chars=_RESULT_PREVIEW_CHARS)
    if spill_dir is None:
        return {"truncated": True, "bytes": len(text.encode("utf-8")), "preview": body}
    info = save_text(spill_dir, text, name)
    return {
        "locator": info["locator"],
        "bytes": info["bytes"],
        "preview": body,
        "hint": info["hint"],
    }


def _shrink(value: Any, spill_dir: Path | None, name: str) -> Any:
    """递归把过长的字符串字段换成溢出指针；结构本身保持合法 JSON。"""
    if isinstance(value, str):
        if len(value) <= _INLINE_STRING_LIMIT:
            return value
        return _pointer(value, spill_dir, name)
    if isinstance(value, list):
        return [_shrink(item, spill_dir, name) for item in value]
    if isinstance(value, dict):
        return {key: _shrink(item, spill_dir, name) for key, item in value.items()}
    return value


# ---- 图片识别 ----
# 只接受模型明确支持的四种格式（API 报错信息里列的就是这四个）
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
MAX_IMAGE_BYTES = 8 * 1024 * 1024   # 单张上限 8MB，超出转文本说明


def _sniff_image(head: bytes) -> str | None:
    """按文件头判断图片类型，不认识就返回 None。"""
    for magic, kind in _IMAGE_MAGIC:
        if head.startswith(magic):
            return kind
    # WebP: RIFF....WEBP
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


class ToolBox:
    def __init__(self, archive: Archive, jobs: JobManager, control: Control, events=None):
        self.archive = archive
        self.jobs = jobs
        self.control = control
        self.events = events

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
            # 注意：handler 返回的 __images__ 会在下方被摘出，见 _finish_result
            payload = handler(args)
            # 图片附件从 payload 里摘出来单独携带：base64 有几 MB，
            # 留在 JSON 文本里会瞬间吃满上下文，而且模型也不需要看 base64。
            images = payload.pop("__images__", None) or []
            # payload 自己带 ok=False 时（如 image_too_large），整体也算失败
            ok_flag = payload.get("ok", True)
            result = ToolResult(
                ok=bool(ok_flag),
                payload=payload,
                name=name,
                call_id=call_id,
                spill_dir=self.archive.spill_dir,
                images=images,
            )
            if isinstance(payload.get("job_id"), str):
                result.job_id = payload["job_id"]
        except _ToolError as exc:
            result = ToolResult(
                ok=False,
                payload={"ok": False, "error": {"code": exc.code, "message": exc.message}},
                name=name,
                call_id=call_id,
                spill_dir=self.archive.spill_dir,
            )
        except Exception as exc:  # noqa: BLE001 - 工具错误必须成为模型可读的返回值
            result = ToolResult(
                ok=False,
                payload={"ok": False, "error": {"code": "tool_crash", "message": f"{type(exc).__name__}: {exc}"}},
                name=name,
                call_id=call_id,
                spill_dir=self.archive.spill_dir,
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

    def _read_image(self, path: Path, kind: str, head: bytes) -> ToolResult:
        """把图片读成 base64，作为 image 块交给模型看。

        返回值里带一个 images 字段，messages 投影时会把它塞进场内容的 image block。
        同时给出文字摘要，方便模型在不支持图片的场合也知道"这里有一张图"。
        """
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            return {
                "ok": False,
                "error": {
                    "code": "image_too_large",
                    "message": f"图片 {size} 字节，超过上限 {MAX_IMAGE_BYTES}，未传给模型",
                },
                "path": str(path),
            }

        data = path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        mime = "jpeg" if kind == "jpeg" else kind
        # 用 data URL，DeepSeek 接受这种形式（实测通过）
        url = f"data:image/{mime};base64,{b64}"
        # 图片本体放在 __images__ 里，dispatch 会把它摘出来放进 ToolResult.images，
        # 不留在给模型看的 JSON 文本里（否则 base64 会污染上下文）。
        return {
            "ok": True,
            "kind": "image",
            "path": str(path),
            "format": kind,
            "size": size,
            "note": "图片已附上，你可以直接看。",
            "__images__": [{"url": url, "path": str(path), "format": kind}],
        }

    def _spill_remainder(self, path: Path, position: int) -> dict[str, Any]:
        """把文件从 position 起的剩余内容写入 spill，返回定位信息。"""
        try:
            total = path.stat().st_size
        except OSError as exc:
            return {"error": str(exc)}
        remaining = max(0, total - position)
        spill_dir = self.archive.spill_dir
        spill_dir.mkdir(parents=True, exist_ok=True)
        dest = spill_dir / f"read-{int(time.time())}-{os.getpid()}-{position}.txt"
        limit = min(remaining, _SPILL_REMAINDER_LIMIT)
        copied = 0
        try:
            with open(path, "rb") as source, open(dest, "wb") as target:
                source.seek(position)
                while copied < limit:
                    chunk = source.read(min(65_536, limit - copied))
                    if not chunk:
                        break
                    target.write(chunk)
                    copied += len(chunk)
        except OSError as exc:
            return {"error": str(exc), "file_bytes": total, "remainder_bytes": remaining}
        hint = f"从第 {position} 字节起的剩余内容已写到 {dest}（{copied} 字节）"
        if copied < remaining:
            hint += f"；源文件还有 {remaining - copied} 字节未复制（单次上限 {_SPILL_REMAINDER_LIMIT} 字节）"
        hint += "；可用 read_file(path=该文件) 或 spawn 继续处理"
        return {
            "locator": f"Runtime/spill/{dest.name}",
            "bytes": copied,
            "file_bytes": total,
            "remainder_bytes": remaining,
            "truncated": copied < remaining,
            "hint": hint,
        }

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
        # 图片不是"读不了的二进制"，而是可以直接交给模型看的内容。
        # deepseek-flash 的 input_modalities 含 image，内核只要把图片块传上去即可。
        image_kind = _sniff_image(head)
        if image_kind is not None:
            return self._read_image(path, image_kind, head)

        if b"\x00" in head:
            raise _ToolError("binary_file", f"{path} 看起来是二进制文件")

        lines: list[str] = []
        bytes_used = 0
        truncated_by_bytes = False
        line_truncated = False
        has_more = False
        spill: dict[str, Any] | None = None
        try:
            with open(path, "rb") as handle:
                for _ in range(offset):
                    if handle.readline() == b"":
                        break
                for _ in range(limit):
                    start_position = handle.tell()
                    raw_line = handle.readline()
                    if raw_line == b"":
                        break
                    if bytes_used + len(raw_line) > MAX_READ_BYTES:
                        if not lines:
                            # 单行就超过字节上限：给出截断预览，避免死循环
                            lines.append(raw_line[: MAX_READ_BYTES // 4].decode("utf-8", errors="replace"))
                            line_truncated = True
                            has_more = True
                        else:
                            truncated_by_bytes = True
                            has_more = True
                        spill = self._spill_remainder(path, start_position)
                        break
                    bytes_used += len(raw_line)
                    lines.append(raw_line.decode("utf-8", errors="replace").rstrip("\n"))
                else:
                    if handle.readline() != b"":
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
        if spill is not None:
            payload["spill"] = spill
        if truncated_by_bytes:
            payload["note"] = f"本次返回达到 {MAX_READ_BYTES} 字节上限，请用 offset={next_offset} 继续读"
        if line_truncated:
            payload["note"] = "有单行超过字节上限，已截断；剩余内容见 spill"
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
        refusal = check_signal_command(command)
        if refusal:
            raise _ToolError("self_guard", refusal)
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
            target = self.jobs.load(job_id)
            refusal = check_pid(target.pid)
            if refusal:
                raise _ToolError("self_guard", refusal)
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
                if self.control.consume_wake():
                    return {"job": job.to_dict(), "waited": False, "interrupted": "wake"}
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
        # KernelDesign 3.2：LongTermMemory.txt 只由内核在压缩事务里写。
        # 这里不碰文件，只把"要记住的东西"登记进事件流；
        # 未触发压缩时，这些内容靠上下文本身被模型看到。
        if self.events is None:
            # 独立使用 ToolBox（无事件流）时退化成立即写盘
            self.archive.write_long_term_memory(updated)
            return {"mode": mode, "bytes": encoded, "deferred": False}
        self.events.append("memory/write", mode=mode, content=content, bytes=encoded)
        return {
            "mode": mode,
            "bytes": encoded,
            "deferred": True,
            "note": "已登记，将在下次上下文压缩时写入 LongTermMemory.txt；在此之前它仍在上下文中可见。",
        }


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
