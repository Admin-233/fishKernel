from __future__ import annotations

from fishkernel.control import Control
from fishkernel.events import read_events
from fishkernel.llm import Completion, LLMError, ScriptedClient, ToolCall
from fishkernel.loop import Kernel


def make_kernel(archive, script, *, max_turns: int = 1) -> Kernel:
    return Kernel(
        archive.root,
        client=ScriptedClient(script),
        max_turns=max_turns,
        echo=False,
        error_backoff_base=0,
    )


def test_two_turns_with_tool_call(archive):
    kernel = make_kernel(
        archive,
        [
            Completion(
                text="先看看目录",
                tool_calls=[ToolCall(id="c1", name="list_dir", arguments='{"path":"."}')],
                usage={"prompt_tokens": 100, "completion_tokens": 5},
            ),
            Completion(text="看完了", usage={"prompt_tokens": 200, "completion_tokens": 5}),
        ],
        max_turns=2,
    )
    assert kernel.run() == 0

    events = read_events(archive.events_path)
    types = [event.type for event in events]
    assert types.count("turn/start") == 2
    assert "tool/result" in types
    result = next(event for event in events if event.type == "tool/result")
    assert result.data["call_id"] == "c1"
    assert result.data["ok"] is True
    assert types[-1] == "session/end"
    assistants = [event for event in events if event.type == "assistant/message"]
    assert assistants[-1].data["text"] == "看完了"
    # API 的精确用量会落进事件，并可作为下一次压缩判定的锚点
    assert assistants[0].data["prompt_tokens_actual"] == 100
    assert isinstance(assistants[0].data["prompt_seq"], int)
    assert isinstance(assistants[0].data["pressure_tokens"], int)


def test_stop_state_prevents_turns(archive):
    Control(archive).write_state(run="stop")
    kernel = make_kernel(archive, [Completion(text="不应该被调用")], max_turns=3)
    assert kernel.run() == 0
    events = read_events(archive.events_path)
    assert not [event for event in events if event.type == "turn/start"]
    assert events[-1].data["reason"] == "stop"


def test_error_then_recovery(archive):
    kernel = make_kernel(
        archive,
        [LLMError("模拟崩溃", retryable=False), Completion(text="我恢复了")],
        max_turns=2,
    )
    assert kernel.run() == 0
    events = read_events(archive.events_path)
    types = [event.type for event in events]
    assert "error" in types
    assert [event for event in events if event.type == "assistant/message"][-1].data["text"] == "我恢复了"
    assert types[-1] == "session/end"


def test_wake_notice_without_tools(archive):
    kernel = make_kernel(archive, [Completion(text="我只是说句话")])
    kernel.run()
    events = read_events(archive.events_path)
    wake = [event for event in events if event.type == "system/message" and event.data.get("kind") == "wake"]
    assert len(wake) == 1


def test_user_message_from_inbox(archive):
    Control(archive).push_message("记得写测试")
    kernel = make_kernel(archive, [Completion(text="收到")])
    kernel.run()
    events = read_events(archive.events_path)
    users = [event for event in events if event.type == "user/message"]
    assert users[0].data["text"] == "你开始运行了，欢迎"
    assert users[0].data["source"] == "init"
    assert any(event.data["text"] == "记得写测试" and event.data.get("source") == "control" for event in users)


def test_tool_call_limit(archive):
    calls = [
        ToolCall(id=f"c{index}", name="list_dir", arguments="{}")
        for index in range(20)
    ]
    kernel = make_kernel(archive, [Completion(text="", tool_calls=calls)])
    kernel.run()
    results = [event for event in read_events(archive.events_path) if event.type == "tool/result"]
    assert len(results) == 20
    assert sum(1 for event in results if event.data["ok"]) == 16
    rejected = [event for event in results if not event.data["ok"]]
    assert len(rejected) == 4
    assert "TOOL_CALL_LIMIT" in rejected[0].data["output"]


def test_welcome_message_written_once(archive):
    for _ in range(2):
        Kernel(
            archive.root,
            client=ScriptedClient([Completion(text="ok")]),
            max_turns=1,
            echo=False,
            error_backoff_base=0,
        ).run()
    init_users = [
        event
        for event in read_events(archive.events_path)
        if event.type == "user/message" and event.data.get("source") == "init"
    ]
    assert len(init_users) == 1


def test_delay_event_appended(tmp_path):
    from fishkernel.archive import create_archive

    archive = create_archive(tmp_path / "delay", name="delay", api_key="sk-test", turn_delay=0.05)
    Kernel(
        archive.root,
        client=ScriptedClient([Completion(text="ok")]),
        max_turns=1,
        echo=False,
        error_backoff_base=0,
    ).run()
    delays = [event for event in read_events(archive.events_path) if event.type == "delay"]
    assert delays and delays[0].data["seconds"] > 0


def test_no_delay_event_when_no_sleep(archive):
    make_kernel(archive, [Completion(text="ok")]).run()
    delays = [event for event in read_events(archive.events_path) if event.type == "delay"]
    assert delays == []
