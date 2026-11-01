"""异步进程任务：id、输出文件、状态、等待与终止。

设计要点：
- 独立任务监督进程捕获 stdout/stderr 并写有界文件，不依赖内核读取线程。
- 任务元数据原子写；重启后验证启动身份再接管，不单凭 PID 操作。
- 杀进程用进程组（POSIX），保证 shell 派生的孙进程一起结束。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
import sys
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .archive import Archive
from .atomic import write_atomic
from .constants import JOBS_KEEP_DIRS

OUTPUT_LIMIT = 16 * 1024 * 1024
from .platform import require_runtime, process_identity, process_alive, terminate_group


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _started_timestamp(stamp: str) -> float | None:
    """把 `started_at` 解析成 Unix 时间戳；无法解析返回 None。

    这里绝不能用 `time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S%z"))`：
    `%z` 只让 strptime 认出偏移，mktime 却一律按**本机时区**解释 wall clock，
    时区信息被静默丢掉。在 +0800 的机器上看不出问题，一放到 UTC 的服务器上，
    每个任务的 started_at 都会被算成 8 小时前（或后），超时要么立刻误杀、
    要么永远不触发。`datetime.strptime` 保留 tzinfo，`.timestamp()` 才是对的。
    """
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError):
        # 老版本写过不带偏移的字符串（"%Y-%m-%dT%H:%M:%S"），按本地时间兜底
        try:
            return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").timestamp()
        except (TypeError, ValueError):
            return None


@dataclass
class Job:
    id: str
    command: str
    cwd: str | None = None
    label: str | None = None
    status: str = "running"  # running | stopping | completed | failed | killed | timeout
    pid: int | None = None
    exit_code: int | None = None
    started_at: str = field(default_factory=_now)
    ended_at: str | None = None
    timeout_seconds: float = 0.0
    adopted: bool = False
    identity: str | None = None
    started_monotonic: float | None = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "cwd": self.cwd,
            "label": self.label,
            "status": self.status,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "timeout_seconds": self.timeout_seconds,
            "adopted": self.adopted,
            "identity": self.identity,
            "started_monotonic": self.started_monotonic,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Job":
        return cls(
            id=str(raw.get("id", "")),
            command=str(raw.get("command", "")),
            cwd=raw.get("cwd"),
            label=raw.get("label"),
            status=str(raw.get("status", "failed")),
            pid=raw.get("pid"),
            exit_code=raw.get("exit_code"),
            started_at=str(raw.get("started_at", "")),
            ended_at=raw.get("ended_at"),
            timeout_seconds=float(raw.get("timeout_seconds") or 0.0),
            adopted=bool(raw.get("adopted")),
            identity=raw.get("identity"),
            started_monotonic=raw.get("started_monotonic"),
        )


class JobManager:
    def __init__(self, archive: Archive):
        self.archive = archive
        self._procs: dict[str, subprocess.Popen] = {}
        self._notices: list[Job] = []
        self._last_cleanup = 0.0

    # ---- 路径 ----
    def job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
            raise ValueError(f"非法任务 id：{job_id!r}")
        return self.archive.jobs_dir / job_id

    def _meta_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "meta.json"

    def stdout_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stdout.log"

    def stderr_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stderr.log"

    # ---- 生命周期 ----
    def start(
        self,
        command: str,
        *,
        cwd: str | None = None,
        label: str | None = None,
        timeout_seconds: float = 0.0,
    ) -> Job:
        require_runtime()
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("timeout must be finite and nonnegative")
        if sum(j.status in {"running", "stopping"} for j in self.list()) >= 32:
            raise ValueError("Maximum 32 active jobs")
        if not command.strip():
            raise ValueError("命令不能为空")
        workdir = Path(cwd).expanduser().resolve() if cwd else self.archive.root
        if not workdir.is_dir():
            raise ValueError(f"工作目录不存在：{workdir}")
        job_id = f"proc-{uuid.uuid4().hex[:12]}"
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        out_path = job_dir / "stdout.log"
        err_path = job_dir / "stderr.log"
        job = Job(
            id=job_id,
            command=command,
            cwd=str(workdir),
            label=label,
            timeout_seconds=max(0.0, float(timeout_seconds or 0.0)),
        )
        with out_path.open("wb") as out, err_path.open("wb") as err:
            process = subprocess.Popen(
                [sys.executable, "-m", "fishkernel.job_runner", str(job_dir / "exit-code"), command],
                cwd=str(workdir), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True,
            )
        job.pid = process.pid
        job.identity = process_identity(process.pid)
        self._procs[job_id] = process
        self._write_meta(job)
        return job

    def load(self, job_id: str) -> Job:
        path = self._meta_path(job_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(f"任务不存在：{job_id}") from None
        except (OSError, ValueError) as exc:
            raise ValueError(f"任务元数据损坏：{job_id}（{exc}）") from None
        job = Job.from_dict(raw)
        if job.status in {"running", "stopping"} and not job.identity and job.id not in self._procs:
            job.status = "historical"
        return job

    def list(self) -> list[Job]:
        if not self.archive.jobs_dir.is_dir():
            return []
        jobs: list[Job] = []
        for entry in sorted(self.archive.jobs_dir.iterdir()):
            if not entry.is_dir() or not (entry / "meta.json").is_file():
                continue
            try:
                jobs.append(self.load(entry.name))
            except (ValueError, FileNotFoundError):
                continue
        return jobs

    def recover(self) -> list[Job]:
        """内核启动时调用：结算已死进程，接管仍活着的，清理过旧目录。"""
        notices: list[Job] = []
        for job in self.list():
            if job.status not in {"running", "stopping"}:
                continue
            if job.pid and process_alive(job.pid, job.identity):
                job.adopted = True
                job.status = "running"
                self._write_meta(job)
                continue
            job.adopted = True
            self._settle_adopted(job)
            notices.append(job)
        self._cleanup_old_jobs()
        self._notices.clear()
        return notices

    def tick(self) -> list[Job]:
        """每轮调用：结算已退出/超时的任务。"""
        settled: list[Job] = []
        now = time.time()
        for job in self.list():
            if job.status not in {"running", "stopping"}:
                continue
            process = self._procs.get(job.id)
            exit_code = process.poll() if process is not None else None
            if process is None and job.pid and not process_alive(job.pid, job.identity):
                self._settle_adopted(job)
                settled.append(job)
                continue
            if exit_code is not None:
                status = "output_limit" if self._output_limited(job) else ("completed" if exit_code == 0 else "failed")
                self._settle(job, status, exit_code)
                settled.append(job)
                continue
            if self._output_limited(job):
                self._terminate(job)
                self._settle(job, "output_limit", None)
                continue
            if job.timeout_seconds > 0:
                started = _started_timestamp(job.started_at)
                # 时间戳解析不出来时不敢猜：宁可让任务继续跑，也不要误杀。
                elapsed = time.monotonic() - job.started_monotonic if job.started_monotonic is not None else (now - started if started is not None else 0)
                if elapsed > job.timeout_seconds:
                    self._terminate(job)
                    self._settle(job, "timeout", None)
                    settled.append(job)
        if time.monotonic() - self._last_cleanup > 60:
            self._cleanup_old_jobs()
            self._last_cleanup = time.monotonic()
        notices, self._notices = self._notices, []
        return notices

    def wait(self, job_id: str, timeout_seconds: float = 30.0) -> Job:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            job = self.load(job_id)
            if job.status not in {"running", "stopping"}:
                return job
            process = self._procs.get(job_id)
            if process is not None:
                try:
                    exit_code = process.wait(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    exit_code = None
                if exit_code is not None:
                    status = "output_limit" if self._output_limited(job) else ("completed" if exit_code == 0 else "failed")
                    self._settle(job, status, exit_code)
                    return self.load(job_id)
            else:
                time.sleep(0.1)
                if job.pid and not process_alive(job.pid, job.identity):
                    self._settle_adopted(job)
                    return self.load(job_id)
            if time.monotonic() >= deadline:
                return self.load(job_id)

    def kill(self, job_id: str, *, reason: str = "killed") -> Job:
        job = self.load(job_id)
        if job.status not in {"running", "stopping"}:
            return job
        job.status = "stopping"
        self._write_meta(job)
        self._terminate(job)
        self._settle(job, reason, None)
        return self.load(job_id)

    def output(self, job_id: str, *, offset: int = 0, max_bytes: int = 32_768, stream: str = "stdout") -> dict:
        job = self.load(job_id)
        path = self.stdout_path(job_id) if stream == "stdout" else self.stderr_path(job_id)
        offset = max(0, int(offset))
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        data = b""
        if offset < size:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(max(1, int(max_bytes)))
        text = data.decode("utf-8", errors="replace")
        next_offset = offset + len(data)
        return {
            "job": job.to_dict(),
            "stream": stream,
            "offset": offset,
            "next_offset": next_offset,
            "size": size,
            "eof": next_offset >= size,
            "data": text,
        }

    # ---- 内部 ----
    def _write_meta(self, job: Job) -> None:
        write_atomic(self._meta_path(job.id), json.dumps(job.to_dict(), ensure_ascii=False, indent=2))

    def _settle(self, job: Job, status: str, exit_code: int | None) -> None:
        job.status = status
        job.exit_code = exit_code
        job.ended_at = _now()
        process = self._procs.pop(job.id, None)
        if process is not None:
            try:
                process.poll()
            except Exception:
                pass
        self._write_meta(job)
        self._notices.append(job)

    def _terminate(self, job: Job) -> None:
        if job.pid and job.identity:
            terminate_group(job.pid, job.identity)
        process = self._procs.get(job.id)
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    @staticmethod
    def _alive(pid: int) -> bool:
        return process_alive(pid)

    def _output_limited(self, job):
        return (self.job_dir(job.id) / "output-limit").exists()

    def _settle_adopted(self, job):
        try:
            code = int((self.job_dir(job.id) / "exit-code").read_text())
        except (OSError, ValueError):
            code = None
        status = "output_limit" if self._output_limited(job) else ("completed" if code == 0 else "failed")
        self._settle(job, status, code)

    def _cleanup_old_jobs(self) -> None:
        if not self.archive.jobs_dir.is_dir():
            return
        from .events import read_events
        referenced = {match for event in read_events(self.archive.events_path)
                      for match in re.findall(r"proc-[a-zA-Z0-9_-]+", json.dumps(event.data))}
        entries = sorted(
            (self.job_dir(job.id) for job in self.list()
             if job.status not in {"running", "stopping", "historical"} and job.id not in referenced),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in entries[JOBS_KEEP_DIRS:]:
            try:
                for child in path.iterdir():
                    child.unlink()
                path.rmdir()
            except OSError:
                pass
