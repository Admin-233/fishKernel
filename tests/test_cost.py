"""用量与成本统计测试。"""
from __future__ import annotations

from fishkernel.events import Event
from fishkernel.tokens import (
    PRICE_CACHE_HIT,
    PRICE_CACHE_MISS,
    PRICE_OUTPUT,
    UsageTotals,
    fold_usage,
)


def msg(usage: dict) -> Event:
    return Event(1, "", "assistant/message", {"usage": usage})


def test_fold_usage_sums_buckets():
    events = [
        msg({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800,
             "prompt_cache_miss_tokens": 200, "completion_tokens": 50}),
        msg({"prompt_tokens": 2000, "prompt_cache_hit_tokens": 1500,
             "prompt_cache_miss_tokens": 500, "completion_tokens": 80}),
    ]
    t = fold_usage(events)
    assert t.calls == 2
    assert t.prompt_tokens == 3000
    assert t.cache_hit == 2300
    assert t.cache_miss == 700
    assert t.output_tokens == 130
    assert t.calls_without_cache_data == 0
    assert abs(t.cache_hit_rate - 2300 / 3000) < 1e-9


def test_cost_uses_separate_prices():
    t = UsageTotals(
        calls=1, prompt_tokens=1_000_000,
        cache_hit=900_000, cache_miss=100_000,
        output_tokens=100_000,
    )
    expected = (
        900_000 * PRICE_CACHE_HIT
        + 100_000 * PRICE_CACHE_MISS
        + 100_000 * PRICE_OUTPUT
    ) / 1_000_000
    assert abs(t.cost_yuan - expected) < 1e-9


def test_cost_estimates_unsampled_calls_by_observed_rate():
    """没有缓存字段的老记录不能按全价算，否则会高出几十倍。"""
    # 42 条有数据、命中率 99.9%
    t = UsageTotals(
        calls=1000, prompt_tokens=10_000_000,
        cache_hit=999_000, cache_miss=1_000,
        calls_without_cache_data=958,
    )
    full_price = 10_000_000 * PRICE_CACHE_MISS / 1_000_000
    # 全按未命中价会是 full_price；按 99.9% 命中率外推后应小得多
    assert t.cost_yuan < full_price / 5, (
        f"无采样部分应按观测命中率外推，实际 {t.cost_yuan:.2f} vs 全价 {full_price:.2f}"
    )


def test_cost_falls_back_to_ninety_percent_without_samples():
    """完全没有缓存样本时，用保守的 0.9 命中率，而不是 0。"""
    t = UsageTotals(calls=10, prompt_tokens=1_000_000, calls_without_cache_data=10)
    assert t.cache_hit_rate == 0.0        # 没有样本，比率无从谈起
    expected = (900_000 * PRICE_CACHE_HIT + 100_000 * PRICE_CACHE_MISS) / 1_000_000
    # 容差 1e-4：int() 截断会让外推的 token 数少 1，金额差约 4e-6
    assert abs(t.cost_yuan - expected) < 1e-4


def test_summary_marks_partial_sampling():
    """只有部分调用有缓存数据时，summary 必须标出样本范围，避免误读。"""
    t = UsageTotals(
        calls=100, prompt_tokens=1_000_000,
        cache_hit=900, cache_miss=100,
        output_tokens=1000, calls_without_cache_data=90,
    )
    text = t.summary()
    assert "10/100" in text, f"应标出 10/100 条有数据，实际：{text}"


def test_fold_usage_skips_events_without_usage():
    events = [
        Event(1, "", "user/message", {"text": "hi"}),
        msg({"prompt_tokens": 100, "completion_tokens": 10}),
        Event(3, "", "tool/result", {"ok": True}),
    ]
    t = fold_usage(events)
    assert t.calls == 1
    assert t.prompt_tokens == 100
