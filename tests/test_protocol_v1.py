import json
from types import SimpleNamespace
import pytest

from fishkernel.config import config_from_values, parse_instance, ConfigError, load_config
from fishkernel.events import Event
from fishkernel.messages import build_messages, message_units, ProjectionError
from fishkernel.compress import _split_by_tokens
from fishkernel.llm import OpenAIClient


def event(seq, kind, **data):
    return Event(seq, "", kind, data)


def test_interleaved_results_and_images_preserve_protocol():
    events = [event(1, "assistant/message", tool_calls=[
        {"id": "a", "name": "memory", "arguments": "{}"},
        {"id": "b", "name": "read_file", "arguments": "{}"}]),
        event(2, "memory/write", mode="append", content="fact"),
        event(3, "tool/result", call_id="a", output="saved",
              images=[{"url": "data:image/png;base64,AA=="}]),
        event(4, "system/message", text="notice"),
        event(5, "tool/result", call_id="b", output="read"),
        event(6, "user/message", text="next")]
    messages = build_messages(events, "system")
    assert [m["role"] for m in messages[:5]] == ["system", "assistant", "tool", "tool", "user"]
    assert messages[2]["content"] == "saved"
    assert messages[3]["content"] == "read"
    assert messages[-1]["content"] == "next"
    chunks = _split_by_tokens(messages[1:], 1)
    assert [m["role"] for m in chunks[0]] == ["assistant", "tool", "tool"]
    for chunk in chunks:
        message_units(chunk)


def test_full_reasoning_and_truncated_legacy_detection():
    calls = [{"id": "a", "name": "tool", "arguments": "{}"}]
    events = [event(1, "assistant/message", tool_calls=calls, reasoning="r" * 5000, reasoning_complete=True)]
    messages = build_messages(events, "", reasoning_field="reasoning_content", require_reasoning=True)
    assert messages[1]["reasoning_content"] == "r" * 5000
    events[0].data.update(reasoning="r" * 4000)
    events[0].data.pop("reasoning_complete")
    with pytest.raises(ProjectionError):
        build_messages(events, "", reasoning_field="reasoning_content", require_reasoning=True)


def test_generic_request_parameters_and_thinking_mapping():
    config = config_from_values({"model": "organization/model:1", "effort": "custom", "thinking": "on"})
    config.options = {"output_parameter": "max_completion_tokens", "send_temperature": False,
                      "thinking_modes": {"on": {"extra_body": {"thinking": {"type": "enabled"}}}}}
    requests = []
    def create(**payload):
        requests.append(payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop")], usage=None)
    client = OpenAIClient(config)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.complete([{"role": "user", "content": "hello"}])
    payload = requests[0]
    assert payload["model"] == "organization/model:1"
    assert payload["reasoning_effort"] == "custom"
    assert payload["extra_body"]["thinking"]["type"] == "enabled"
    assert "temperature" not in payload and "max_tokens" not in payload
    assert payload["max_completion_tokens"] == config.max_tokens


def test_missing_thinking_mapping_and_reserved_extra_rejected(archive):
    with archive.config_path.open("a", encoding="utf-8") as handle:
        handle.write("\nthinking = on\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(archive.root)
    (archive.root / "model-options.json").write_text(json.dumps({"extra_body": {"model": "override"}}))
    with pytest.raises(ConfigError, match="core request"):
        load_config(archive.root)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_configuration_rejected(value):
    with pytest.raises(ConfigError):
        config_from_values({"turn_delay": value})


def test_inline_comments_keep_fragments():
    values = parse_instance('max_tokens = 1024 # comment\napi_base = https://host/#fragment\nname = "a # b"')
    assert values == {"max_tokens": "1024", "api_base": "https://host/#fragment", "name": "a # b"}
