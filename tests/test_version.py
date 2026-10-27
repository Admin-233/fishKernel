"""版本号一致性测试。

背景：版本号原本写在两处（`__init__.py` 和 `pyproject.toml`），CHANGELOG 已经写到
v0.4.3 时两处仍停在 0.3.0，`fishkernel --version` 一直报旧版本。
现在 `__init__.py` 是唯一来源，pyproject 用 setuptools 的 dynamic attr 读它。
这个测试保证三件事不脱节：包版本、pyproject 配置、CHANGELOG 里出现过这个版本。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import fishkernel

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_pep440_ish():
    assert re.fullmatch(r"\d+\.\d+\.\d+([-.+].*)?", fishkernel.__version__), fishkernel.__version__


def test_pyproject_reads_version_from_package():
    """pyproject 不能自己写死 version，否则又会和包内版本脱节。"""
    raw = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    project = data["project"]

    assert "version" not in project, "pyproject 不应写死 version，应使用 dynamic"
    assert project.get("dynamic") == ["version"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "fishkernel.__version__"}


def test_changelog_mentions_current_version():
    """当前版本必须出现在 CHANGELOG 顶部几条里——防止改了版本忘了写日志。"""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## (v[\d.]+)", changelog, re.MULTILINE)
    assert versions, "CHANGELOG 里没有任何版本标题"
    assert f"v{fishkernel.__version__}" == versions[0], (
        f"CHANGELOG 最新版本是 {versions[0]}，但包版本是 v{fishkernel.__version__}"
    )
