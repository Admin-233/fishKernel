#!/usr/bin/env bash
# 工具联调：让真实模型走一遍 spawn → wait_job → job_output。
# 用法：FISHKERNEL_KEY=sk-... bash scripts/smoke_tools.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${FISHKERNEL_KEY:?需要 FISHKERNEL_KEY 环境变量}"
ARCHIVE=/tmp/fk-smoke-tools
rm -rf "$ARCHIVE"

.venv/bin/fishkernel new "$ARCHIVE" --name 工具联调 --api-key "$FISHKERNEL_KEY" --max-tokens 4096 --turn-delay 1

cat >> "$ARCHIVE/Memory/Agent.txt" <<'EOF'

## 本轮任务（联调）
只做一件事：用 spawn 执行命令 `echo fishkernel-ok`，然后用 wait_job 等它结束、用 job_output 读取输出，
确认输出包含 fishkernel-ok 后简短汇报。不要执行任何其他命令，不要 sleep。
EOF

echo "== 跑 3 轮 =="
set +e
timeout 240 .venv/bin/fishkernel run "$ARCHIVE" --max-turns 3 --quiet
code=$?
set -e
echo "run exit=$code"

.venv/bin/fishkernel status "$ARCHIVE" --tail 25 --jobs

if grep -Rqs 'fishkernel-ok' "$ARCHIVE/Runtime/jobs"; then
  echo "TOOLS_SMOKE_OK"
else
  echo "TOOLS_SMOKE_FAIL"
  exit 1
fi
