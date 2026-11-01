#!/usr/bin/env bash
# 网页控制台冒烟：起服务 → 拉快照 → 发消息 → 暂停/继续 → 收工。
# 用法：scripts/smoke_web.sh [存档目录]
set -euo pipefail

ARCHIVE="${1:-}"
if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE="$(mktemp -d)/smoke-web"
  fishkernel new "$ARCHIVE" --name 冒烟 --api-key sk-smoke >/dev/null
  echo "临时存档：$ARCHIVE"
fi

PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
LOG=$(mktemp)

fishkernel web "$ARCHIVE" --port "$PORT" >"$LOG" 2>&1 &
WEB_PID=$!
trap 'kill $WEB_PID 2>/dev/null || true; wait $WEB_PID 2>/dev/null || true; rm -f "$LOG"' EXIT

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$PORT/api/snapshot" >/dev/null 2>&1 && break
  sleep 0.1
done

echo "— 页面 —"
curl -sf -o /dev/null -w 'GET /            %{http_code}  %{size_download} B\n' "http://127.0.0.1:$PORT/"

echo "— 快照 —"
curl -sf "http://127.0.0.1:$PORT/api/snapshot?tail=1" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ok=",d["ok"],"events=",d["state"]["events"],"run=",d["state"]["control"]["run"])'

echo "— 发消息 —"
curl -sf -X POST "http://127.0.0.1:$PORT/api/say" \
  -H 'Content-Type: application/json' -d '{"text":"冒烟测试"}' >/dev/null
echo "已投递"

echo "— 控制 —"
for action in pause resume; do
  curl -sf -X POST "http://127.0.0.1:$PORT/api/ctl" \
    -H 'Content-Type: application/json' -d "{\"action\":\"$action\"}" | \
    python3 -c "import json,sys; d=json.load(sys.stdin); assert d['ok']; assert d['state']['run'] == {'pause':'pause','resume':'run'}['$action']; print('$action ->',d['state']['run'])"
done

echo "— 错误处理 —"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/api/say" \
  -H 'Content-Type: application/json' -d '{"text":""}')
[[ "$code" == 400 ]] || { echo "空消息应返回 400，实际 $code" >&2; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/nope")
[[ "$code" == 404 ]] || { echo "未知路径应返回 404，实际 $code" >&2; exit 1; }

echo "冒烟通过。"
