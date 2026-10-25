from __future__ import annotations

import pytest

from fishkernel.config import ConfigError, config_from_values, load_config, parse_instance


def test_parse_instance_basic():
    text = """
# 注释
name = 小鱼
api_key = "sk-abc"
max_tokens = 1024
"""
    values = parse_instance(text)
    assert values["name"] == "小鱼"
    assert values["api_key"] == "sk-abc"
    assert values["max_tokens"] == "1024"


def test_parse_instance_rejects_no_equals():
    with pytest.raises(ConfigError):
        parse_instance("name 小鱼")


def test_config_defaults_and_budget():
    config = config_from_values({"name": "x"})
    assert config.model == "deepseek-flash"
    assert config.max_tokens == 8192
    assert config.spec.context_window == 1_048_576
    assert config.budget.threshold == 838_860
    assert config.budget.retain == 166_461


def test_config_rejects_unknown_model():
    with pytest.raises(ConfigError):
        config_from_values({"model": "gpt-4"})


def test_config_rejects_bad_number():
    with pytest.raises(ConfigError):
        config_from_values({"max_tokens": "很多"})


def test_config_requires_api_key(monkeypatch):
    config = config_from_values({"api_key": "sk-..."})
    monkeypatch.delenv("FISHKERNEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        config.require_api_key()

    monkeypatch.setenv("FISHKERNEL_API_KEY", "sk-env")
    assert config.require_api_key() == "sk-env"


def test_load_config_roundtrip(archive):
    config = load_config(archive.root)
    assert config.name == "测试体"
    assert config.api_key == "sk-test"
    assert config.turn_delay == 0


def test_effort_validation():
    with pytest.raises(ConfigError):
        config_from_values({"effort": "ultra"})
    assert config_from_values({"effort": "max"}).effort == "max"
