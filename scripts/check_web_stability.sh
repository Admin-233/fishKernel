#!/usr/bin/env bash
# 网页控制台长期运行的稳定性检查：内存是否泄漏、缓存是否生效。
#
# 起因：web 控制台可能要连续开好多天。如果每秒轮询都在泄漏，几十小时后
# 就会吃掉内存；如果解析缓存失效，每次轮询都会重解析全部事件。
# 这两件事都只能靠长时间/高频访问才暴露，所以做成脚本定期跑。
#
# 用法：scripts/check_web_stability.sh [端口] [轮询次数]
set -uo pipefail

PORT="${1:-8787}"
ROUNDS="${2:-300}"
URL="http://127.0.0.1:$PORT/api/snapshot?after=999999999"

# 按端口找 PID——不要用 pgrep -f "fishkernel web"，
# 那会匹配到本脚本自己的命令行，把脚本自己杀掉。
PID=""
while read -r line; do
  case "$line" in
    *":$PORT"*) PID="$(printf '%s' "$line" | sed 's/.*pid=\([0-9]*\).*/\1/')" ;;
  esac
done < <(ss -lptn 2>/dev/null)

if [[ -z "$PID" ]] || [[ ! -d "/proc/$PID" ]]; then
  echo "✗ 端口 $PORT 上没有服务在跑" >&2
  exit 2
fi

rss() { awk '/^VmRSS/{print $2}' "/proc/$PID/status" 2>/dev/null || echo 0; }

echo "web pid=$PID  端口=$PORT  轮询=$ROUNDS 次"

BASE="$(rss)"
for _ in $(seq 1 50); do curl -sf -o /dev/null "$URL" || break; done
WARM="$(rss)"

FAIL=0
for _ in $(seq 1 "$ROUNDS"); do
  curl -sf -o /dev/null "$URL" || { echo "✗ 请求失败" >&2; FAIL=1; break; }
done
AFTER="$(rss)"

GROWTH=$((AFTER - WARM))
PER=$((GROWTH * 100 / ROUNDS))   # 单位：0.01 kB/次
echo "  RSS: 起始 ${BASE} kB -> 预热 ${WARM} kB -> ${ROUNDS} 次后 ${AFTER} kB"
echo "  增量: ${GROWTH} kB（每 ${ROUNDS} 次）"

# 阈值：每 1000 次轮询增长超过 1 MB 视为可疑
if [[ "$GROWTH" -gt $((ROUNDS)) ]]; then
  echo "  ⚠ 增长偏高：按此速率每 10 万次约 $((GROWTH * 100000 / ROUNDS / 1024)) MB"
  FAIL=1
else
  echo "  ✓ 无显著内存泄漏"
fi

# 响应时间不应随运行时长劣化
T0=$(date +%s%N)
for _ in $(seq 1 50); do curl -sf -o /dev/null "$URL"; done
T1=$(date +%s%N)
AVG=$(( (T1 - T0) / 50000000 ))
echo "  平均响应: ${AVG} ms"
if [[ "$AVG" -gt 100 ]]; then
  echo "  ⚠ 响应偏慢"
  FAIL=1
fi

echo
[[ "$FAIL" == "0" ]] && echo "稳定性检查通过。" || echo "稳定性检查有问题。" >&2
exit "$FAIL"
