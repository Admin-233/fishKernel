from __future__ import annotations

import json
import os

from fishkernel.control import Control
from fishkernel.jobs import JobManager
from fishkernel.selfguard import check_pid, check_signal_command
from fishkernel.tools import ToolBox


def make_toolbox(archive) -> ToolBox:
    manager = JobManager(archive)
    return ToolBox(archive, manager, Control(archive))


def test_selfguard_refuses_own_pid():
    assert check_pid(os.getpid()) is not None


def test_selfguard_refuses_parent_pid():
    assert check_pid(os.getppid()) is not None


def test_selfguard_detects_kill_commands():
    assert check_signal_command(f"kill -9 {os.getpid()}") is not None
    assert check_signal_command(f"bash -c 'pkill {os.getpid()}'") is not None
    assert check_signal_command(f"taskkill /F /PID {os.getpid()}") is not None
    assert check_signal_command("echo hello") is None
    assert check_signal_command("kill -9 999999") is None


def test_spawn_refuses_kill_self(archive):
    box = make_toolbox(archive)
    result = box.dispatch("spawn", json.dumps({"command": f"kill -9 {os.getpid()}"}))
    assert not result.ok
    assert result.payload["error"]["code"] == "self_guard"
    assert not any(archive.jobs_dir.iterdir())


def test_job_kill_refuses_self(archive):
    manager = JobManager(archive)
    job = manager.start("sleep 5")
    meta_path = manager.job_dir(job.id) / "meta.json"
    original = json.loads(meta_path.read_text(encoding="utf-8"))
    spoofed = dict(original)
    spoofed["pid"] = os.getpid()
    meta_path.write_text(json.dumps(spoofed), encoding="utf-8")

    box = ToolBox(archive, manager, Control(archive))
    result = box.dispatch("job_kill", json.dumps({"job_id": job.id}))
    assert not result.ok
    assert result.payload["error"]["code"] == "self_guard"

    # 关键：先还原真实 pid，再清理任务；否则清理会打到测试进程自己
    meta_path.write_text(json.dumps(original), encoding="utf-8")
    manager.kill(job.id)
