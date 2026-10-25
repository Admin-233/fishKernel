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
