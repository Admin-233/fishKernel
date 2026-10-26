"""常量与模型参数表。

模型参数以 DeepSeek-V4.1-Flash（1017）为准，取自 api.deepseek.com/models:
  context_window = 1048576, max_output_tokens = 393216, effort: low/high/max。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TURN_DELAY = 1.0
DEFAULT_TEMPERATURE = 1.0

# 压缩预算（与 dsh-compaction-basic 同一套算法）
COMPACT_THRESHOLD_RATIO = 0.6
COMPACT_HEADROOM_TOKENS = 65_536
COMPACT_RETAIN_RATIO = 0.16
SUMMARY_MAX_TOKENS_CAP = 8_192

# 工具与 IO 上限
MAX_TOOL_CALLS_PER_TURN = 16
MAX_READ_BYTES = 256 * 1024
MAX_INLINE_RESULT_CHARS = 32_768
MAX_MEMORY_BYTES = 64 * 1024
MAX_SLEEP_SECONDS = 3_600
MAX_LIST_ENTRIES = 1_000
JOB_OUTPUT_CHUNK = 32_768

# 溢出与任务保留
SPILL_KEEP_FILES = 200
SPILL_KEEP_BYTES = 50 * 1024 * 1024
JOBS_KEEP_DIRS = 100

# 网络
LLM_TIMEOUT_SECONDS = 300.0
LLM_RETRIES = 4
RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    context_window: int
    max_output_tokens: int
    effort_levels: tuple[str, ...]
    default_effort: str


MODEL_SPECS: dict[str, ModelSpec] = {
    "deepseek-flash": ModelSpec(
        id="deepseek-flash",
        name="DeepSeek-V4.1-Flash (1017)",
        context_window=1_048_576,
        max_output_tokens=393_216,
        effort_levels=("low", "high", "max"),
        default_effort="high",
    ),
    "deepseek-v4-pro": ModelSpec(
        id="deepseek-v4-pro",
        name="DeepSeek-V4-Pro",
        context_window=1_048_576,
        max_output_tokens=393_216,
        effort_levels=("low", "high", "max"),
        default_effort="high",
    ),
}


def model_spec(model_id: str) -> ModelSpec:
    try:
        return MODEL_SPECS[model_id]
    except KeyError:
        raise ValueError(f"未知模型 {model_id!r}；可选：{', '.join(sorted(MODEL_SPECS))}") from None


@dataclass(frozen=True)
class CompactionBudget:
    """压缩的阈值与逐字保留量（token）。"""

    threshold: int
    retain: int

    @property
    def window(self) -> int:
        return self.threshold  # 仅便于日志阅读


def compaction_budget(spec: ModelSpec, max_tokens: int) -> CompactionBudget:
    """阈值 = floor(min(W*r, W-O-B))；保留 = floor(r_retain*(W-O))。"""
    w = spec.context_window
    o = max(0, min(int(max_tokens), spec.max_output_tokens))
    threshold = math.floor(min(w * COMPACT_THRESHOLD_RATIO, w - o - COMPACT_HEADROOM_TOKENS))
    threshold = max(threshold, 1)
    retain = math.floor(COMPACT_RETAIN_RATIO * (w - o))
    retain = max(0, min(retain, threshold - 1))
    return CompactionBudget(threshold=threshold, retain=retain)
