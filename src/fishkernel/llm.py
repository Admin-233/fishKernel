"""模型客户端：OpenAI SDK + 重试 + 超限识别 + mock。"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import InstanceConfig
from .constants import LLM_RETRIES, LLM_TIMEOUT_SECONDS, RETRY_BACKOFF_SECONDS

_CONTEXT_OVERFLOW_PATTERN = re.compile(
    r"context length|maximum context|context window|too many tokens|input is too long|超长",
    re.IGNORECASE,
)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Completion:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    model: str = ""

    @property
    def prompt_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens") or 0)


class LLMError(Exception):
    def __init__(self, message: str, *, code: str = "LLM_ERROR", retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ContextOverflowError(LLMError):
    def __init__(self, message: str):
        super().__init__(message, code="CONTEXT_WINDOW_EXCEEDED", retryable=False)


class LLMClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion: ...


class OpenAIClient:
    def __init__(self, config: InstanceConfig):
        self.config = config
        self.model = config.model
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - 依赖装好时不发生
                raise LLMError("缺少 openai 依赖，请先 pip install -e .", code="NO_SDK", retryable=False) from exc
            self._client = OpenAI(
                api_key=self.config.require_api_key(),
                base_url=self.config.api_base,
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=0,
            )
        return self._client

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        import openai

        client = self._ensure_client()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens or self.config.max_tokens),
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(LLM_RETRIES):
            try:
                response = client.chat.completions.create(**payload)
                return _parse_completion(response, self.model)
            except openai.APITimeoutError as exc:
                last_error = exc
                error = LLMError(f"请求超时：{exc}", code="TIMEOUT")
            except openai.APIConnectionError as exc:
                last_error = exc
                error = LLMError(f"网络错误：{exc}", code="CONNECTION")
            except openai.RateLimitError as exc:
                last_error = exc
                error = LLMError(f"限流：{exc}", code="RATE_LIMIT")
            except openai.BadRequestError as exc:
                text = _error_text(exc)
                if _CONTEXT_OVERFLOW_PATTERN.search(text):
                    raise ContextOverflowError(f"上下文超限：{text}") from exc
                raise LLMError(f"请求被拒绝（400）：{text}", code="BAD_REQUEST", retryable=False) from exc
            except openai.APIStatusError as exc:
                last_error = exc
                text = _error_text(exc)
                if getattr(exc, "status_code", 0) and exc.status_code >= 500:
                    error = LLMError(f"服务端错误：{text}", code="SERVER", retryable=True)
                else:
                    raise LLMError(f"API 错误：{text}", code="API_ERROR", retryable=False) from exc
            except Exception as exc:  # noqa: BLE001 - SDK 异常种类多，兜底成可重试
                last_error = exc
                error = LLMError(f"未知错误：{exc}", code="UNKNOWN")

            if not error.retryable or attempt == LLM_RETRIES - 1:
                raise error
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            time.sleep(delay + random.uniform(0, 0.5))

        raise LLMError(f"请求失败：{last_error}", code="EXHAUSTED")


def _error_text(exc: Exception) -> str:
    text = str(exc)
    body = getattr(exc, "body", None)
    if body is not None:
        text += f" body={body}"
    return text[:1000]


def _parse_completion(response: Any, model: str) -> Completion:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMError("API 返回空 choices", code="EMPTY_RESPONSE")
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise LLMError("API 返回缺少 message", code="EMPTY_RESPONSE")
    text = getattr(message, "content", None) or ""
    reasoning = ""
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value:
            reasoning = value
            break
    raw_calls = getattr(message, "tool_calls", None) or []
    tool_calls: list[ToolCall] = []
    for raw in raw_calls:
        function = getattr(raw, "function", None)
        name = getattr(function, "name", None) if function is not None else None
        arguments = getattr(function, "arguments", None) if function is not None else None
        if not name:
            continue
        tool_calls.append(
            ToolCall(
                id=str(getattr(raw, "id", "") or f"call_{len(tool_calls)}"),
                name=str(name),
                arguments=arguments if isinstance(arguments, str) else "{}",
            )
        )
    usage_raw = getattr(response, "usage", None)
    usage: dict[str, int] = {}
    if usage_raw is not None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage_raw, key, None)
            if isinstance(value, int):
                usage[key] = value
    return Completion(
        text=text,
        reasoning=reasoning,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=str(getattr(choice, "finish_reason", "stop") or "stop"),
        model=str(getattr(response, "model", model) or model),
    )


class ScriptedClient:
    """测试用：按脚本返回 Completion / 抛异常 / 调用可调用对象。"""

    def __init__(self, script: list[Any] | None = None, *, default_text: str = "（mock）我在。"):
        self.script = list(script or [])
        self.default_text = default_text
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        if not self.script:
            return Completion(text=self.default_text, usage={"prompt_tokens": 10, "completion_tokens": 2})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(messages)
        if isinstance(item, Completion):
            return item
        if isinstance(item, str):
            return Completion(text=item, usage={"prompt_tokens": 10, "completion_tokens": 2})
        raise TypeError(f"无法识别的脚本项：{type(item)!r}")
