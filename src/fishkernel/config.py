"""basic.instance 解析。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .constants import (
    DEFAULT_API_BASE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_TURN_DELAY,
    CompactionBudget,
    ModelSpec,
    compaction_budget,
    model_spec,
)

API_KEY_ENV_VARS = ("FISHKERNEL_API_KEY", "DEEPSEEK_API_KEY")
_PLACEHOLDER_KEYS = {"", "sk-...", "...", "changeme"}


class ConfigError(Exception):
    """配置缺失或非法。"""


@dataclass
class InstanceConfig:
    name: str
    model: str
    api_base: str
    api_key: str | None
    max_tokens: int
    turn_delay: float
    temperature: float
    effort: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def spec(self) -> ModelSpec:
        return model_spec(self.model)

    @property
    def budget(self) -> CompactionBudget:
        return compaction_budget(self.spec, self.max_tokens)

    def require_api_key(self) -> str:
        key = self.api_key or _env_api_key()
        if not key or key.strip().lower() in _PLACEHOLDER_KEYS:
            raise ConfigError(
                "缺少 API Key：请在 basic.instance 写 api_key = sk-...，"
                "或设置环境变量 FISHKERNEL_API_KEY / DEEPSEEK_API_KEY"
            )
        return key.strip()


def _env_api_key() -> str | None:
    for name in API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def parse_instance(text: str) -> dict[str, str]:
    """解析 `key = value` 文本；# 起始为注释；重复 key 后者覆盖。"""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            raise ConfigError(f"basic.instance 第 {lineno} 行不是 key = value 格式：{line!r}")
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            raise ConfigError(f"basic.instance 第 {lineno} 行缺少 key")
        values[key] = value
    return values


def load_config(archive_root: Path) -> InstanceConfig:
    path = Path(archive_root) / "basic.instance"
    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取 {path} 失败：{exc}") from exc
    return config_from_values(parse_instance(text), source=str(path))


def config_from_values(values: dict[str, str], *, source: str = "basic.instance") -> InstanceConfig:
    known = {"name", "model", "api_base", "api_key", "max_tokens", "turn_delay", "temperature", "effort"}
    extra = {k: v for k, v in values.items() if k not in known}

    model = values.get("model", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        spec = model_spec(model)
    except ValueError as exc:
        raise ConfigError(f"{source}: {exc}") from None

    def _int(key: str, default: int) -> int:
        raw = values.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{source}: {key} 必须是整数，实际是 {raw!r}") from None

    def _float(key: str, default: float) -> float:
        raw = values.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{source}: {key} 必须是数字，实际是 {raw!r}") from None

    max_tokens = _int("max_tokens", DEFAULT_MAX_TOKENS)
    if max_tokens <= 0:
        raise ConfigError(f"{source}: max_tokens 必须为正")
    if max_tokens > spec.max_output_tokens:
        max_tokens = spec.max_output_tokens

    turn_delay = _float("turn_delay", DEFAULT_TURN_DELAY)
    if turn_delay < 0:
        raise ConfigError(f"{source}: turn_delay 不能为负")

    temperature = _float("temperature", DEFAULT_TEMPERATURE)
    if not 0 <= temperature <= 2:
        raise ConfigError(f"{source}: temperature 必须在 [0, 2] 内")

    effort = values.get("effort") or None
    if effort is not None and effort not in spec.effort_levels:
        raise ConfigError(f"{source}: effort 必须是 {spec.effort_levels} 之一，实际是 {effort!r}")

    api_key = values.get("api_key") or None
    if api_key and api_key.strip().lower() in _PLACEHOLDER_KEYS:
        api_key = None

    return InstanceConfig(
        name=(values.get("name") or "agent").strip() or "agent",
        model=model,
        api_base=(values.get("api_base") or DEFAULT_API_BASE).strip() or DEFAULT_API_BASE,
        api_key=api_key,
        max_tokens=max_tokens,
        turn_delay=turn_delay,
        temperature=temperature,
        effort=effort,
        extra=extra,
    )
