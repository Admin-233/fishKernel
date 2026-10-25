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
