"""事件日志归档（README 已知限制 2：events.jsonl 只增不减）。

## 什么可以安全归档

压缩之后，`compaction/summary` 事件里的 `to_seq` 之前的所有事件**已经不再参与
上下文投影**——`build_messages()` 只从最近一次 summary 的 `to_seq` 之后开始投影。
所以这些事件只承担「审计」职责，可以从热路径里挪走。

## 安全原则：先验证，后提交

归档是不可逆的破坏性操作，所以这里不信任「理论上等价」，而是**真的算一遍**：

1. 计算归档前 `build_messages()` 的结果；
2. 在内存里算出归档后的结果；
3. 两者逐字节比较，**不一致就放弃**，一个字节都不写。

只有完全一致才落盘。这是本工具唯一的正确性保证，也是最该被测试的部分。

## 不做什么

- 不重编号 `seq`：seq 是事件的身份，重编号会让 anchor、`prompt_seq`、
  历史记录全部失效。归档只删除，不改写。
- 不归档未闭合的压缩事务（`compaction/start` 没有配对的 end/abort）。
- 不归档没有发生过压缩的存档：没有 summary 就没有安全的切分点。
- 不删除 `compaction/summary` 事件本身：它是投影的起点，删了等于丢历史。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .archive import Archive
from .atomic import write_atomic
from .events import Event, read_events
from .messages import build_messages

# 归档文件放在 Memory/ 下，和 Archive-<ts>.txt（压缩存档）并列
ARCHIVE_PREFIX = "Archive-events-"


class TrimError(Exception):
    """归档被拒绝或验证失败。"""


@dataclass
class TrimPlan:
    """一次归档的计划：要挪走哪些、留下哪些、能省多少。"""

    cutoff_seq: int
    archived: list[Event]
    kept: list[Event]
    summary_event: Event

    @property
    def archived_bytes(self) -> int:
        return sum(len(event.to_json()) + 1 for event in self.archived)

    @property
    def empty(self) -> bool:
        return not self.archived


def _projection_signature(events: list[Event], system_text: str) -> list[dict]:
    """归档是否安全的判据：投影结果必须完全一致。"""
    return build_messages(events, system_text)


def plan_trim(archive: Archive, events: list[Event] | None = None) -> TrimPlan:
    """算出归档计划；不安全的存档直接抛 TrimError。"""
    events = events if events is not None else read_events(archive.events_path)
    if not events:
        raise TrimError("事件日志为空，没有可归档的内容")

    # 找到最近一次完成的压缩
    summary_index = -1
    for index, event in enumerate(events):
        if event.type == "compaction/summary":
            summary_index = index
    if summary_index < 0:
        raise TrimError(
            "这个存档还没有完成过压缩，没有安全的归档切分点。\n"
            "压缩之后，summary 里 to_seq 之前的事件才不再参与上下文投影。"
        )

    summary_event = events[summary_index]
    to_seq = summary_event.data.get("to_seq")
    if not isinstance(to_seq, int):
        raise TrimError("最近的 compaction/summary 缺少合法的 to_seq，拒绝归档")

    # summary 之后不能再有未闭合的压缩事务
    for event in events[summary_index + 1 :]:
        if event.type == "compaction/start":
            raise TrimError("存在未闭合的压缩事务（compaction/start 没有配对），拒绝归档")

    # 切分点：to_seq 及之前全部归档，但必须保留 summary 事件本身
    archived = [event for event in events if event.seq <= to_seq and event is not summary_event]
    kept = [event for event in events if event.seq > to_seq or event is summary_event]

    if not archived:
        return TrimPlan(cutoff_seq=to_seq, archived=[], kept=kept, summary_event=summary_event)

    # ---- 核心安全校验：投影必须逐字节一致 ----
    system_text = archive.system_text()
    before = _projection_signature(events, system_text)
    after = _projection_signature(kept, system_text)
    if before != after:
        raise TrimError(
            "归档后上下文投影会发生变化，已拒绝执行。\n"
            "这是个需要调查的情况：请把 events.jsonl 留档后报告。"
        )

    return TrimPlan(cutoff_seq=to_seq, archived=archived, kept=kept, summary_event=summary_event)


def apply_trim(archive: Archive, plan: TrimPlan, *, stamp: str | None = None) -> Path | None:
    """执行归档：先把要挪走的事件写成归档文件，再原子替换 events.jsonl。"""
    if plan.empty:
        return None

    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    archive_path = archive.memory_dir / f"{ARCHIVE_PREFIX}{stamp}.jsonl"
    header = (
        f'{{"_trim": true, "stamp": "{stamp}", "cutoff_seq": {plan.cutoff_seq}, '
        f'"archived_events": {len(plan.archived)}}}\n'
    )
    body = "\n".join(event.to_json() for event in plan.archived) + "\n"
    write_atomic(archive_path, header + body)

    # 归档文件写成功后才动原日志：这一步失败也只是白写一个归档文件，不会丢数据
    write_atomic(archive.events_path, "\n".join(event.to_json() for event in plan.kept) + "\n")

    # History.txt 是从事件投影出来的，跟着刷新
    archive.rewrite_history(plan.kept)
    return archive_path


def list_archives(archive: Archive) -> list[Path]:
    if not archive.memory_dir.is_dir():
        return []
    return sorted(archive.memory_dir.glob(f"{ARCHIVE_PREFIX}*.jsonl"))


def stats(archive: Archive) -> dict[str, object]:
    """给 CLI 用的概览。"""
    events = read_events(archive.events_path)
    try:
        size = archive.events_path.stat().st_size
    except OSError:
        size = 0
    return {
        "events": len(events),
        "bytes": size,
        "first_seq": events[0].seq if events else 0,
        "last_seq": events[-1].seq if events else 0,
        "archives": len(list_archives(archive)),
    }
