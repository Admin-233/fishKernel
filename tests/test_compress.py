from __future__ import annotations

from fishkernel.compress import Compressor
from fishkernel.events import read_events
from fishkernel.llm import Completion, ScriptedClient
from fishkernel.messages import SUMMARY_TAG, build_messages


def seed_conversation(log, pairs: int = 6) -> None:
    for index in range(pairs):
        log.append("user/message", text=f"第 {index} 条用户消息" + "长" * 30)
        log.append("assistant/message", text=f"第 {index} 条回复" + "好" * 30)


def test_compact_replaces_old_region(archive, config, log):
    seed_conversation(log)
    client = ScriptedClient(
        [Completion(text="## 主要请求与意图\n用户在做测试。", usage={"prompt_tokens": 100, "completion_tokens": 10})]
    )
    compressor = Compressor(archive, config, client, log)
    outcome = compressor.compact(retain_override=1)

    assert outcome.compacted is True
    events = read_events(archive.events_path)
    types = [event.type for event in events]
    assert "compaction/start" in types
    assert "compaction/summary" in types
    assert "compaction/end" in types

    assert "用户在做测试。" in archive.read_text(archive.digest_path)
    assert list(archive.memory_dir.glob("Archive-*.txt"))

    messages = build_messages(events, "系统")
    assert SUMMARY_TAG in messages[1]["content"]
    assert messages[-1]["role"] == "assistant"
    assert "第 5 条回复" in messages[-1]["content"]


def test_region_never_splits_tool_pairs(archive, config, log):
    log.append("user/message", text="开始")
    log.append("assistant/message", text="", tool_calls=[{"id": "c1", "name": "list_dir", "arguments": "{}"}])
    log.append("tool/result", call_id="c1", name="list_dir", ok=True, output="{}")
    log.append("user/message", text="继续")
    log.append("assistant/message", text="好的" + "好" * 40)
    log.append("user/message", text="最后")
    log.append("assistant/message", text="结束" + "终" * 40)

    client = ScriptedClient([Completion(text="概要")])
    compressor = Compressor(archive, config, client, log)
    outcome = compressor.compact(retain_override=1)
    assert outcome.compacted

    events = read_events(archive.events_path)
    summary = next(event for event in events if event.type == "compaction/summary")
    to_seq = summary.data["to_seq"]
    region = [event for event in events if event.seq <= to_seq]
    for index, event in enumerate(region):
        if event.type == "assistant/message" and event.data.get("tool_calls"):
            following = region[index + 1] if index + 1 < len(region) else None
            assert following is not None and following.type == "tool/result"


def test_rejects_summary_not_shorter(archive, config, log):
    seed_conversation(log)
    long_summary = "长" * 20_000
    client = ScriptedClient([Completion(text=long_summary), Completion(text=long_summary)])
    compressor = Compressor(archive, config, client, log)
    outcome = compressor.compact(retain_override=1)

    assert outcome.compacted is False
    types = [event.type for event in read_events(archive.events_path)]
    assert "compaction/abort" in types
    assert "compaction/summary" not in types


def test_nothing_to_compact(archive, config, log):
    compressor = Compressor(archive, config, ScriptedClient(), log)
    outcome = compressor.compact(retain_override=1)
    assert outcome.compacted is False
    assert outcome.reason == "nothing_to_compact"


def test_stale_lock_recovery(archive, config, log):
    log.append("compaction/start", trigger="auto")
    compressor = Compressor(archive, config, ScriptedClient(), log)
    assert compressor.recover_stale() is True
    types = [event.type for event in read_events(archive.events_path)]
    assert types[-1] == "compaction/abort"


def test_chunked_summarization_for_oversized_region(archive, config, log):
    seed_conversation(log, pairs=8)
    script = [Completion(text=f"分块概要 {index}") for index in range(12)]
    client = ScriptedClient(script)
    compressor = Compressor(archive, config, client, log, chunk_limit=120)
    outcome = compressor.compact(retain_override=1)

    assert outcome.compacted is True
    assert len(client.calls) >= 3  # 至少两个分块 + 一次合并
    merged_request = client.calls[-1]["messages"][-1]["content"]
    assert "分块概要" in merged_request
    assert archive.read_text(archive.digest_path).strip()


def test_no_stale_lock_when_closed(archive, config, log):
    log.append("compaction/start")
    log.append("compaction/end", ok=True)
    compressor = Compressor(archive, config, ScriptedClient(), log)
    assert compressor.recover_stale() is False


# Memory operations are committed independently of compaction.
def _flusher(archive):
    from fishkernel.config import load_config
    from fishkernel.events import EventLog
    return Compressor(archive, load_config(archive.root), ScriptedClient(), EventLog(archive.events_path))


def test_memory_replace_append_sequence(archive, log):
    from fishkernel.memory import MemoryStore
    archive.write_long_term_memory("original")
    store = MemoryStore(archive)
    store.recover([])
    for mode, content in [("append", "A"), ("replace", "B"), ("append", "C")]:
        log.append("memory/write", mode=mode, content=content)
        store.recover(read_events(archive.events_path))
    assert archive.read_text(archive.long_term_memory_path) == "B\n\nC"
    _flusher(archive)._flush_pending_memory(read_events(archive.events_path))
    assert archive.read_text(archive.long_term_memory_path) == "B\n\nC"


# ---- 超大尾部（oversized_tail）边界 ----------------------------------------
# 保留逻辑是：从最新单元往回累加，直到不超过 retain 预算。如果**最新一个单元本身**
# 就超过预算，循环一次都不进，kept=0，于是走兜底 kept=1，并记 oversized_tail=True。
# 这意味着它前面的所有正常对话都会被一次性摘要掉。
#
# 这不是崩溃，但代价会被严重低估：一条 12 万 token 的用户消息（比如贴了一大段日志）
# 就能把之前几十轮正常对话全部送进摘要。至少要保证：
#   1) 兜底一定保住最新单元，不会出现「最新消息被压缩掉」；
#   2) 事件里留下 oversized_tail 标记，事后能定位；
#   3) 最新单元确实不进入压缩区（to_seq 不包括它）。


def _seed_turns(log, count: int):
    for index in range(count):
        log.append("user/message", text=f"小问题 {index}")
        log.append("assistant/message", text=f"小回答 {index}")


def test_oversized_tail_keeps_newest_unit(archive, config, log):
    _seed_turns(log, 5)
    log.append("user/message", text="巨大内容" + "字" * 200_000)

    client = ScriptedClient(
        [Completion(text="## 主要请求与意图\n压缩了旧对话。", usage={"prompt_tokens": 100, "completion_tokens": 10})]
    )
    compressor = Compressor(archive, config, client, log)
    # retain_override 给小，保证最新那条一定超预算
    outcome = compressor.compact(retain_override=4096)
    assert outcome.compacted is True

    events = read_events(archive.events_path)
    start = next(event for event in events if event.type == "compaction/start")
    assert start.data["retained_oversized"] is True, "应标记 oversized_tail"
    assert start.data["retained_units"] == 1

    # 最新那条 user 消息绝不能进压缩区
    summary = next(event for event in events if event.type == "compaction/summary")
    to_seq = summary.data["to_seq"]
    newest_seq = max(event.seq for event in events if event.type == "user/message")
    assert newest_seq > to_seq, "最新的用户消息被压缩掉了"


def test_oversized_tail_region_ends_before_newest(archive, config, log):
    """大消息之前的正常对话确实会被压缩——把这个代价显式钉住。

    这里不是在为「损失」背书，而是让行为可观测：如果将来改进到能保留更多，
    这条断言会失败并提醒更新预期。
    """
    _seed_turns(log, 5)
    log.append("user/message", text="巨大内容" + "字" * 200_000)

    client = ScriptedClient(
        [Completion(text="## 主要请求与意图\n旧对话摘要。", usage={"prompt_tokens": 100, "completion_tokens": 10})]
    )
    compressor = Compressor(archive, config, client, log)
    compressor.compact(retain_override=4096)

    events = read_events(archive.events_path)
    start = next(event for event in events if event.type == "compaction/start")
    # 5 轮正常对话 = 10 个单元，全部进入压缩区
    assert start.data["region_units"] == 10
    assert start.data["retained_units"] == 1


def test_normal_tail_retains_within_budget(archive, config, log):
    """没有超大单元时，保留应该按预算尽量多留，且不误标 oversized_tail。"""
    _seed_turns(log, 5)
    client = ScriptedClient(
        [Completion(text="## 主要请求与意图\n摘要。", usage={"prompt_tokens": 100, "completion_tokens": 10})]
    )
    compressor = Compressor(archive, config, client, log)
    compressor.compact(retain_override=20)   # 小预算：大约够留两条

    events = read_events(archive.events_path)
    start = next(event for event in events if event.type == "compaction/start")
    assert start.data["retained_oversized"] is False
    assert start.data["retained_units"] >= 1
    assert start.data["region_units"] + start.data["retained_units"] == 10


# ---- 小压缩区 / 摘要不比原文短 ----------------------------------------------
# _summarize_once 有收敛条件：摘要 token 必须 < 压缩区 token，否则重试一次后放弃。
# 这条守卫很重要——如果没有它，一个"比原文还长"的摘要会写进 Digest，
# 反而增大上下文，压缩就成了负优化。
# 下面这个场景确保：摘要过长时走 abort，绝不落盘、绝不改写历史。


def test_aborts_when_summary_longer_than_region(archive, config, log):
    for index in range(20):
        log.append("user/message", text=f"q{index}")
        log.append("assistant/message", text=f"a{index}", tool_calls=[])

    long_summary = "## 主要请求与意图\n" + "刻意写得比原文还长的摘要。" * 50
    client = ScriptedClient(
        [Completion(text=long_summary, usage={"prompt_tokens": 10, "completion_tokens": 10})] * 3
    )
    compressor = Compressor(archive, config, client, log)
    outcome = compressor.compact(retain_override=50)

    assert outcome.compacted is False
    assert outcome.reason == "summary_not_shorter"

    events = read_events(archive.events_path)
    types = [event.type for event in events]
    assert "compaction/abort" in types
    assert "compaction/summary" not in types, "摘要过长却仍然提交了"
    assert archive.read_text(archive.digest_path).strip() == "", "Digest 被写坏了"


def test_abort_leaves_history_intact(archive, config, log):
    """abort 之后，上下文投影必须与压缩前完全一致。"""
    for index in range(20):
        log.append("user/message", text=f"q{index}")
        log.append("assistant/message", text=f"a{index}", tool_calls=[])

    from fishkernel.messages import build_messages

    before = build_messages(read_events(archive.events_path), archive.system_text())

    client = ScriptedClient(
        [Completion(text="长" * 2000, usage={"prompt_tokens": 10, "completion_tokens": 10})] * 3
    )
    Compressor(archive, config, client, log).compact(retain_override=50)

    after = build_messages(read_events(archive.events_path), archive.system_text())
    assert before == after, "压缩中止后历史被改写了"


def test_normal_compaction_still_works_after_abort(archive, config, log):
    """中止一次之后，下一次（摘要正常）应当照常压缩成功。"""
    for index in range(20):
        log.append("user/message", text=f"q{index}")
        log.append("assistant/message", text=f"a{index}", tool_calls=[])

    bad = ScriptedClient([Completion(text="长" * 2000, usage={})] * 3)
    assert Compressor(archive, config, bad, log).compact(retain_override=50).compacted is False

    good = ScriptedClient([Completion(text="## 概要\n简短摘要。", usage={})])
    outcome = Compressor(archive, config, good, log).compact(retain_override=50)
    assert outcome.compacted is True
    assert outcome.reason == "ok"


# ---- 压缩区外有更新记忆时不得回退 -------------------------------------------
# 真实事故（2026-10-27）：压缩只处理「压缩区内」的 memory/write。如果之后在保留区
# 又用 memory 工具改过记忆，那些内容不在 pending 里，却是更新的事实。
# 此时若按 pending 里的旧 replace 覆盖磁盘，就把更新的记忆打回了旧版。
#
# 实测：磁盘 3686 字符的完整记忆，压缩区内只有 seq 48/498 两个旧 replace
# （合计 1620 字符），压缩后磁盘被覆盖成旧版。而最新的 replace(seq 3028)
# 因为"还在上下文里"反而被跳过 —— 越新越容易丢，非常反直觉。


def _write_memory(archive, content: str, seq: int, mode: str = "replace"):
    from fishkernel.events import Event

    return Event(seq, "", "memory/write", {"mode": mode, "content": content})


def test_flush_skips_when_newer_memory_exists_outside_region(archive, log):
    """压缩区外有更新记忆时，一个字都不许写。"""
    disk_now = "# 磁盘上的最新完整记忆\n" + "重要内容。" * 50
    archive.write_long_term_memory(disk_now)

    # 事件流：旧 replace 在前，更新的 replace 在后（后者不在 pending 里）
    log.append("memory/write", mode="replace", content="旧版记忆")
    log.append("memory/write", mode="replace", content=disk_now)

    from fishkernel.events import read_events

    events = read_events(archive.events_path)
    region = [events[0]]          # 只有第一条在压缩区里
    compressor = _flusher(archive)

    written = compressor._flush_pending_memory(region)
    assert written == 1
    assert archive.read_text(archive.long_term_memory_path).strip() == disk_now.strip()


def test_flush_still_writes_when_region_has_newest(archive, log):
    """没有更新记忆时，正常落盘行为不变。"""
    log.append("memory/write", mode="replace", content="这是最新的记忆")
    from fishkernel.events import read_events

    events = read_events(archive.events_path)
    compressor = _flusher(archive)
    assert compressor._flush_pending_memory(events) == 1
    assert "这是最新的记忆" in archive.read_text(archive.long_term_memory_path)


def test_memory_applies_newer_writes_outside_compaction_region(archive, log):
    older = log.append("memory/write", mode="append", content="older")
    log.append("memory/write", mode="append", content="newer")
    _flusher(archive)._flush_pending_memory([older])
    assert archive.read_text(archive.long_term_memory_path) == "older\n\nnewer"
