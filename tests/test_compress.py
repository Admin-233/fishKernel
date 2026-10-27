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


# ---- 待写记忆落盘（_flush_pending_memory）-----------------------------------
# 事故：曾经写成
#     if mode == "replace":
#         replace_text = content
#         appended = []          # 清空之前的 append
#     else:
#         appended.append(content)
# 最后 `if replace_text is not None:` 只用 replace_text 拼结果，
# 于是 **replace 之后的所有 append 被静默丢弃**，函数却照样返回处理条数。
# 「先 replace 重写长期记忆，再 append 追加」正是 Agent 自己的常见用法。


def _memory_events(*pairs):
    from fishkernel.events import Event

    return [
        Event(index + 1, "", "memory/write", {"mode": mode, "content": content})
        for index, (mode, content) in enumerate(pairs)
    ]


def _flusher(archive):
    """只取 _flush_pending_memory 所需的两个属性，绕开构造函数。"""
    from fishkernel.compress import Compressor

    compressor = Compressor.__new__(Compressor)
    compressor.archive = archive

    class _Logger:
        def info(self, message):  # noqa: ARG002
            pass

    compressor.logger = _Logger()
    return compressor


def test_flush_appends_after_replace_are_kept(archive):
    archive.write_long_term_memory("磁盘上的旧内容\n")
    compressor = _flusher(archive)
    events = _memory_events(
        ("append", "追加A"),
        ("replace", "替换后的新记忆"),
        ("append", "追加C"),
    )
    assert compressor._flush_pending_memory(events) == 3

    text = archive.read_text(archive.long_term_memory_path)
    assert "替换后的新记忆" in text
    assert "追加C" in text, "replace 之后的 append 被丢了"
    assert "追加A" not in text, "replace 之前的 append 应当被覆盖"
    assert "磁盘上的旧内容" not in text, "replace 应当覆盖磁盘上的旧内容"


def test_flush_append_only_keeps_existing(archive):
    archive.write_long_term_memory("旧内容\n")
    compressor = _flusher(archive)
    compressor._flush_pending_memory(_memory_events(("append", "新的一条")))

    text = archive.read_text(archive.long_term_memory_path)
    assert "旧内容" in text and "新的一条" in text


def test_flush_replace_only_discards_old(archive):
    archive.write_long_term_memory("旧内容\n")
    compressor = _flusher(archive)
    compressor._flush_pending_memory(_memory_events(("replace", "全新的")))

    text = archive.read_text(archive.long_term_memory_path)
    assert text.strip() == "全新的"


def test_flush_last_replace_wins(archive):
    compressor = _flusher(archive)
    compressor._flush_pending_memory(
        _memory_events(("replace", "第一版"), ("append", "中间的"), ("replace", "第二版"))
    )
    text = archive.read_text(archive.long_term_memory_path)
    assert "第二版" in text
    assert "第一版" not in text and "中间的" not in text


def test_flush_no_events_leaves_file_untouched(archive):
    archive.write_long_term_memory("保持原样\n")
    compressor = _flusher(archive)
    assert compressor._flush_pending_memory([]) == 0
    assert archive.read_text(archive.long_term_memory_path) == "保持原样\n"


def test_flush_ignores_blank_content(archive):
    compressor = _flusher(archive)
    compressor._flush_pending_memory(_memory_events(("append", "   "), ("replace", "")))
    # 全是空白 → 不该生成文件内容，也不该抛异常
    assert archive.read_text(archive.long_term_memory_path).strip() == ""


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
