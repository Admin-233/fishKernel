#!/usr/bin/env bash
# Explicit paid API smoke test; never used by offline CI.
set -euo pipefail
cd "$(dirname "$0")/.."
export FISHKERNEL_API_KEY="${FISHKERNEL_API_KEY:-${FISHKERNEL_KEY:-}}"
: "${FISHKERNEL_API_KEY:?需要 FISHKERNEL_API_KEY}"
: "${FISHKERNEL_MODEL:?需要 FISHKERNEL_MODEL}"
: "${FISHKERNEL_API_BASE:?需要 FISHKERNEL_API_BASE}"
PYTHON="${PYTHON:-python3}"
TEMP_DIR="$(mktemp -d -t fishkernel-api-XXXXXX)"
ARCHIVE="$TEMP_DIR/agent"
echo "测试存档：$ARCHIVE"
"$PYTHON" -m fishkernel new "$ARCHIVE" --name smoke --model "$FISHKERNEL_MODEL" --api-base "$FISHKERNEL_API_BASE" --max-tokens 2048 --turn-delay 0
printf '\n仅回复 API 连接成功，不调用工具。\n' >> "$ARCHIVE/Memory/Agent.txt"
timeout 180 "$PYTHON" -m fishkernel run "$ARCHIVE" --max-turns 1 --quiet
"$PYTHON" - "$ARCHIVE" <<'PY'
import sys
from pathlib import Path
from fishkernel.events import read_events
events = read_events(Path(sys.argv[1]) / 'Runtime/events.jsonl')
assert any(e.type == 'assistant/message' for e in events)
assert not any(e.type == 'error' for e in events)
assert events[-1].type == 'session/end' and events[-1].data['exit_code'] == 0
print('REAL_API_OK')
PY
