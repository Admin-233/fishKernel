"""异步进程任务：id、输出文件、状态、等待与终止。

设计要点：
- stdout/stderr 直接写文件，不经过管道（否则管道写满会卡死子进程）。
- 任务元数据原子写；内核重启后按 pid 接管，不会留下"幽灵任务"。
- 杀进程用进程组（POSIX），保证 shell 派生的孙进程一起结束。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .archive import Archive
from .atomic import write_atomic
from .constants import JOBS_KEEP_DIRS


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


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
        )


class JobManager:
    def __init__(self, archive: Archive):
        self.archive = archive
        self._procs: dict[str, subprocess.Popen] = {}

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
        popen_kwargs: dict = {
            "shell": True,
            "cwd": str(workdir),
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with open(out_path, "wb") as out_handle, open(err_path, "wb") as err_handle:
            process = subprocess.Popen(command, stdout=out_handle, stderr=err_handle, **popen_kwargs)
        job.pid = process.pid
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
        return Job.from_dict(raw)

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
            if job.pid and self._alive(job.pid):
                job.adopted = True
                job.status = "running"
                self._write_meta(job)
                continue
            job.adopted = True
            job.status = "failed"
            job.exit_code = None
            job.ended_at = _now()
            self._write_meta(job)
            notices.append(job)
        self._cleanup_old_jobs()
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
            if process is None and job.pid and not self._alive(job.pid):
                self._settle(job, "failed", None)
                settled.append(job)
                continue
            if exit_code is not None:
                status = "completed" if exit_code == 0 else "failed"
                self._settle(job, status, exit_code)
                settled.append(job)
                continue
            if job.timeout_seconds > 0:
                started = time.mktime(time.strptime(job.started_at, "%Y-%m-%dT%H:%M:%S%z"))
                if now - started > job.timeout_seconds:
                    self._terminate(job)
                    self._settle(job, "timeout", None)
                    settled.append(job)
        return settled

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
                    status = "completed" if exit_code == 0 else "failed"
                    self._settle(job, status, exit_code)
                    return self.load(job_id)
            else:
                time.sleep(0.1)
                if job.pid and not self._alive(job.pid):
                    self._settle(job, "failed", None)
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

    def _terminate(self, job: Job) -> None:
        process = self._procs.get(job.id)
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
        if not job.pid:
            return
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(job.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(job.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        else:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(job.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                pass

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            return True
        return True

    def _cleanup_old_jobs(self) -> None:
        if not self.archive.jobs_dir.is_dir():
            return
        entries = sorted(
            (path for path in self.archive.jobs_dir.iterdir() if path.is_dir()),
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
