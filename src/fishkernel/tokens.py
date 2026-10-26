"""token 计量。

事实：DeepSeek 的 chat/completions 响应里 `usage.prompt_tokens` 是精确值，
但 API 没有"发送前计数"接口（/tokenize、/count_tokens 均 404），官方提供的
离线分词器需要额外依赖。因此采用与 dsh-token-meter 相同的策略：

  - 以最近一次成功响应的精确 `prompt_tokens` 作为锚点；
  - 只对锚点之后新增的内容做启发式估算；
  - 用「实测 / 估算」的比例校准后续估算（EMA）。

这样绝大多数的上下文压力来自 API 的精确数字，误差只落在单个增量上。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .events import Event
from .messages import build_messages

import re

# CJK 统一表意文字、日文假名、韩文、全角标点（与 _char_cost 的判断保持一致）
_CJK_RE = re.compile(
    r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef\u3000-\u303f]"
)
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")

PER_MESSAGE_OVERHEAD = 4
PER_TOOL_OVERHEAD = 16


def _char_cost(ch: str) -> float:
    code = ord(ch)
    if code < 128:
        return 0.3
    # CJK 统一表意文字、日文假名、韩文、全角标点
    if (
        0x2E80 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
        or 0x3000 <= code <= 0x303F
    ):
        # 官方文档：1 个中文字符 ≈ 0.6 token
        return 0.6
    return 0.5


def estimate_text(text: str) -> int:
    """估算一段文本的 token 数。

    性能说明：早期实现是 `for ch in text: total += _char_cost(ch)`，
    在 45 万 token 的上下文上要跑 100 万次 Python 函数调用，实测 37ms/次，
    是每轮最大的 CPU 开销（占 66%）。这里改成按类别批量统计：
    ASCII / CJK / 其它各数一次，把逐字符的函数调用降到 3 次统计。
    """
    if not text:
        return 0

    # re 的 C 实现只扫一遍，比 Python 层逐字符快一个数量级
    if text.isascii():
        return math.ceil(len(text) * 0.3)

    total_len = len(text)
    # sub 把非 ASCII 换成空，剩下的就是 ASCII 部分
    ascii_count = len(_NON_ASCII_RE.sub("", text))
    cjk_count = len(_CJK_RE.findall(text))
    other_count = total_len - ascii_count - cjk_count

    total = ascii_count * 0.3 + cjk_count * 0.6 + other_count * 0.5
    return math.ceil(total)


def estimate_message(message: dict[str, Any]) -> int:
    total = PER_MESSAGE_OVERHEAD
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_text(content)
    elif isinstance(content, list):
        total += estimate_text(json.dumps(content, ensure_ascii=False))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += estimate_text(str(function.get("name", "")))
        total += estimate_text(str(function.get("arguments", "")))
        total += 8
    if message.get("tool_call_id"):
        total += 8
    return total


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message(message) for message in messages)


def estimate_tools(tools: list[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    total = PER_TOOL_OVERHEAD * len(tools)
    total += estimate_text(json.dumps(tools, ensure_ascii=False))
    return total


@dataclass(frozen=True)
class UsageAnchor:
    """最近一次请求的精确用量锚点。"""

    prompt_seq: int
    prompt_tokens: int
    system_tokens: int
    tools_tokens: int


class TokenMeter:
    """API 实测锚点 + 增量估算 + 比例校准。"""

    def __init__(self) -> None:
        self._anchor: UsageAnchor | None = None
        self._ratio = 1.0
        self._samples = 0

    @property
    def anchor(self) -> UsageAnchor | None:
        return self._anchor

    @property
    def ratio(self) -> float:
        return self._ratio

    @property
    def samples(self) -> int:
        return self._samples

    def invalidate(self) -> None:
        self._anchor = None

    # ---- 从历史恢复 ----
    def restore(self, events: list[Event], system_text: str = "", tools: list[dict[str, Any]] | None = None) -> None:
        """重启后从事件日志里找回锚点；压缩之后的锚点不再有效。"""
        anchor: UsageAnchor | None = None
        last_summary_seq = 0
        for event in events:
            if event.type == "compaction/summary":
                last_summary_seq = event.seq
            elif event.type == "assistant/message":
                usage = event.data.get("usage") or {}
                prompt_tokens = usage.get("prompt_tokens")
                prompt_seq = event.data.get("prompt_seq")
                if not isinstance(prompt_tokens, int) or prompt_tokens <= 0:
                    continue
                if not isinstance(prompt_seq, int):
                    prompt_seq = event.seq - 1
                anchor = UsageAnchor(
                    prompt_seq=prompt_seq,
                    prompt_tokens=prompt_tokens,
                    system_tokens=estimate_text(system_text),
                    tools_tokens=estimate_tools(tools),
                )
        if anchor is not None and anchor.prompt_seq >= last_summary_seq:
            self._anchor = anchor

    # ---- 记录一次真实请求 ----
    def record(
        self,
        *,
        prompt_seq: int,
        prompt_tokens: int,
        system_text: str,
        tools: list[dict[str, Any]] | None,
        estimated_prompt: int,
    ) -> None:
        if prompt_tokens <= 0:
            return
        self._anchor = UsageAnchor(
            prompt_seq=prompt_seq,
            prompt_tokens=prompt_tokens,
            system_tokens=estimate_text(system_text),
            tools_tokens=estimate_tools(tools),
        )
        if estimated_prompt > 0:
            ratio = prompt_tokens / estimated_prompt
            ratio = max(0.3, min(3.0, ratio))
            self._ratio = ratio if self._samples == 0 else 0.7 * self._ratio + 0.3 * ratio
            self._samples += 1

    # ---- 计算下一次请求的压力 ----
    def pressure(
        self,
        *,
        events: list[Event],
        messages: list[dict[str, Any]],
        system_text: str,
        tools: list[dict[str, Any]] | None,
    ) -> int:
        estimated_full = estimate_messages(messages) + estimate_tools(tools)
        anchor = self._anchor
        if anchor is None:
            return int(estimated_full * self._ratio)

        delta_events = [event for event in events if event.seq > anchor.prompt_seq]
        if any(event.type == "compaction/summary" for event in delta_events):
            # 压缩改写了历史，锚点作废
            self.invalidate()
            return int(estimated_full * self._ratio)

        delta_messages = [message for message in build_messages(delta_events, "") if message["role"] != "system"]
        delta_tokens = estimate_messages(delta_messages)
        delta_system = max(0, estimate_text(system_text) - anchor.system_tokens)
        delta_tools = max(0, estimate_tools(tools) - anchor.tools_tokens)
        return int(anchor.prompt_tokens + (delta_tokens + delta_system + delta_tools) * self._ratio)

    # ---- 观测 ----
    def describe(self) -> str:
        if self._anchor is None:
            return f"无锚点（估算比例 {self._ratio:.2f}，样本 {self._samples}）"
        return (
            f"锚点 prompt={self._anchor.prompt_tokens}（自 seq {self._anchor.prompt_seq} 起），"
            f"估算比例 {self._ratio:.2f}，样本 {self._samples}"
        )
