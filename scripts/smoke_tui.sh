#!/usr/bin/env bash
# TUI 冒烟：生成事件 → 渲染快照 → 在伪终端里启动一次真实 TUI。
set -euo pipefail
cd "$(dirname "$0")/.."

ARCHIVE=/tmp/fk-tui-demo
rm -rf "$ARCHIVE"

.venv/bin/fishkernel new "$ARCHIVE" --name TUI演示 --api-key sk-test --turn-delay 0
.venv/bin/fishkernel say "$ARCHIVE" "你好，展示一下界面"
.venv/bin/fishkernel run "$ARCHIVE" --mock --max-turns 3 --quiet

echo "== snapshot =="
.venv/bin/fishkernel tui "$ARCHIVE" --snapshot --width 100 --height 22 --no-color | head -30

echo "== pty smoke =="
if command -v script >/dev/null 2>&1; then
  timeout 3 script -qec "TERM=xterm-256color $(pwd)/.venv/bin/fishkernel tui $ARCHIVE --refresh 0.2" /dev/null > /tmp/fk-tui-pty.out 2>&1 || true
  if grep -q "fishKernel" /tmp/fk-tui-pty.out; then
    echo "TUI_PTY_OK"
  else
    echo "TUI_PTY_FAIL"
    head -20 /tmp/fk-tui-pty.out || true
    exit 1
  fi
else
  echo "script 命令不可用，跳过 pty 冒烟"
fi
