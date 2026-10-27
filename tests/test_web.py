"""网页控制台测试：投影纯函数 + 真实 HTTP 往返（起在随机端口上）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from fishkernel.archive import save_kernel_state
from fishkernel.control import Control
from fishkernel.events import Event
from fishkernel.web import WebState, build_snapshot, make_handler, project_event


def _ev(seq: int, etype: str, **data) -> Event:
    return Event(seq=seq, ts=f"2026-10-27T12:00:{seq:02d}.000+08:00", type=etype, data=data)


# ---------------------------------------------------------------- 投影


def test_project_user_and_system_messages():
    user = project_event(_ev(1, "user/message", text="你好"))
    assert user is not None and user["cls"] == "user" and "你好" in user["text"]

    system = project_event(_ev(2, "system/message", text="任务结束"))
    assert system is not None and system["cls"] == "system"


def test_project_tool_result_marks_failure():
    ok = project_event(_ev(3, "tool/result", name="read_file", ok=True, output='{"ok":true}', duration_ms=3))
    fail = project_event(_ev(4, "tool/result", name="spawn", ok=False, output='{"ok":false}', duration_ms=1))
    assert ok is not None and ok["cls"] == "tool"
    assert fail is not None and fail["cls"] == "fail"


def test_project_tool_result_non_string_output_is_valid():
    line = project_event(_ev(5, "tool/result", name="list_dir", ok=True, output={"ok": True, "total": 3}))
    assert line is not None and '"total": 3' in line["text"]


def test_project_assistant_with_only_reasoning():
    line = project_event(_ev(6, "assistant/message", text="", reasoning="想一想", tool_calls=[]))
    assert line is not None and "[思考]" in line["text"]


def test_project_assistant_with_tool_calls():
    calls = [{"id": "c1", "name": "spawn", "arguments": "{}"}]
    line = project_event(_ev(7, "assistant/message", text="", tool_calls=calls))
    assert line is not None and "[调用] spawn" in line["text"]


def test_project_ignores_noise_events():
    assert project_event(_ev(8, "turn/start", turn=1)) is None
    assert project_event(_ev(9, "turn/end", turn=1)) is None


# ---------------------------------------------------------------- 快照


def test_build_snapshot_shape(archive, log):
    log.append("user/message", text="开始", source="init")
    log.append("assistant/message", text="收到", tool_calls=[], usage={"prompt_tokens": 100, "completion_tokens": 10})
    save_kernel_state(archive, {"pid": 1234, "status": "running", "turn": 3})
    Control(archive).write_state(run="pause")

    snap = build_snapshot(archive)
    assert snap["ok"] is True
    assert snap["state"]["kernel"]["turn"] == 3
    assert snap["state"]["control"]["run"] == "pause"
    assert snap["state"]["usage"]["calls"] == 1
    assert snap["last_seq"] == 2
    assert [line["seq"] for line in snap["events"]] == [1, 2]


def test_build_snapshot_incremental(archive, log):
    for index in range(5):
        log.append("user/message", text=f"第 {index} 条", source="test")
    snap = build_snapshot(archive, after_seq=3)
    assert [line["seq"] for line in snap["events"]] == [4, 5]
    assert snap["last_seq"] == 5


def test_build_snapshot_tail_wins_over_after(archive, log):
    for index in range(10):
        log.append("user/message", text=f"m{index}", source="test")
    snap = build_snapshot(archive, after_seq=0, tail=3)
    assert [line["seq"] for line in snap["events"]] == [8, 9, 10]


def test_build_snapshot_empty_archive(archive):
    snap = build_snapshot(archive)
    assert snap["events"] == []
    assert snap["last_seq"] == 0
    assert snap["jobs"] == []


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def server(archive):
    state = WebState(archive)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_index_serves_page(server):
    status, body = _get(server + "/")
    assert status == 200
    assert "fishKernel" in body and "/api/snapshot" in body


def test_http_snapshot(server, archive, log):
    log.append("user/message", text="喂", source="test")
    status, body = _get(server + "/api/snapshot?after=0")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["state"]["name"] == "测试体"
    assert payload["events"][0]["text"].endswith("喂")


def test_http_say_writes_inbox(server, archive):
    status, payload = _post(server + "/api/say", {"text": "去休息一下"})
    assert status == 200 and payload["ok"] is True
    assert Control(archive).poll_inbox() == ["去休息一下"]


def test_http_say_rejects_empty(server):
    status, payload = _post(server + "/api/say", {"text": "   "})
    assert status == 400 and payload["ok"] is False


@pytest.mark.parametrize("action,expected", [("pause", "pause"), ("resume", "run"), ("stop", "stop")])
def test_http_ctl_run_state(server, archive, action, expected):
    status, payload = _post(server + "/api/ctl", {"action": action})
    assert status == 200 and payload["ok"] is True
    assert Control(archive).read_state()["run"] == expected


def test_http_ctl_delay_and_wake_and_restart(server, archive):
    status, _ = _post(server + "/api/ctl", {"action": "delay", "value": 2.5})
    assert status == 200
    assert Control(archive).read_state()["delay"] == 2.5

    assert _post(server + "/api/ctl", {"action": "wake"})[0] == 200
    assert archive.wake_path.exists()

    assert _post(server + "/api/ctl", {"action": "restart"})[0] == 200
    assert archive.restart_path.exists()


def test_http_ctl_delay_rejects_negative(server):
    status, payload = _post(server + "/api/ctl", {"action": "delay", "value": -1})
    assert status == 400 and payload["ok"] is False


def test_http_ctl_unknown_action(server):
    status, payload = _post(server + "/api/ctl", {"action": "explode"})
    assert status == 400 and payload["ok"] is False


def test_http_unknown_path(server):
    assert _get(server + "/nope")[0] == 404
    assert _post(server + "/nope", {})[0] == 404


def test_http_bad_json_body(server):
    request = urllib.request.Request(
        server + "/api/say", data=b"{not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=5)
        raise AssertionError("应当返回 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_http_job_output(server, archive):
    from fishkernel.jobs import JobManager

    job = JobManager(archive).start("echo hello-web")
    for _ in range(50):
        if JobManager(archive).wait(job.id, timeout_seconds=0.1).status != "running":
            break
    status, body = _get(server + f"/api/job_output?job_id={job.id}")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True and "hello-web" in payload["data"]


def test_http_job_output_missing_job(server):
    status, payload = _get(server + "/api/job_output?job_id=proc-deadbeef0000")
    body = json.loads(payload)
    assert status == 404 and body["ok"] is False


def test_http_token_required(archive):
    state = WebState(archive, token="s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert _get(base + "/api/snapshot")[0] == 403
        assert _get(base + "/api/snapshot?token=s3cret")[0] == 200
        assert _post(base + "/api/say", {"text": "hi"})[0] == 403
        assert _post(base + "/api/say", {"text": "hi"}, {"X-Fish-Token": "s3cret"})[0] == 200
        # 页面本身不设防：没有它连 token 都没地方输
        assert _get(base + "/")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- usage 缓存


def test_usage_cache_matches_full_fold(archive, log):
    from fishkernel.tokens import fold_usage
    from fishkernel.web import UsageCache

    for index in range(6):
        log.append(
            "assistant/message", text="x", tool_calls=[],
            usage={
                "prompt_tokens": 1000 + index,
                "prompt_cache_hit_tokens": 900,
                "prompt_cache_miss_tokens": 100 + index,
                "completion_tokens": 10,
            },
        )

    from fishkernel.events import read_events

    cache = UsageCache()
    events = read_events(archive.events_path)
    # 分三次增量喂进去，结果必须与一次性全量折叠一致
    for chunk in (events[:2], events[2:4], events[4:]):
        totals = cache.update(chunk)
    full = fold_usage(events)
    assert totals.calls == full.calls == 6
    assert totals.prompt_tokens == full.prompt_tokens
    assert totals.cache_hit == full.cache_hit
    assert totals.cache_miss == full.cache_miss
    assert abs(totals.cost_yuan - full.cost_yuan) < 1e-12


def test_usage_cache_ignores_already_folded(archive, log):
    from fishkernel.events import read_events
    from fishkernel.web import UsageCache

    log.append("assistant/message", text="x", tool_calls=[], usage={"prompt_tokens": 500, "completion_tokens": 5})
    cache = UsageCache()
    events = read_events(archive.events_path)
    cache.update(events)
    again = cache.update(events)  # 同一批重复投喂不能重复计数
    assert again.calls == 1 and again.prompt_tokens == 500


def test_usage_cache_resets_on_seq_regression(archive, log):
    from fishkernel.events import Event, clear_parse_cache, read_events
    from fishkernel.web import UsageCache

    cache = UsageCache()
    cache.update([Event(10, "", "assistant/message", {"usage": {"prompt_tokens": 999, "completion_tokens": 1}})])
    assert cache.totals.prompt_tokens == 999

    log.append("assistant/message", text="新存档", tool_calls=[], usage={"prompt_tokens": 7, "completion_tokens": 1})
    clear_parse_cache(archive.events_path)
    totals = cache.update(read_events(archive.events_path))
    assert totals.calls == 1 and totals.prompt_tokens == 7


def test_snapshot_accepts_precomputed_usage(archive, log):
    from fishkernel.tokens import UsageTotals

    log.append("assistant/message", text="x", tool_calls=[], usage={"prompt_tokens": 1234, "completion_tokens": 5})
    preset = UsageTotals(calls=99, prompt_tokens=42)
    snap = build_snapshot(archive, usage=preset)
    assert snap["state"]["usage"]["calls"] == 99


def test_jobs_sorted_by_start_time(archive, log):
    """任务列表必须按开始时间排，而不是按 proc-<hex> 目录名。"""
    import json as _json

    from fishkernel.jobs import JobManager

    manager = JobManager(archive)
    stamps = ["2026-10-27T10:00:00+0800", "2026-10-27T12:00:00+0800", "2026-10-27T11:00:00+0800"]
    for index, stamp in enumerate(stamps):
        job_id = f"proc-{'f' * (10 - index)}0000"  # 目录名顺序与时间顺序故意错开
        job_dir = manager.job_dir(job_id)
        job_dir.mkdir(parents=True)
        meta = {
            "id": job_id, "command": f"echo {index}", "cwd": str(archive.root),
            "label": f"任务{index}", "status": "completed", "pid": None, "exit_code": 0,
            "started_at": stamp, "ended_at": stamp, "timeout_seconds": 0.0, "adopted": False,
        }
        (job_dir / "meta.json").write_text(_json.dumps(meta), encoding="utf-8")
        (job_dir / "stdout.log").write_text("x", encoding="utf-8")

    snap = build_snapshot(archive)
    assert [job["label"] for job in snap["jobs"]] == ["任务0", "任务2", "任务1"]
