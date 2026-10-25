from __future__ import annotations

from fishkernel.events import Event
from fishkernel.tokens import TokenMeter, estimate_messages, estimate_text


def ev(seq: int, etype: str, **data) -> Event:
    return Event(seq=seq, ts="", type=etype, data=data)


def test_estimate_text_follows_official_ratio():
    # 官方文档：1 个中文字符 ≈ 0.6 token，1 个英文字符 ≈ 0.3 token
    assert estimate_text("中文中文") == 3
    assert estimate_text("abcdef") == 2
    assert estimate_text("") == 0


def test_no_anchor_falls_back_to_estimate():
    meter = TokenMeter()
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "你好"}]
    pressure = meter.pressure(events=[], messages=messages, system_text="s", tools=None)
    assert pressure == estimate_messages(messages)


def test_anchor_uses_api_exact_count_plus_delta():
    meter = TokenMeter()
    events = [ev(1, "user/message", text="a")]
    sent = [{"role": "system", "content": "s"}, {"role": "user", "content": "a"}]
    meter.record(prompt_seq=1, prompt_tokens=1000, system_text="s", tools=None, estimated_prompt=800)

    events.append(ev(2, "user/message", text="b"))
    messages = sent + [{"role": "user", "content": "b"}]
    pressure = meter.pressure(events=events, messages=messages, system_text="s", tools=None)

    delta = estimate_messages([{"role": "user", "content": "b"}])
    assert pressure == 1000 + int(delta * meter.ratio)
    assert meter.anchor is not None and meter.anchor.prompt_tokens == 1000


def test_calibration_scales_delta():
    meter = TokenMeter()
    meter.record(prompt_seq=1, prompt_tokens=200, system_text="", tools=None, estimated_prompt=100)
    assert abs(meter.ratio - 2.0) < 1e-9

    meter.record(prompt_seq=2, prompt_tokens=200, system_text="", tools=None, estimated_prompt=100)
    # EMA：0.7*2 + 0.3*2 = 2
    assert abs(meter.ratio - 2.0) < 1e-9


def test_compaction_invalidates_anchor():
    meter = TokenMeter()
    events = [ev(1, "user/message", text="a")]
    meter.record(prompt_seq=1, prompt_tokens=1000, system_text="s", tools=None, estimated_prompt=800)
    events.extend([ev(2, "compaction/start"), ev(3, "compaction/summary", summary="概要", to_seq=1)])
    messages = [{"role": "system", "content": "s"}]
    pressure = meter.pressure(events=events, messages=messages, system_text="s", tools=None)
    assert meter.anchor is None
    # 锚点失效后退回全量估算，但保留已学到的比例校准
    assert pressure == int(estimate_messages(messages) * meter.ratio)


def test_restore_does_not_double_count_tools():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读文件",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    events = [
        ev(1, "user/message", text="a"),
        ev(2, "assistant/message", text="x", usage={"prompt_tokens": 500}, prompt_seq=1),
    ]
    meter = TokenMeter()
    meter.restore(events, "s", tools)
    messages = [{"role": "system", "content": "s"}]
    pressure = meter.pressure(events=events, messages=messages, system_text="s", tools=tools)
    # 锚点 500 已经包含工具 schema；新增的只有那条 assistant 消息
    expected = 500 + estimate_messages([{"role": "assistant", "content": "x"}])
    assert pressure == expected


def test_restore_from_events():
    events = [
        ev(1, "user/message", text="a"),
        ev(2, "assistant/message", text="x", usage={"prompt_tokens": 500}, prompt_seq=1),
    ]
    meter = TokenMeter()
    meter.restore(events, "s")
    assert meter.anchor is not None
    assert meter.anchor.prompt_tokens == 500
    assert meter.anchor.prompt_seq == 1


def test_restore_ignores_anchor_before_compaction():
    events = [
        ev(1, "user/message", text="a"),
        ev(2, "assistant/message", text="x", usage={"prompt_tokens": 500}, prompt_seq=1),
        ev(3, "compaction/summary", summary="概要", to_seq=2),
    ]
    meter = TokenMeter()
    meter.restore(events, "s")
    assert meter.anchor is None


def test_describe_mentions_ratio():
    meter = TokenMeter()
    assert "无锚点" in meter.describe()
    meter.record(prompt_seq=0, prompt_tokens=100, system_text="", tools=None, estimated_prompt=100)
    assert "锚点" in meter.describe()
