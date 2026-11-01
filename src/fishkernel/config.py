"""basic.instance 解析。"""

from __future__ import annotations

import os
import json
import math
import re
import hashlib
from dataclasses import dataclass, field, replace
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

    context_window: int | None = None
    max_output_tokens: int | None = None
    thinking: str = "auto"
    options: dict = field(default_factory=dict)

    @property
    def revision(self) -> str:
        # API credentials are excluded; only request behavior identifies a profile.
        value = {key: getattr(self, key) for key in ("model", "api_base", "max_tokens", "temperature",
                 "effort", "context_window", "max_output_tokens", "thinking", "options")}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]

    @property
    def effective_effort(self):
        return self.options.get("thinking_modes", {}).get(self.thinking, {}).get("reasoning_effort", self.effort)

    @property
    def spec(self) -> ModelSpec:
        original = model_spec(self.model)
        return replace(original, context_window=self.context_window or original.context_window,
                       max_output_tokens=self.max_output_tokens or original.max_output_tokens)

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
        # Comments begin after whitespace outside quoted/JSON values; URL fragments survive.
        if not value.startswith(('"', "'", "{", "[")):
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
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
    values = parse_instance(text)
    config = config_from_values(values, source=str(path))
    options_path = Path(archive_root) / "model-options.json"
    if options_path.exists():
        try:
            options = json.loads(options_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Invalid model-options.json: {exc}") from exc
        config.options = validate_options(options)
    validate_thinking(config)
    return config


def config_from_values(values: dict[str, str], *, source: str = "basic.instance") -> InstanceConfig:
    known = {"name", "model", "api_base", "api_key", "max_tokens", "turn_delay", "temperature", "effort", "reasoning_effort", "context_window", "max_output_tokens", "thinking"}
    extra = {k: v for k, v in values.items() if k not in known}

    model = values.get("model", DEFAULT_MODEL).strip()
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
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("non-finite")
            return value
        except ValueError:
            raise ConfigError(f"{source}: {key} 必须是数字，实际是 {raw!r}") from None

    window = _int("context_window", spec.context_window)
    output_limit = _int("max_output_tokens", min(spec.max_output_tokens, max(1, window // 2)))
    if window < 1024 or output_limit <= 0 or output_limit >= window:
        raise ConfigError("Require context_window >= 1024 and 0 < max_output_tokens < context_window")
    spec = replace(spec, context_window=window, max_output_tokens=output_limit)
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

    effort = values.get("reasoning_effort") or values.get("effort") or None
    thinking = values.get("thinking", "auto")
    if thinking not in {"auto", "on", "off"}:
        raise ConfigError("thinking must be auto/on/off")

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
        context_window=window,
        max_output_tokens=output_limit,
        thinking=thinking,
        extra=extra,
    )


def validate_options(options: object) -> dict:
    if not isinstance(options, dict):
        raise ConfigError("model-options.json must be an object")
    try:
        json.dumps(options, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigError("Model options must contain finite JSON values") from exc
    allowed = {"extra_body", "thinking_modes", "output_parameter", "send_temperature", "tools", "vision",
               "reasoning_field", "require_reasoning", "prices"}
    if set(options) - allowed:
        raise ConfigError(f"Unknown model options: {sorted(set(options) - allowed)}")
    reserved = {"model", "messages", "tools", "tool_choice", "stream", "max_tokens", "max_completion_tokens",
                "temperature", "reasoning_effort"}
    def body(value):
        if not isinstance(value, dict) or set(value) & reserved:
            raise ConfigError("extra_body must be an object without core request fields")
    body(options.get("extra_body", {}))
    modes = options.get("thinking_modes", {})
    if not isinstance(modes, dict) or set(modes) - {"on", "off"}:
        raise ConfigError("thinking_modes must map on/off")
    for mapping in modes.values():
        if not isinstance(mapping, dict) or set(mapping) - {"extra_body", "reasoning_effort"}:
            raise ConfigError("Invalid thinking mode mapping")
        body(mapping.get("extra_body", {}))
        if "reasoning_effort" in mapping and not isinstance(mapping["reasoning_effort"], str):
            raise ConfigError("Mapped reasoning_effort must be a string")
    for key in ("send_temperature", "tools", "vision", "require_reasoning"):
        if key in options and not isinstance(options[key], bool):
            raise ConfigError(f"{key} must be a boolean")
    if not isinstance(options.get("output_parameter", "max_tokens"), str) or options.get("output_parameter", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
        raise ConfigError("Invalid output_parameter")
    if options.get("reasoning_field") is not None and (not isinstance(options["reasoning_field"], str) or options["reasoning_field"] not in {"reasoning_content", "reasoning"}):
        raise ConfigError("Invalid reasoning_field")
    if options.get("require_reasoning") and not options.get("reasoning_field"):
        raise ConfigError("require_reasoning needs a reasoning_field")
    if "prices" in options:
        prices = options["prices"]
        if not isinstance(prices, dict) or set(prices) - {"input", "output", "cache_input", "currency"}:
            raise ConfigError("Invalid prices mapping")
        if not {"input", "output", "currency"} <= set(prices):
            raise ConfigError("prices requires input/output rates per million tokens and currency")
        if not isinstance(prices["currency"], str) or not re.fullmatch(r"[A-Z]{3}", prices["currency"]):
            raise ConfigError("prices.currency must be a three-letter currency code")
        for key in ("input", "output", "cache_input"):
            if key in prices and (type(prices[key]) not in (int, float) or prices[key] < 0):
                raise ConfigError("Token prices must be finite nonnegative numbers")
    return options


def validate_thinking(config: InstanceConfig) -> None:
    if config.thinking != "auto" and config.thinking not in config.options.get("thinking_modes", {}):
        raise ConfigError("thinking on/off requires a corresponding model-options.json mapping")
