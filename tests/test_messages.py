from __future__ import annotations

from fishkernel.events import Event
from fishkernel.messages import SUMMARY_TAG, build_messages, conversation_units
from fishkernel.tokens import estimate_text


def ev(seq: int, etype: str, **data) -> Event:
    return Event(seq=seq, ts="", type=etype, data=data)


def test_basic_projection():
    events = [
        ev(1, "user/message", text="你好"),
        ev(2, "assistant/message", text="在的"),
    ]
    messages = build_messages(events, "系统提示")
    assert messages[0] == {"role": "system", "content": "系统提示"}
    assert messages[1] == {"role": "user", "content": "你好"}
    assert messages[2] == {"role": "assistant", "content": "在的"}


def test_orphan_tool_call_gets_unknown_result():
    events = [
        ev(1, "user/message", text="跑一下"),
        ev(2, "assistant/message", text="", tool_calls=[{"id": "c1", "name": "list_dir", "arguments": "{}"}]),
    ]
    messages = build_messages(events, "s")
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "c1"
    assert "TOOL_OUTCOME_UNKNOWN" in messages[-1]["content"]


def test_tool_result_pairing():
    events = [
        ev(1, "user/message", text="看目录"),
        ev(2, "assistant/message", text="", tool_calls=[{"id": "c1", "name": "list_dir", "arguments": "{}"}]),
        ev(3, "tool/result", call_id="c1", name="list_dir", ok=True, output='{"ok":true}'),
        ev(4, "assistant/message", text="看完了"),
    ]
    messages = build_messages(events, "s")
    roles = [message["role"] for message in messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[3]["tool_call_id"] == "c1"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "list_dir"


def test_summary_base_and_after():
    events = [
        ev(1, "user/message", text="旧消息"),
        ev(2, "compaction/start"),
        ev(3, "compaction/summary", summary="这是旧消息的概要"),
        ev(4, "compaction/end", ok=True),
        ev(5, "user/message", text="新消息"),
    ]
    messages = build_messages(events, "s")
    assert len(messages) == 3
    assert SUMMARY_TAG in messages[1]["content"]
    assert "旧消息的概要" in messages[1]["content"]
    assert messages[2]["content"] == "新消息"


def test_runtime_messages_merged():
    events = [
        ev(1, "user/message", text="开始"),
        ev(2, "system/message", text="任务结束", kind="job"),
        ev(3, "system/message", text="任务结束2", kind="job"),
    ]
    messages = build_messages(events, "s")
    assert len(messages) == 2
    assert "[runtime]" in messages[1]["content"]
    assert "任务结束2" in messages[1]["content"]


def test_conversation_units_keep_tool_pairs():
    events = [
        ev(1, "user/message", text="a"),
        ev(2, "assistant/message", text="", tool_calls=[{"id": "c1", "name": "x", "arguments": "{}"}]),
        ev(3, "tool/result", call_id="c1", name="x", ok=True, output="{}"),
        ev(4, "user/message", text="b"),
    ]
    units = conversation_units(events)
    assert len(units) == 3
    assert len(units[1]) == 2  # assistant + tool 一起


def test_estimate_text():
    assert estimate_text("") == 0
    assert estimate_text("abcdef") > 0
    # 官方比例：中文 ≈0.6 token/字，英文 ≈0.3 token/字符
    assert estimate_text("中文中文") == 3
