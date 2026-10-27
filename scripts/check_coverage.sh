#!/usr/bin/env bash
# 覆盖率检查：守住"我新增的模块"不被无声劣化。
#
# 为什么需要：测试数量（214 项）不等于覆盖充分。一个模块可能有很多测试，
# 但关键分支全都没走到。覆盖率能暴露这类问题。
#
# 基线（2026-10-27 实测）：
#   web.py 86% / trim.py 95% / compress.py 93% / messages.py 94%
#   整体 73%（llm.py 33%、terminal.py 19% 属于既有代码，非我改动）
#
# 用法：check_coverage.sh [最低百分比]
set -uo pipefail
FLOOR="${1:-85}"
VENV="$HOME/桌面/fishKernel/.venv"

cd "$HOME/桌面/fishKernel" || exit 2

if ! "$VENV/bin/python" -c "import coverage" 2>/dev/null; then
  echo "✗ 未安装 coverage：$VENV/bin/pip install coverage" >&2
  exit 2
fi

"$VENV/bin/python" -m coverage run --source=src/fishkernel -m pytest -q >/dev/null 2>&1
REPORT="$("$VENV/bin/python" -m coverage report 2>/dev/null)"

echo "=== 我新增/修改的模块 ==="
FAIL=0
for MOD in web trim compress messages; do
  LINE="$(echo "$REPORT" | grep "/$MOD.py" | head -1)"
  PCT="$(echo "$LINE" | awk '{gsub(/%/,"",$NF); print $NF}')"
  if [[ -z "$PCT" ]]; then
    echo "  ⚠ $MOD.py 未找到"; continue
  fi
  if [[ "$PCT" -ge "$FLOOR" ]]; then
    echo "  ✓ $MOD.py ${PCT}%"
  else
    echo "  ✗ $MOD.py ${PCT}%（低于底线 ${FLOOR}%）"; FAIL=1
  fi
done

TOTAL="$(echo "$REPORT" | grep "^TOTAL" | awk '{gsub(/%/,"",$NF); print $NF}')"
echo
echo "整体覆盖率：${TOTAL}%"
echo "（低于底线的模块：web/trim/compress/messages 之外的不计入判定）"

echo
[[ "$FAIL" == "0" ]] && echo "覆盖率检查通过。" || { echo "覆盖率不足。" >&2; exit 1; }
