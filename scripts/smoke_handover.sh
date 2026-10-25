#!/usr/bin/env bash
# 交班冒烟：旧进程 → 新进程接管 → 停止；检查事件序列与 seq 连续性。
set -euo pipefail
cd "$(dirname "$0")/.."

ARCHIVE=/tmp/fk-handover
rm -rf "$ARCHIVE"

.venv/bin/fishkernel new "$ARCHIVE" --name 交班演示 --api-key sk-test --turn-delay 0.3
.venv/bin/fishkernel run "$ARCHIVE" --mock >/tmp/fk-handover.out 2>&1 &
OLD=$!
sleep 2

echo "== 触发交班 =="
.venv/bin/fishkernel ctl "$ARCHIVE" restart
sleep 4

.venv/bin/fishkernel ctl "$ARCHIVE" stop
sleep 1
wait "$OLD" 2>/dev/null || true

echo "== 事件序列 =="
grep -E '"type":"session/(handover|end|start)"' "$ARCHIVE/Runtime/events.jsonl" || true

echo "== seq 连续性 =="
.venv/bin/python - "$ARCHIVE" <<'PY'
import json
import sys

path = sys.argv[1] + "/Runtime/events.jsonl"
seq = [json.loads(line)["seq"] for line in open(path, encoding="utf-8")]
ok = all(b == a + 1 for a, b in zip(seq, seq[1:]))
print(f"events={len(seq)} contiguous={ok}")
sys.exit(0 if ok else 1)
PY

grep -q 'session/handover' "$ARCHIVE/Runtime/events.jsonl" && echo HANDOVER_OK || { echo HANDOVER_FAIL; exit 1; }
