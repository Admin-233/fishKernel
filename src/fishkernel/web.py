"""网页控制台：零依赖（stdlib http.server），只读事件日志、只写控制文件。

设计边界（与 TUI 一致）：
- 不 import loop / Kernel，不碰 Runtime/kernel.lock —— 控制台可以在内核运行时随便开随便关。
- 读：events.jsonl（增量）、kernel.json、control/state.json、jobs/<id>/meta.json。
- 写：control/state.json、inbox.jsonl、wake、restart —— 也就是 `ctl` 和 `say` 能做的全部事情。
- 默认只监听 127.0.0.1：控制台等于完全权限，不该默认暴露到局域网。

因为「投影」是纯函数，所以完全可以单测；HTTP 层只做参数校验与 JSON 序列化。
"""

from __future__ import annotations

import bisect
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .archive import Archive, load_kernel_state
from .control import Control
from .events import Event, read_events
from .jobs import JobManager
from .tokens import UsageTotals, fold_usage_into
from .web_page import PAGE

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_BODY_BYTES = 64 * 1024
SNAPSHOT_EVENT_CAP = 400
JOB_OUTPUT_CHUNK = 32_768


# ---------------------------------------------------------------- 投影（纯函数）


def _shorten(text: str, limit: int = 2000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 200] + f"\n…（省略 {len(text) - limit + 200} 字，完整内容见 events.jsonl）"


def project_event(event: Event) -> dict[str, Any] | None:
    """把一条事件投影成前端要的一行；不可显示的事件返回 None。"""
    data = event.data
    kind = event.type

    if kind == "user/message":
        return _line(event, "user", "▸ " + str(data.get("text", "")))
    if kind == "system/message":
        return _line(event, "system", "· " + _shorten(str(data.get("text", ""))))
    if kind == "assistant/message":
        text = str(data.get("text") or "")
        reasoning = data.get("reasoning")
        calls = data.get("tool_calls") or []
        parts: list[str] = []
        if text.strip():
            parts.append(text)
        if calls:
            parts.append("[调用] " + ", ".join(str(call.get("name")) for call in calls))
        if not parts and reasoning:
            parts.append("[思考] " + _shorten(str(reasoning), 400))
        return _line(event, "assistant", "\n".join(parts) if parts else "(空回复)")
    if kind == "tool/result":
        ok = bool(data.get("ok"))
        body = data.get("output")
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        head = f"[{data.get('name')}] {'ok' if ok else 'fail'} · {data.get('duration_ms')}ms"
        return _line(event, "tool" if ok else "fail", head + "\n" + _shorten(text, 1200))
    if kind in {"job/start", "job/settle"}:
        return _line(event, "system", f"[任务] {data.get('job_id')} {data.get('status', 'started')}")
    if kind == "delay":
        seconds = data.get("seconds")
        return _line(event, "system", f"[等待] {seconds}s（{data.get('reason')}）")
    if kind == "compaction/summary":
        return _line(
            event,
            "compact",
            f"[压缩] 到 seq {data.get('to_seq')}，释放约 {data.get('freed_tokens')} tokens",
        )
    if kind == "compaction/abort":
        return _line(event, "compact", f"[压缩中止] {data.get('reason')}")
    if kind == "session/handover":
        return _line(event, "compact", f"[交班] 新进程 {data.get('target')}")
    if kind in {"session/start", "session/end"}:
        return _line(event, "compact", f"[会话] {kind.split('/')[1]} {data.get('reason', '')}".strip())
    if kind == "error":
        return _line(event, "fail", f"[错误] {data.get('stage')}: {data.get('message')}")
    return None


def _line(event: Event, cls: str, text: str) -> dict[str, Any]:
    return {"seq": event.seq, "ts": event.ts, "cls": cls, "text": text}


class UsageCache:
    """增量折叠 usage：只处理上次之后的新事件。

    网页控制台每秒轮询，若每次都 `fold_usage(全部事件)`，日志涨到十万条后
    每次轮询都要跑十万次字典查找，页面会越来越卡。这里记住「已折叠到哪个
    seq」，只折叠新增部分。事件日志只追加，所以这个缓存天然成立；
    seq 回退（换存档、日志被截断）时自动重置。
    """

    def __init__(self) -> None:
        self.totals = UsageTotals()
        self.seq = 0

    def update(self, events: list[Event]) -> UsageTotals:
        if events and events[-1].seq < self.seq:
            self.totals = UsageTotals()
            self.seq = 0
        fresh = events
        if self.seq:
            fresh = [event for event in events if event.seq > self.seq]
        if fresh:
            fold_usage_into(self.totals, fresh)
            self.seq = fresh[-1].seq
        return self.totals


def _seqs(events: list[Event]) -> list[int]:
    """取出 seq 列表（二分用）。事件数量级是几万，这点开销远小于全表扫描。

    注意：不能缓存这个列表，因为 events 本身就是每次重新读的。
    """
    return [event.seq for event in events]


def build_snapshot(
    archive: Archive,
    *,
    after_seq: int = 0,
    tail: int = 0,
    job_limit: int = 20,
    usage: UsageTotals | None = None,
) -> dict[str, Any]:
    """一次快照：状态 + 增量事件 + 任务列表。

    `usage` 传入预先算好的累计用量（HTTP 层用缓存传，测试可直接省掉）。
    """
    events = read_events(archive.events_path)
    last_seq = events[-1].seq if events else 0

    if tail > 0:
        selected = events[-tail:]
    elif after_seq <= 0:
        selected = events[-SNAPSHOT_EVENT_CAP:]
    else:
        # 事件按 seq 递增，用二分定位起点，别为了取最后几十条去遍历 20 万条。
        # 前端总是带着 after=<上次 last_seq> 来，所以这里热路径应当是 O(log n)。
        start = bisect.bisect_right(_seqs(events), after_seq)
        selected = events[start:]
        if len(selected) > SNAPSHOT_EVENT_CAP:
            selected = selected[-SNAPSHOT_EVENT_CAP:]

    lines = [line for line in (project_event(event) for event in selected) if line is not None]

    config = _read_instance(archive)
    control = Control(archive).read_state()
    if usage is None:
        usage = UsageTotals()
        fold_usage_into(usage, events)
    totals = usage

    # JobManager.list() 按目录名（proc-<hex>）排序，那是随机的；
    # 界面上要看的是「最近在做什么」，所以按 started_at 排序后再取尾部。
    manager = JobManager(archive)
    all_jobs = sorted(manager.list(), key=lambda job: (job.started_at, job.id))
    jobs = []
    for job in all_jobs[-job_limit:]:
        item = job.to_dict()
        try:
            item["stdout_size"] = manager.stdout_path(job.id).stat().st_size
        except OSError:
            item["stdout_size"] = 0
        jobs.append(item)

    return {
        "ok": True,
        "state": {
            "name": config.get("name") or archive.root.name,
            "root": str(archive.root),
            "kernel": load_kernel_state(archive),
            "control": control,
            "events": len(events),
            "last_seq": last_seq,
            "usage": {
                "calls": totals.calls,
                "prompt_tokens": totals.prompt_tokens,
                "output_tokens": totals.output_tokens,
                "cache_hit_rate": round(totals.cache_hit_rate, 4),
                "cost_yuan": round(totals.cost_yuan, 2),
            },
        },
        "events": lines,
        "last_seq": last_seq,
        "jobs": jobs,
    }


def _read_instance(archive: Archive) -> dict[str, str]:
    """只取需要的字段；解析失败就当没有（控制台不该因为配置写坏而 500）。"""
    try:
        from .config import parse_instance

        return parse_instance(archive.read_text(archive.config_path))
    except Exception:
        return {}


# ---------------------------------------------------------------- HTTP 层


@dataclass
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None


class WebState:
    """请求间共享的只读上下文。"""

    def __init__(self, archive: Archive, token: str | None = None):
        self.archive = archive
        self.token = token
        self.lock = threading.Lock()
        self.usage = UsageCache()


def make_handler(state: WebState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fishkernel-web"
        protocol_version = "HTTP/1.1"

        # ---- 工具 ----
        def log_message(self, fmt: str, *args: Any) -> None:  # 静音，避免污染内核日志
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _deny(self) -> bool:
            """控制台拥有全部权限，可选 token 挡一下同机的其它用户。"""
            if state.token is None:
                return False
            given = self.headers.get("X-Fish-Token") or parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if given == state.token:
                return False
            self._json(403, {"ok": False, "error": "token 不匹配"})
            return True

        def _read_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from None
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return payload

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/snapshot":
                if self._deny():
                    return
                query = parse_qs(parsed.query)
                after = _as_int(query.get("after", ["0"])[0], 0)
                tail = _as_int(query.get("tail", ["0"])[0], 0)
                with state.lock:
                    snapshot = build_snapshot(
                        state.archive, after_seq=after, tail=tail, usage=state.usage.update(
                            read_events(state.archive.events_path)
                        )
                    )
                self._json(200, snapshot)
                return
            if parsed.path == "/api/job_output":
                if self._deny():
                    return
                query = parse_qs(parsed.query)
                job_id = (query.get("job_id") or [""])[0]
                if not job_id:
                    self._json(400, {"ok": False, "error": "缺少 job_id"})
                    return
                offset = _as_int((query.get("offset") or ["0"])[0], 0)
                stream = (query.get("stream") or ["stdout"])[0]
                stream = stream if stream in {"stdout", "stderr"} else "stdout"
                try:
                    data = JobManager(state.archive).output(
                        job_id, offset=offset, max_bytes=JOB_OUTPUT_CHUNK, stream=stream
                    )
                except (FileNotFoundError, ValueError) as exc:
                    self._json(404, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, **data})
                return
            self._json(404, {"ok": False, "error": "没有这个路径"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if self._deny():
                return
            try:
                payload = self._read_body()
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return

            control = Control(state.archive)
            if parsed.path == "/api/say":
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    self._json(400, {"ok": False, "error": "消息不能为空"})
                    return
                with state.lock:
                    control.push_message(text.strip())
                self._json(200, {"ok": True})
                return

            if parsed.path == "/api/ctl":
                action = payload.get("action")
                if action == "pause":
                    result = control.write_state(run="pause")
                elif action == "resume":
                    result = control.write_state(run="run")
                elif action == "stop":
                    result = control.write_state(run="stop")
                elif action == "wake":
                    control.request_wake()
                    result = {"run": control.read_state()["run"], "delay": control.read_state()["delay"]}
                elif action == "restart":
                    control.request_restart("web")
                    result = {"run": control.read_state()["run"], "delay": control.read_state()["delay"]}
                elif action == "delay":
                    value = payload.get("value")
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                        self._json(400, {"ok": False, "error": "delay 需要一个非负数"})
                        return
                    result = control.write_state(delay=float(value))
                else:
                    self._json(400, {"ok": False, "error": f"未知动作：{action!r}"})
                    return
                self._json(200, {"ok": True, "state": result})
                return

            self._json(404, {"ok": False, "error": "没有这个路径"})

    return Handler


def _as_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def serve(directory: str | Path, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str | None = None) -> None:
    archive = Archive(Path(directory).expanduser().resolve())
    archive.validate()
    archive.ensure_runtime()

    state = WebState(archive, token=token or None)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    print(f"网页控制台：http://{host}:{httpd.server_address[1]}/  存档：{archive.root}")
    print("Ctrl-C 退出。控制台只读写存档内的文件，关掉不影响内核运行。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
