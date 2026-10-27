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

# 一张普通截图的视觉 token 估算值。
# 关键教训：绝不能按 base64 字符数估算图片开销——
# 一张 1100x649 的 PNG 的 data URL 有 60 万字符，按文本算约 18 万 token，
# 而模型实际只计几百到一千多。误差 200 倍，会直接把压缩的保留预算吃光，
# 导致「一个单元就超预算 → 兜底 kept=1 → 前面所有历史被摘要掉」。
IMAGE_TOKEN_ESTIMATE = 1000
_DATA_URL_RE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


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

    # 兜底：任何路径带进来的 data URL 都按「一张图」计价，绝不按 base64 长度算
    image_cost = 0
    if "data:image/" in text:
        text, removed = _DATA_URL_RE.subn("", text)
        image_cost = removed * IMAGE_TOKEN_ESTIMATE
        if not text:
            return image_cost

    # re 的 C 实现只扫一遍，比 Python 层逐字符快一个数量级
    if text.isascii():
        return math.ceil(len(text) * 0.3) + image_cost

    total_len = len(text)
    # sub 把非 ASCII 换成空，剩下的就是 ASCII 部分
    ascii_count = len(_NON_ASCII_RE.sub("", text))
    cjk_count = len(_CJK_RE.findall(text))
    other_count = total_len - ascii_count - cjk_count

    total = ascii_count * 0.3 + cjk_count * 0.6 + other_count * 0.5
    return math.ceil(total) + image_cost


def estimate_message(message: dict[str, Any]) -> int:
    total = PER_MESSAGE_OVERHEAD
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_text(content)
    elif isinstance(content, list):
        # 逐块处理：image_url 块按「一张图」计价，其余文本照常估算。
        # 早期实现是 json.dumps 整个列表再估文本，图片的 base64 会被当文本算。
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                total += IMAGE_TOKEN_ESTIMATE
            else:
                total += estimate_text(
                    json.dumps(part, ensure_ascii=False) if not isinstance(part, str) else part
                )
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


# ---- 用量与成本统计 ----
# DeepSeek 定价（元 / 百万 token），按 2026 年标准档。改价时只需改这里。
PRICE_CACHE_HIT = 0.5      # 命中缓存的输入
PRICE_CACHE_MISS = 4.0     # 未命中的输入
PRICE_OUTPUT = 12.0        # 输出（含推理 token）


@dataclass
class UsageTotals:
    """把一串 assistant/message 的 usage 聚合成可读的用量与成本。"""

    calls: int = 0
    prompt_tokens: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls_without_cache_data: int = 0

    @property
    def cache_hit_rate(self) -> float:
        denom = self.cache_hit + self.cache_miss
        return (self.cache_hit / denom) if denom else 0.0

    @property
    def cost_yuan(self) -> float:
        """按当前定价估算花费。

        注意：大量老记录没有缓存字段（实现里曾经漏记）。直接把这些 prompt
        全按未命中价算会严重高估——实测命中率是 99.9%，全价算能差出几十倍。
        所以对「无缓存数据的调用」用已观测到的命中率来估，没有观测值时
        退化为保守的 0.9（DeepSeek 长前缀场景实测值）。
        """
        # 有缓存数据的部分：按实际命中/未命中分别计价
        hit = self.cache_hit
        miss = self.cache_miss
        # 无缓存数据的部分：用观测命中率外推
        unknown_prompt = max(0, self.prompt_tokens - hit - miss)
        if unknown_prompt:
            rate = self.cache_hit_rate if (hit + miss) else 0.9
            hit += int(unknown_prompt * rate)
            miss += int(unknown_prompt * (1 - rate))
        return (
            hit * PRICE_CACHE_HIT
            + miss * PRICE_CACHE_MISS
            + self.output_tokens * PRICE_OUTPUT
        ) / 1_000_000

    def summary(self) -> str:
        parts = [f"{self.calls} 次调用"]
        if self.prompt_tokens:
            parts.append(f"输入 {self.prompt_tokens:,}")
        # 只在「有缓存样本」时才报命中率，并显式标出样本范围，
        # 否则 42 条样本的 99.9% 会被误读成全局比例。
        sampled = self.calls - self.calls_without_cache_data
        if sampled:
            if sampled < self.calls:
                parts.append(f"缓存命中 {self.cache_hit_rate:.0%}（{sampled}/{self.calls} 条有数据）")
            else:
                parts.append(f"缓存命中 {self.cache_hit_rate:.0%}")
        parts.append(f"输出 {self.output_tokens:,}")
        parts.append(f"约 ¥{self.cost_yuan:.2f}")
        return " · ".join(parts)


def fold_usage(events) -> UsageTotals:
    """从事件里折叠出累计用量。events 需含 assistant/message 的 usage。"""
    return fold_usage_into(UsageTotals(), events)


def fold_usage_into(totals: UsageTotals, events) -> UsageTotals:
    """把一批事件累加进已有的 totals（就地修改并返回）。

    Web 控制台每秒轮询一次，事件日志会涨到几十万条；有了这个函数就能
    只折叠「新增的那几条」，而不必每次都把全量事件重算一遍。
    """
    for event in events:
        if event.type != "assistant/message":
            continue
        usage = event.data.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        if not isinstance(prompt, int):
            continue
        totals.calls += 1
        totals.prompt_tokens += prompt
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if isinstance(hit, int):
            totals.cache_hit += hit
        if isinstance(miss, int):
            totals.cache_miss += miss
        if hit is None:
            totals.calls_without_cache_data += 1
        output = usage.get("completion_tokens")
        if isinstance(output, int):
            totals.output_tokens += output
        reasoning = usage.get("reasoning_tokens")
        if isinstance(reasoning, int):
            totals.reasoning_tokens += reasoning
    return totals
