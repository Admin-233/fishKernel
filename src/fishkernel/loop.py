"""主循环（Kernel）。"""

from __future__ import annotations

import json
import os
import signal
import time
import traceback
from pathlib import Path

from .archive import Archive, save_kernel_state
from .compress import Compressor
from .config import InstanceConfig, load_config
from .constants import MAX_TOOL_CALLS_PER_TURN
from .control import Control
from .events import EventLog, read_events
from .jobs import Job, JobManager
from .llm import Completion, ContextOverflowError, LLMClient, OpenAIClient
from .lock import InstanceLock, LockError
from .messages import build_messages
from .restart import spawn_successor
from .spill import cleanup as spill_cleanup
from .tokens import TokenMeter, estimate_messages, estimate_tools
from .tools import ToolBox


class Logger:
    def __init__(self, path: Path, *, echo: bool = True):
        self.path = path
        self.echo = echo
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
        with open(self.path, "a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")
        if self.echo:
            print(line, flush=True)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)


class Kernel:
    def __init__(
        self,
        archive_root: Path | str,
        *,
        client: LLMClient | None = None,
        max_turns: int = 0,
        echo: bool = True,
        error_backoff_base: float = 2.0,
    ):
        self.archive = Archive(Path(archive_root).expanduser().resolve())
        self.archive.validate()
        self.archive.ensure_runtime()
        self.config: InstanceConfig = load_config(self.archive.root)
        self.logger = Logger(self.archive.log_path, echo=echo)
        self.events = EventLog(self.archive.events_path).open()
        self.control = Control(self.archive)
        self.jobs = JobManager(self.archive)
        self.lock = InstanceLock(self.archive.lock_path)
        self.meter = TokenMeter()
        self.toolbox = ToolBox(self.archive, self.jobs, self.control, self.events)
        self.client: LLMClient = client or OpenAIClient(self.config)
        if client is None:
            # 没有 Key 就在启动时报错，而不是每轮失败一次
            self.config.require_api_key()
        self.compressor = Compressor(self.archive, self.config, self.client, self.events, self.logger)
        self.max_turns = max(0, int(max_turns))
        self.error_backoff_base = max(0.0, float(error_backoff_base))
        self.turns = 0
        self._stop = False
        self._old_handlers: dict[int, object] = {}

    # ---- 生命周期 ----
    def run(self) -> int:
        try:
            self.lock.acquire(timeout=5.0)
        except LockError as exc:
            self.logger.error(str(exc))
            raise
        self._install_signals()
        reason = "max_turns"
        self.events.append(
            "session/start",
            model=self.config.model,
            model_name=self.config.spec.name,
            context_window=self.config.spec.context_window,
            max_tokens=self.config.max_tokens,
            pid=os.getpid(),
        )
        history = read_events(self.archive.events_path)
        if not any(event.type == "user/message" for event in history):
            # 首次启动的欢迎语；重启/交班不会重复写
            self.events.append("user/message", text="你开始运行了，欢迎", source="init")
            self.archive.append_history("用户：你开始运行了，欢迎")
        # 上次退出时残留的 stop 是一次性指令，不该让新实例一启动就退出
        if self.control.reset_stop_on_start():
            self.logger.info("检测到上次遗留的 stop 状态，已重置为 run（stop 不跨进程生效）")
        self._write_state("running")
        self.archive.rewrite_history(read_events(self.archive.events_path))
        self.meter.restore(read_events(self.archive.events_path), self.archive.system_text(), self.toolbox.specs())
        spill_cleanup(self.archive.spill_dir)
        self.logger.info(
            f"内核启动：{self.config.name} / {self.config.spec.name}；"
            f"压缩阈值 {self.compressor.threshold}，保留 {self.compressor.retain} tokens；{self.meter.describe()}"
        )

        for job in self.jobs.recover():
            self._report_job(job, note="内核重启时结算：进程已不存在")

        if self.compressor.recover_stale():
            self.logger.warn("修复了上次未完成的压缩事务")

        failures = 0
        try:
            while True:
                if self._stop:
                    reason = "signal"
                    break
                if self.max_turns and self.turns >= self.max_turns:
                    reason = "max_turns"
                    break
                state = self.control.read_state()
                if state["run"] == "stop":
                    reason = "stop"
                    break
                restart_request = self.control.consume_restart()
                if restart_request is not None:
                    if self._handover(restart_request):
                        reason = "restart"
                        break
                    self.logger.warn("交班失败，继续运行")
                if state["run"] == "pause":
                    # 暂停时不消耗 API，也不能空转烧 CPU；0.5s 粒度响应用户指令
                    time.sleep(0.5)
                    continue

                self.turns += 1
                try:
                    self._turn(self.turns, state)
                    failures = 0
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001 - 内核必须活下来
                    failures += 1
                    self.logger.error(traceback.format_exc(limit=8).strip())
                    self.events.append(
                        "error",
                        stage="turn",
                        message=f"{type(exc).__name__}: {exc}"[:1000],
                        retryable=failures < 5,
                        failures=failures,
                    )
                    self._write_state("error")
                    if self.error_backoff_base > 0:
                        backoff = min(60.0, self.error_backoff_base ** min(failures, 6))
                        _, why = self.control.interruptible_sleep(backoff)
                        if why == "stop":
                            reason = "stop"
                            break
        except KeyboardInterrupt:
            reason = "interrupt"
        finally:
            self._write_state("stopped")
            self.events.append("session/end", reason=reason, turns=self.turns)
            self.events.close()
            self.lock.release()
            self._restore_signals()
            self.logger.info(f"内核退出：{reason}（共 {self.turns} 轮）")
        return 0

    def _handover(self, request: dict[str, object] | None = None) -> bool:
        """交班：先启动接班人，再释放锁，然后由调用方正常退出。"""
        try:
            target = spawn_successor(self.archive)
        except Exception as exc:  # noqa: BLE001 - 交班失败不能让旧进程直接死掉
            self.logger.error(f"交班启动失败：{exc}")
            self.events.append("error", stage="handover", message=f"{type(exc).__name__}: {exc}"[:800], retryable=False)
            return False
        reason = (request or {}).get("reason") if isinstance(request, dict) else None
        self.events.append("session/handover", target=target, pid=os.getpid(), reason=reason)
        # 锁在 finally 里、写完 session/end 之后再释放：
        # 让旧进程的最后几条事件排在 session/start 之前，避免两个写者同时追加。
        self.logger.info(f"已启动接班进程 {target}，本进程收尾后释放实例锁")
        return True

    # ---- 单轮 ----
    def _turn(self, turn: int, state: dict[str, object]) -> None:
        self.events.append("turn/start", turn=turn)
        self.logger.info(f"—— 第 {turn} 轮 ——")

        for text in self.control.poll_inbox():
            self.events.append("user/message", text=text, source="control")
            self.archive.append_history(f"用户：{text}")
            self.logger.info(f"用户消息：{text}")

        for job in self.jobs.tick():
            self._report_job(job)

        system_text = self.archive.system_text()
        tools = self.toolbox.specs()
        events = read_events(self.archive.events_path)
        messages = build_messages(events, system_text)
        pressure = self.meter.pressure(events=events, messages=messages, system_text=system_text, tools=tools)
        if self.compressor.should_compact(pressure):
            self.logger.info(f"上下文压力 {pressure} ≥ 阈值 {self.compressor.threshold}，触发压缩（{self.meter.describe()}）")
            outcome = self.compressor.compact(trigger="auto")
            if outcome.compacted:
                self.meter.invalidate()
                events = read_events(self.archive.events_path)
                messages = build_messages(events, system_text)
                pressure = self.meter.pressure(events=events, messages=messages, system_text=system_text, tools=tools)
        self.logger.info(f"上下文压力 {pressure} / 阈值 {self.compressor.threshold}（{self.meter.describe()}）")

        completion, prompt_seq, estimated_prompt = self._call_model(messages, tools)
        self._record_completion(completion, turn, prompt_seq=prompt_seq, pressure=pressure)
        self.meter.record(
            prompt_seq=prompt_seq,
            prompt_tokens=completion.prompt_tokens,
            system_text=system_text,
            tools=tools,
            estimated_prompt=estimated_prompt,
        )
        if prompt_seq >= 0 and completion.prompt_tokens > 0 and estimated_prompt > 0:
            error = (completion.prompt_tokens - estimated_prompt) / estimated_prompt * 100
            self.logger.info(
                f"token：实测 prompt={completion.prompt_tokens}，估算={estimated_prompt}（{error:+.1f}%）；"
                f"{self.meter.describe()}"
            )

        if completion.tool_calls:
            self._run_tool_calls(completion)
        else:
            self.events.append(
                "system/message",
                text=f"第 {turn} 轮没有工具调用。内核会延时后唤醒你；如果确实在等待，请用 sleep 明确等待。",
                kind="wake",
            )

        delay = state.get("delay")
        delay = float(delay) if isinstance(delay, (int, float)) else self.config.turn_delay
        slept, why = self.control.interruptible_sleep(delay)
        if slept > 0:
            self.events.append("delay", seconds=round(slept, 2), reason=why)
        self.events.append("turn/end", turn=turn, reason=why, slept_seconds=round(slept, 2))
        self._write_state("running")

    def _call_model(
        self, messages: list[dict], tools: list[dict]
    ) -> tuple[Completion, int, int]:
        """返回 (完成结果, 本次请求的 prompt_seq, 发出前的估算 token)。"""
        prompt_seq = self.events.last_seq
        estimated = estimate_messages(messages) + estimate_tools(tools)
        try:
            return self.client.complete(messages, tools=tools, max_tokens=self.config.max_tokens), prompt_seq, estimated
        except ContextOverflowError as exc:
            self.logger.warn(f"API 报上下文超限，强制压缩后重试：{exc}")
            outcome = self.compressor.compact(
                trigger="overflow",
                retain_override=min(self.compressor.retain, 4096),
            )
            if not outcome.compacted:
                raise
            self.meter.invalidate()
            refreshed = build_messages(read_events(self.archive.events_path), self.archive.system_text())
            prompt_seq = self.events.last_seq
            estimated = estimate_messages(refreshed) + estimate_tools(tools)
            return self.client.complete(refreshed, tools=tools, max_tokens=self.config.max_tokens), prompt_seq, estimated

    def _record_completion(
        self, completion: Completion, turn: int, *, prompt_seq: int = -1, pressure: int = 0
    ) -> None:
        calls = [call.to_dict() for call in completion.tool_calls]
        reasoning = completion.reasoning or ""
        self.events.append(
            "assistant/message",
            text=completion.text or "",
            reasoning=str(reasoning)[:4000],
            tool_calls=calls,
            usage=completion.usage,
            prompt_seq=prompt_seq,
            prompt_tokens_actual=completion.prompt_tokens,
            pressure_tokens=pressure,
            finish_reason=completion.finish_reason,
            model=completion.model,
        )
        preview = (completion.text or "").strip().replace("\n", " ")[:160]
        self.logger.info(f"模型回复（{len(completion.text or '')} 字，{len(calls)} 个调用）：{preview}")

    def _run_tool_calls(self, completion: Completion) -> None:
        calls = completion.tool_calls
        for call in calls[:MAX_TOOL_CALLS_PER_TURN]:
            result = self.toolbox.dispatch(call.name, call.arguments, call_id=call.id)
            self.events.append(
                "tool/result",
                call_id=call.id,
                name=call.name,
                ok=result.ok,
                output=result.text,
                job_id=result.job_id,
                duration_ms=result.duration_ms,
            )
            self.archive.append_history(self._tool_history_line(call.name, result.ok, result.text))
            self.logger.info(f"工具 {call.name} → {'成功' if result.ok else '失败'}（{result.duration_ms}ms）")
        for call in calls[MAX_TOOL_CALLS_PER_TURN:]:
            output = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "TOOL_CALL_LIMIT",
                        "message": f"单轮最多执行 {MAX_TOOL_CALLS_PER_TURN} 个调用，这次没有执行。",
                    },
                },
                ensure_ascii=False,
            )
            self.events.append(
                "tool/result",
                call_id=call.id,
                name=call.name,
                ok=False,
                output=output,
                job_id=None,
                duration_ms=0,
            )
            self.logger.warn(f"工具调用超过单轮上限，已拒绝 {call.name}")

    # ---- 任务通知 ----
    def _report_job(self, job: Job, *, note: str = "") -> None:
        self.events.append(
            "job/settle",
            job_id=job.id,
            status=job.status,
            exit_code=job.exit_code,
            duration_ms=None,
            note=note,
        )
        label = f"（{job.label}）" if job.label else ""
        text = f"任务 {job.id}{label} 已结束：{job.status}，退出码 {job.exit_code}。{note}".strip()
        tail = self._tail_output(job.id)
        if tail:
            text += f"\nstdout 末尾：\n{tail}"
        self.events.append("system/message", text=text, kind="job")
        self.logger.info(f"任务结算：{job.id} {job.status}")

    def _tail_output(self, job_id: str, *, limit: int = 600) -> str:
        try:
            path = self.jobs.stdout_path(job_id)
            size = path.stat().st_size
            with open(path, "rb") as handle:
                handle.seek(max(0, size - limit * 2))
                data = handle.read()
            text = data.decode("utf-8", errors="replace")
            return text[-limit:].strip()
        except OSError:
            return ""

    @staticmethod
    def _tool_history_line(name: str, ok: bool, output: str) -> str:
        body = output if len(output) <= 300 else output[:180] + " ... " + output[-60:]
        return f"[工具 {name} {'成功' if ok else '失败'}] {body}"

    # ---- 状态与信号 ----
    def _write_state(self, status: str) -> None:
        save_kernel_state(
            self.archive,
            {
                "pid": os.getpid(),
                "name": self.config.name,
                "model": self.config.model,
                "status": status,
                "turn": self.turns,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        )

    def _install_signals(self) -> None:
        def handler(signum, frame):  # noqa: ANN001, ARG001
            self._stop = True
            self.logger.warn(f"收到信号 {signum}，准备退出")

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                self._old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    def _restore_signals(self) -> None:
        for signum, old in self._old_handlers.items():
            try:
                signal.signal(signum, old)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        self._old_handlers.clear()
