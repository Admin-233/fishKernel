#!/bin/bash
# 在 X 桌面里启动一个真正带 tty 的 TUI 窗口。
#
# 为什么需要这个脚本：
#   用 nohup/& 直接从非交互 shell 起 `fishkernel tui`，进程没有 tty，
#   TUI 会退化成分行打印（follow 模式），看不到左右分栏。
#   必须通过 xterm 这类终端模拟器才能拿到真 tty。
#
# 为什么杀进程要这么小心：
#   写 `pkill -f "fishkernel tui"` 会把执行这条命令的 shell 自己也杀掉，
#   因为它的命令行里同样含这个字符串。所以这里用 pgrep -x 精确匹配
#   可执行名，再读 /proc/<pid>/cmdline 判断，并跳过自己和父进程。
set -u

NAME="${FK_TUI_TITLE:-fk-tui}"
ARCHIVE="${1:-$(cd "$(dirname "$0")/.." && pwd)/../buildFish}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GEOMETRY="${FK_TUI_GEOMETRY:-150x42}"
FONT_SIZE="${FK_TUI_FONT:-11}"

if ! command -v xterm >/dev/null 2>&1; then
  echo "缺少 xterm，请先：sudo apt install -y xterm" >&2
  exit 1
fi

# 1) 停掉旧实例（只认 xterm 进程，且 cmdline 含标题）
for pid in $(pgrep -x xterm 2>/dev/null); do
  [ "$pid" = "$$" ] && continue
  [ "$pid" = "$PPID" ] && continue
  if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- "$NAME"; then
    kill "$pid" 2>/dev/null && echo "停止旧窗口 pid=$pid"
  fi
done
sleep 1

# 2) 启动新窗口
cd "$ROOT" || exit 1
nohup xterm -title "$NAME" -geometry "$GEOMETRY" -fs "$FONT_SIZE" \
  -e bash -lc "cd '$ROOT' && exec .venv/bin/fishkernel tui '$ARCHIVE'" \
  >/dev/null 2>&1 &

sleep 5
if pgrep -x xterm >/dev/null 2>&1; then
  echo "OK 已在 xterm 中启动 TUI（标题 $NAME，存档 $ARCHIVE）"
else
  echo "FAILED 未能启动，请检查 DISPLAY 是否可用" >&2
  exit 1
fi
