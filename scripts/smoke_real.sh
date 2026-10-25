#!/usr/bin/env bash
# 真实 API 端到端联调（限轮次）。用法：FISHKERNEL_KEY=sk-... bash scripts/smoke_real.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${FISHKERNEL_KEY:?需要 FISHKERNEL_KEY 环境变量}"
ARCHIVE=/tmp/fk-smoke
rm -rf "$ARCHIVE"

.venv/bin/fishkernel new "$ARCHIVE" --name 联调体 --api-key "$FISHKERNEL_KEY" --max-tokens 4096 --turn-delay 1

# 联调只做只读操作，避免测试机上产生副作用
printf '\n\n## 本轮附加约束\n这是一次限 2 轮的联调测试：只做只读操作（list_dir / read_file），最多调用一次工具；不要执行会修改系统的命令，也不要 sleep。\n' >> "$ARCHIVE/Memory/Agent.txt"

echo "== 跑 2 轮 =="
set +e
timeout 240 .venv/bin/fishkernel run "$ARCHIVE" --max-turns 2 --quiet
code=$?
set -e
echo "run exit=$code"

echo "== 状态 =="
.venv/bin/fishkernel status "$ARCHIVE" --tail 25

if grep -q '"type":"assistant/message"' "$ARCHIVE/Runtime/events.jsonl"; then
  echo "REAL_API_OK"
else
  echo "REAL_API_FAIL"
  exit 1
fi
