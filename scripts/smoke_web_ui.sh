#!/usr/bin/env bash
# 网页控制台的**界面层**冒烟：用真实浏览器驱动页面里的 JS。
#
# 为什么需要它：tests/test_web.py 测的是 HTTP 接口，页面上的 JavaScript
# 从来没有被真正执行过。按钮绑定、轮询刷新、输入框清空这些行为只有跑真浏览器
# 才能验证。这个脚本靠 Tools/browser（CDP 驱动 chromium）来做。
#
# 用法：scripts/smoke_web_ui.sh [端口]
set -uo pipefail

PORT="${1:-8799}"
ARCHIVE="$(mktemp -d)/ui-smoke"
BROWSER="${FISH_BROWSER:-/home/fish/桌面/fish/Tools/browser/browse.py}"
SESSION="smoke-$$"
FAIL=0

command -v chromium >/dev/null || { echo "缺少 chromium，跳过"; exit 0; }
[[ -f "$BROWSER" ]] || { echo "缺少 browser 工具：$BROWSER"; exit 2; }

fishkernel new "$ARCHIVE" --name ui冒烟 --api-key sk-test >/dev/null 2>&1
fishkernel web "$ARCHIVE" --port "$PORT" >/dev/null 2>&1 &
WEB_PID=$!

cleanup() {
  python3 "$BROWSER" --session "$SESSION" close >/dev/null 2>&1
  kill "$WEB_PID" 2>/dev/null
  wait "$WEB_PID" 2>/dev/null
}
trap cleanup EXIT

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$PORT/api/snapshot" >/dev/null 2>&1 && break
  sleep 0.1
done

js() { python3 "$BROWSER" --session "$SESSION" eval "$1" 2>/dev/null | head -1; }

check() {  # check <说明> <期望> <实际>
  if [[ "$2" == "$3" ]]; then
    echo "  ✓ $1"
  else
    echo "  ✗ $1：期望 [$2]，实际 [$3]"
    FAIL=1
  fi
}

python3 "$BROWSER" --session "$SESSION" goto "http://127.0.0.1:$PORT/" >/dev/null 2>&1
sleep 2.5   # 等首次轮询

echo "— 页面加载 —"
check "标题被 JS 更新" "ui冒烟 ·" "$(js 'document.title')"
check "控制状态已填充" "run" "$(js 'document.getElementById("k-run").textContent')"

echo "— 按钮 → 控制文件 —"
js 'document.getElementById("b-pause").click()' >/dev/null; sleep 1.2
check "点暂停后 state.json" "\"pause\"" "$(jq -c .run "$ARCHIVE/Runtime/control/state.json")"
check "点暂停后页面显示" "pause" "$(js 'document.getElementById("k-run").textContent')"

js 'document.getElementById("b-resume").click()' >/dev/null; sleep 1.2
check "点继续后 state.json" "\"run\"" "$(jq -c .run "$ARCHIVE/Runtime/control/state.json")"

echo "— 输入框发消息 —"
js '(function(){document.getElementById("msg").value="界面冒烟消息";document.getElementById("b-say").click();return 1})()' >/dev/null
sleep 1.2
check "消息进了 inbox" "界面冒烟消息" "$(jq -r .text "$ARCHIVE/Runtime/control/inbox.jsonl" 2>/dev/null)"
check "输入框已清空" "" "$(js 'document.getElementById("msg").value')"

echo
if [[ "$FAIL" == "0" ]]; then
  echo "界面冒烟通过。"
else
  echo "界面冒烟有失败项。" >&2
fi
exit "$FAIL"
