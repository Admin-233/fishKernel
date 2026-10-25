#!/usr/bin/env bash
# 在测试机上跑：装依赖 + 全部测试。
# FISHKERNEL_SUDO_PASSWORD 用于无人值守的 sudo（可选）。
set -euo pipefail
cd "$(dirname "$0")/.."

SUDO_PW="${FISHKERNEL_SUDO_PASSWORD:-}"
run_sudo() {
  if [ -n "$SUDO_PW" ]; then
    echo "$SUDO_PW" | sudo -S -p '' "$@"
  else
    sudo -n "$@"
  fi
}

if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/pip ]; then
  rm -rf .venv
  if ! python3 -m venv .venv 2>/dev/null || [ ! -x .venv/bin/pip ]; then
    echo "== 安装 python3-venv =="
    run_sudo apt-get update -qq
    run_sudo apt-get install -y -qq python3-venv python3-pip
    rm -rf .venv
    python3 -m venv .venv
  fi
fi

.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e '.[dev]'
echo "== pytest =="
.venv/bin/python -m pytest -q
