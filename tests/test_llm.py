"""LLM 响应解析与错误处理。

为什么单独补这个：llm.py 是所有 API 调用的必经路径，但此前
完全没有测试（覆盖率 33%，未覆盖的正是 HTTP 请求与响应解析那段）。

这里不测真实网络，只测「API 返回各种东西时解析层怎么反应」——
尤其是畸形响应，那是最容易出问题的地方。
"""

from __future__ import annotations

import json

import pytest

from fishkernel.llm import LLMError, _error_text, _parse_completion


# ---- 测试替身：模拟 openai SDK 的返回结构 -------------------------------------


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt=10, completion=5, total=15):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _Response:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class _ToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = type("Fn", (), {"name": name, "arguments": arguments})()


# ---- 正常解析 -----------------------------------------------------------------


def test_parses_plain_text_reply():
    out = _parse_completion(_Response([_Choice(_Msg("你好"))]), "m")
    assert out.text == "你好"
    assert out.tool_calls == []


def test_parses_usage():
    out = _parse_completion(_Response([_Choice(_Msg("x"))], _Usage()), "m")
    assert out.usage["prompt_tokens"] == 10
    assert out.usage["completion_tokens"] == 5


def test_parses_tool_calls():
    raw = _ToolCall("call_1", "shell", json.dumps({"command": "ls"}))
    out = _parse_completion(_Response([_Choice(_Msg(None, [raw]))]), "m")
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].name == "shell"
    assert json.loads(out.tool_calls[0].arguments) == {"command": "ls"}


def test_tool_call_arguments_already_dict_becomes_empty_string():
    """arguments 不是字符串时给 "{}"，不要让下游拿到非字符串。"""
    raw = _ToolCall("c", "shell", {"command": "ls"})
    out = _parse_completion(_Response([_Choice(_Msg(None, [raw]))]), "m")
    assert out.tool_calls[0].arguments == "{}"


# ---- 畸形响应：必须报错，不能静默返回空 ---------------------------------------


def test_empty_choices_raises():
    with pytest.raises(LLMError) as excinfo:
        _parse_completion(_Response([]), "m")
    assert excinfo.value.code == "EMPTY_RESPONSE"


def test_none_response_raises():
    with pytest.raises(LLMError) as excinfo:
        _parse_completion(None, "m")
    assert excinfo.value.code == "EMPTY_RESPONSE"


def test_missing_choices_attribute_raises():
    """完全没有 choices 属性的对象，也要报 EMPTY_RESPONSE 而不是 AttributeError。"""
    with pytest.raises(LLMError) as excinfo:
        _parse_completion(object(), "m")
    assert excinfo.value.code == "EMPTY_RESPONSE"


def test_none_content_becomes_empty_text():
    out = _parse_completion(_Response([_Choice(_Msg(None))]), "m")
    assert out.text == ""


def test_usage_none_gives_empty_dict():
    out = _parse_completion(_Response([_Choice(_Msg("x"))], None), "m")
    assert out.usage == {}


def test_usage_with_non_int_values_is_ignored():
    class Weird:
        prompt_tokens = "十"      # 不是 int
        completion_tokens = 5
        total_tokens = None

    out = _parse_completion(_Response([_Choice(_Msg("x"))], Weird()), "m")
    assert out.usage == {"completion_tokens": 5}


# ---- 错误信息截断 -------------------------------------------------------------


def test_error_text_truncates_long_message():
    long = "x" * 5000
    assert len(_error_text(Exception(long))) <= 1000


def test_error_text_includes_body():
    exc = Exception("boom")
    exc.body = {"error": "bad key"}
    text = _error_text(exc)
    assert "boom" in text
    assert "bad key" in text
