from __future__ import annotations

import json
import time

from fishkernel.jobs import JobManager


def test_spawn_echo_and_read_output(archive):
    manager = JobManager(archive)
    job = manager.start("echo hello-fishkernel", label="echo")
    assert job.id.startswith("proc-")
    done = manager.wait(job.id, timeout_seconds=10)
    assert done.status == "completed"
    assert done.exit_code == 0
    output = manager.output(job.id)
    assert "hello-fishkernel" in output["data"]
    assert output["eof"] is True


def test_failed_command(archive):
    manager = JobManager(archive)
    job = manager.start("exit 7")
    done = manager.wait(job.id, timeout_seconds=10)
    assert done.status == "failed"
    assert done.exit_code == 7


def test_kill(archive):
    manager = JobManager(archive)
    job = manager.start("sleep 30")
    time.sleep(0.2)
    killed = manager.kill(job.id)
    assert killed.status == "killed"
    assert manager.load(job.id).status == "killed"


def test_timeout(archive):
    manager = JobManager(archive)
    job = manager.start("sleep 5", timeout_seconds=0.3)
    time.sleep(0.6)
    settled = manager.tick()
    assert any(item.id == job.id for item in settled)
    assert manager.load(job.id).status == "timeout"


def test_orphan_recovery(archive):
    job_dir = archive.jobs_dir / "proc-00000000beef"
    job_dir.mkdir(parents=True)
    (job_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": "proc-00000000beef",
                "command": "sleep 1",
                "status": "running",
                "pid": 999_999_999,
                "started_at": "2026-01-01T00:00:00+0800",
            }
        ),
        encoding="utf-8",
    )
    manager = JobManager(archive)
    notices = manager.recover()
    assert len(notices) == 1
    assert manager.load("proc-00000000beef").status == "failed"


def test_adopt_live_process(archive):
    first = JobManager(archive)
    job = first.start("sleep 3")
    time.sleep(0.2)

    second = JobManager(archive)
    notices = second.recover()
    adopted = second.load(job.id)
    assert adopted.status == "running"
    assert adopted.adopted is True
    assert notices == []
    second.kill(job.id)
    assert second.load(job.id).status == "killed"


def test_output_offsets(archive):
    manager = JobManager(archive)
    job = manager.start("printf 'abcdefghij'")
    manager.wait(job.id, 10)
    first = manager.output(job.id, offset=0, max_bytes=4)
    assert first["data"] == "abcd"
    assert first["next_offset"] == 4
    second = manager.output(job.id, offset=4, max_bytes=100)
    assert second["data"] == "efghij"
    assert second["eof"] is True


def test_invalid_job_id_rejected(archive):
    manager = JobManager(archive)
    try:
        manager.output("../etc/passwd")
    except ValueError as exc:
        assert "非法任务 id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当拒绝路径穿越的 job id")


# ---- 超时判定与时区 ----------------------------------------------------------
# 事故：tick() 里用 time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S%z")) 算任务
# 起始时刻。%z 只让 strptime 认出偏移，mktime 却一律按本机时区解释，
# 时区信息被静默丢掉。本机是 +0800 时恰好正确，所以测试全绿；
# 一旦跑在 UTC 的机器上，每个任务都被算成 8 小时前 → 立刻误杀。


def _with_tz(tz: str, fn):
    """在指定时区下执行 fn（mktime 依赖进程时区，必须真的切）。"""
    import os
    import time as _time

    original = os.environ.get("TZ")
    os.environ["TZ"] = tz
    _time.tzset()
    try:
        return fn()
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        _time.tzset()


def test_started_timestamp_is_timezone_independent():
    from fishkernel.jobs import _started_timestamp

    # 同一时刻的两种写法，在任意进程时区下都必须解析成同一个时间戳
    expected = 1793073600.0  # 2026-10-27T12:00:00+0800 == 04:00:00Z
    for tz in ("Asia/Shanghai", "UTC", "America/New_York"):
        for stamp in ("2026-10-27T12:00:00+0800", "2026-10-27T04:00:00+0000"):
            got = _with_tz(tz, lambda: _started_timestamp(stamp))
            assert got == expected, f"TZ={tz} {stamp} -> {got}，应为 {expected}"


def test_started_timestamp_returns_none_on_garbage():
    from fishkernel.jobs import _started_timestamp

    assert _started_timestamp("") is None
    assert _started_timestamp("不是时间") is None


def test_timeout_fires_in_any_timezone(archive):
    """一个「刚刚开始、超时 60 秒」的任务，在 UTC 下也不能被误判成超时。"""
    import json as _json
    import time as _time

    from fishkernel.jobs import JobManager

    manager = JobManager(archive)
    job_id = "proc-abcdef123456"
    job_dir = manager.job_dir(job_id)
    job_dir.mkdir(parents=True)

    def build() -> None:
        meta = {
            "id": job_id, "command": "sleep 300", "cwd": str(archive.root),
            "label": "长任务", "status": "running", "pid": None, "exit_code": None,
            "started_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ended_at": None, "timeout_seconds": 60.0, "adopted": False,
        }
        (job_dir / "meta.json").write_text(_json.dumps(meta), encoding="utf-8")

    for tz in ("Asia/Shanghai", "UTC", "America/New_York"):
        build()
        settled = _with_tz(tz, lambda: JobManager(archive).tick())
        assert settled == [], f"TZ={tz} 把刚开始的任务误判为超时"


def test_timeout_still_fires_when_actually_expired(archive):
    """真的超时了必须照杀，别把 bug 修成「永不超时」。"""
    import json as _json
    import time as _time

    from fishkernel.jobs import JobManager

    manager = JobManager(archive)
    job_id = "proc-expired0001"
    job_dir = manager.job_dir(job_id)
    job_dir.mkdir(parents=True)
    meta = {
        "id": job_id, "command": "sleep 300", "cwd": str(archive.root),
        "label": "早就该超时", "status": "running", "pid": None, "exit_code": None,
        "started_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z", _time.localtime(_time.time() - 3600)),
        "ended_at": None, "timeout_seconds": 60.0, "adopted": False,
    }
    (job_dir / "meta.json").write_text(_json.dumps(meta), encoding="utf-8")

    for tz in ("Asia/Shanghai", "UTC"):
        (job_dir / "meta.json").write_text(_json.dumps(meta), encoding="utf-8")
        settled = _with_tz(tz, lambda: JobManager(archive).tick())
        assert [job.status for job in settled] == ["timeout"], f"TZ={tz} 没有触发超时"
        meta["status"] = "running"  # 复位再测下一个时区
