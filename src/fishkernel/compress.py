"""上下文压缩：阈值判定、选区、摘要事务。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .archive import Archive, ArchiveError, render_history
from .config import InstanceConfig, ConfigError
from .constants import SUMMARY_MAX_TOKENS_CAP
from .events import Event, EventLog, read_events
from .llm import LLMClient, LLMError
from .messages import conversation_units, project_units, message_units, ProjectionError
from .tokens import estimate_message, estimate_messages, estimate_text, estimate_tools

SUMMARY_SYSTEM = """你是上下文压缩器。你的唯一任务是把给定的对话历史压缩成一份结构化的中文概要，
供另一个正在运行的 Agent 在未来继续工作。不要臆造细节，不要评论压缩本身，只输出概要正文。"""

SUMMARY_INSTRUCTION = """请把上面的对话压缩成一份可以独立支撑后续工作的概要，严格使用下面的小标题：

## 主要请求与意图
## 关键技术概念
## 文件与代码
## 错误与修复
## 进行中的任务
## 当前工作
## 下一步
## 关键上下文

要求：保留具体路径、命令、ID、数字与未完成的承诺；删掉寒暄与重复；总长度不超过原文的 30%。"""

RETRY_INSTRUCTION = "摘要太长了。请压缩到原文 20% 以内，只保留继续工作必需的事实。"


def _split_by_tokens(messages: list[dict], limit: int) -> list[list[dict]]:
    """按估算 token 把消息切成若干块，保持消息完整。"""
    limit = max(1, limit)
    chunks: list[list[dict]] = []
    current: list[dict] = []
    total = 0
    for unit in message_units(messages):
        cost = sum(estimate_message(message) for message in unit)
        if current and total + cost > limit:
            chunks.append(current)
            current = []
            total = 0
        current.extend(unit)
        total += cost
    if current:
        chunks.append(current)
    return chunks or [messages]


@dataclass
class CompactionOutcome:
    compacted: bool
    reason: str = ""
    freed_tokens: int = 0
    archived: str | None = None
    summary: str = ""
    region_tokens: int = 0


class Compressor:
    def __init__(
        self,
        archive: Archive,
        config: InstanceConfig,
        client: LLMClient,
        events: EventLog,
        logger=None,
        *,
        chunk_limit: int | None = None,
    ):
        self.archive = archive
        self.config = config
        self.client = client
        self.events = events
        self.logger = logger
        # 单次摘要请求最多占多大上下文；超过就分块摘要再合并。
        self._chunk_limit = chunk_limit

    @property
    def chunk_limit(self):
        return self._chunk_limit or max(1024, self.config.spec.context_window // 2)

    def _project(self, units):
        return project_units(units, reasoning_field=self.config.options.get("reasoning_field"),
                             require_reasoning=self.config.options.get("require_reasoning", False))

    # ---- 观测 ----
    @property
    def threshold(self) -> int:
        return self.config.budget.threshold

    @property
    def retain(self) -> int:
        return self.config.budget.retain

    def pressure(self, messages: list[dict], tools: list[dict] | None) -> int:
        return estimate_messages(messages) + estimate_tools(tools)

    def should_compact(self, pressure: int) -> bool:
        return pressure >= self.threshold

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(f"[compact] {message}")

    # ---- 崩溃恢复 ----
    def recover_stale(self) -> bool:
        """未闭合的 compaction/start 视为上次崩溃残留，补一条 abort。"""
        events = read_events(self.archive.events_path)
        committed = [e for e in events if e.type == "compaction/summary"
                     and e.data.get("projection_version") == 1]
        if committed:
            summary = str(committed[-1].data["summary"])
            if self.archive.read_text(self.archive.digest_path) != summary:
                self.archive.write_digest(summary)
        last_start = -1
        for index, event in enumerate(events):
            if event.type == "compaction/start":
                last_start = index
            elif event.type in {"compaction/end", "compaction/abort"}:
                last_start = -1
        if last_start < 0:
            return False
        committed_here = any(e.type == "compaction/summary" for e in events[last_start + 1:])
        self.events.append("compaction/end" if committed_here else "compaction/abort",
                           ok=committed_here, reason="recovered", recovered=True,
                           transaction_id=events[last_start].data.get("transaction_id"))
        self._log("发现未闭合的压缩事务，已标记为 stale")
        return True

    # ---- 压缩 ----
    def compact(self, *, trigger: str = "auto", retain_override: int | None = None) -> CompactionOutcome:
        events = read_events(self.archive.events_path)
        units = conversation_units(events)
        if len(units) <= 1:
            return CompactionOutcome(compacted=False, reason="nothing_to_compact")

        unit_tokens = [estimate_messages(self._project([unit])) for unit in units]
        retain_budget = max(1, retain_override if retain_override is not None else self.retain)

        kept = 0
        kept_tokens = 0
        while kept < len(units) and kept_tokens + unit_tokens[len(units) - 1 - kept] <= retain_budget:
            kept_tokens += unit_tokens[len(units) - 1 - kept]
            kept += 1
        oversized_tail = kept == 0
        if kept == 0:
            # 最新一个单元就超过保留预算：仍然保留它，压缩其余部分。
            # 危险情况：一旦最新单元异常巨大（例如把图片 base64 当文本估算），
            # 就会把整份历史一次性摘要掉。这里显式记录，便于事后定位。
            kept = 1
            kept_tokens = unit_tokens[-1]
        if kept >= len(units):
            return CompactionOutcome(compacted=False, reason="nothing_to_compact")

        region_units = units[: len(units) - kept]
        region_messages = self._project(region_units)
        region_tokens = estimate_messages(region_messages)
        region_events = [event for unit in region_units for event in unit]
        from_seq = region_events[0].seq
        to_seq = region_events[-1].seq

        transaction_id = uuid.uuid4().hex
        self.events.append(
            "compaction/start",
            transaction_id=transaction_id,
            trigger=trigger,
            threshold=self.threshold,
            region_tokens=region_tokens,
            region_units=len(region_units),
            retained_units=kept,
            retained_tokens=kept_tokens,
            retained_oversized=oversized_tail,
            retain_budget=retain_budget,
        )

        summary_max_tokens = min(SUMMARY_MAX_TOKENS_CAP, max(256, retain_budget // 8))
        try:
            summary, usage = self._summarize(region_messages, region_tokens, summary_max_tokens)
            if summary is None:
                self.events.append("compaction/abort", transaction_id=transaction_id, reason="摘要未缩短输入，重试后放弃")
                return CompactionOutcome(compacted=False, reason="summary_not_shorter")
            return self._commit(
                transaction_id=transaction_id,
                summary=summary,
                usage=usage,
                region_events=region_events,
                from_seq=from_seq,
                to_seq=to_seq,
                region_tokens=region_tokens,
                kept_tokens=kept_tokens,
            )
        except (ArchiveError, OSError):
            raise
        except Exception as exc:  # Model failures leave the previous committed summary intact.
            self.events.append("compaction/abort", transaction_id=transaction_id, reason=f"{type(exc).__name__}: {exc}"[:500])
            self._log(f"压缩失败：{exc}")
            if isinstance(exc, (ConfigError, ProjectionError)) or (isinstance(exc, LLMError) and not exc.retryable):
                raise
            return CompactionOutcome(compacted=False, reason="error")

    def _summarize(self, region_messages: list[dict], region_tokens: int, max_tokens: int):
        previous_digest = self.archive.read_text(self.archive.digest_path).strip()
        instruction = SUMMARY_INSTRUCTION
        if previous_digest:
            instruction += (
                "\n\n## 上一份概要\n下面这份概要对应更早、已被压缩的历史，请把其中的事实合并进新概要，不要丢失：\n\n"
                + previous_digest
            )

        # 区域本身就快把窗口占满时，先分块摘要，再做一次合并，避免摘要请求自己溢出。
        if region_tokens > self.chunk_limit:
            chunks = _split_by_tokens(region_messages, self.chunk_limit)
            partials: list[str] = []
            for index, chunk in enumerate(chunks, start=1):
                text, _ = self._summarize_once(
                    chunk,
                    estimate_messages(chunk),
                    max_tokens,
                    SUMMARY_SYSTEM,
                    SUMMARY_INSTRUCTION,
                    attempt_note=f"（第 {index}/{len(chunks)} 块）",
                )
                if text is None:
                    return None, {}
                partials.append(f"### 第 {index} 块\n{text}")
            merge_request = [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": "下面是同一次对话按时间顺序分成的若干块的分块概要。请把它们合并成一份完整概要，"
                                            "去掉重复，保留所有具体路径、命令、ID 与未完成的承诺。\n\n" + "\n\n".join(partials)
                                            + ("\n\n" + instruction if previous_digest else "")},
            ]
            completion = self.client.complete(merge_request, tools=None, max_tokens=max_tokens, temperature=0.3)
            merged = completion.text.strip()
            if not merged or estimate_text(merged) >= region_tokens:
                return None, completion.usage or {}
            return merged, completion.usage or {}

        return self._summarize_once(region_messages, region_tokens, max_tokens, SUMMARY_SYSTEM, instruction)

    def _summarize_once(
        self,
        messages: list[dict],
        region_tokens: int,
        max_tokens: int,
        system_text: str,
        instruction_text: str,
        *,
        attempt_note: str = "",
    ):
        request = [
            {"role": "system", "content": system_text},
            *messages,
            {"role": "user", "content": attempt_note + instruction_text},
        ]
        best: str | None = None
        usage: dict[str, int] = {}
        for _ in range(2):
            completion = self.client.complete(
                request,
                tools=None,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            usage = completion.usage or {}
            summary = completion.text.strip()
            if not summary:
                request = request + [{"role": "user", "content": "上一次输出为空。请直接输出概要正文。"}]
                continue
            if estimate_text(summary) >= region_tokens:
                request = request + [{"role": "user", "content": RETRY_INSTRUCTION}]
                best = summary
                continue
            return summary, usage
        return best if best and estimate_text(best) < region_tokens else None, usage

    def _flush_pending_memory(self, region_events: list[Event]) -> int:
        from .memory import MemoryStore
        MemoryStore(self.archive).recover(read_events(self.archive.events_path))
        return sum(event.type == "memory/write" for event in region_events)

    def _commit(
        self,
        *,
        transaction_id: str,
        summary: str,
        usage: dict,
        region_events: list[Event],
        from_seq: int,
        to_seq: int,
        region_tokens: int,
        kept_tokens: int,
    ) -> CompactionOutcome:
        import uuid
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        archived_path = self.archive.archive_record_path(stamp)
        header = (
            f"# 压缩存档 {stamp}\n"
            f"# 源区间 seq {from_seq}..{to_seq}，估算 {region_tokens} tokens\n\n"
        )
        body = render_history(region_events)
        from .atomic import write_atomic

        write_atomic(archived_path, header + body + "\n")
        flushed = self._flush_pending_memory(region_events)
        if flushed:
            self._log(f"把 {flushed} 条待写记忆落盘到 LongTermMemory.txt")
        summary_tokens = estimate_text(summary)
        freed = max(0, region_tokens - summary_tokens)
        self.events.append(
            "compaction/summary",
            transaction_id=transaction_id,
            projection_version=1,
            summary=summary,
            from_seq=from_seq,
            to_seq=to_seq,
            archived=f"Memory/{archived_path.name}",
            freed_tokens=freed,
            region_tokens=region_tokens,
            summary_tokens=summary_tokens,
            retained_tokens=kept_tokens,
            usage=usage,
        )
        self.archive.write_digest(summary)
        self.events.append("compaction/end", ok=True, transaction_id=transaction_id)
        self.archive.rewrite_history(read_events(self.archive.events_path))
        self._log(f"压缩完成：seq {from_seq}..{to_seq} → 概要，释放约 {freed} tokens")
        return CompactionOutcome(
            compacted=True,
            reason="ok",
            freed_tokens=freed,
            archived=f"Memory/{archived_path.name}",
            summary=summary,
            region_tokens=region_tokens,
        )
