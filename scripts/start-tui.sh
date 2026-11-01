#!/usr/bin/env bash
# Open an explicitly selected archive in a Linux terminal emulator.
set -euo pipefail
ARCHIVE="${1:?用法：start-tui.sh 存档目录}"
command -v xterm >/dev/null || { echo '需要 xterm' >&2; exit 2; }
exec xterm -title "${FK_TUI_TITLE:-fishKernel}" -geometry "${FK_TUI_GEOMETRY:-150x42}" \
  -e "${PYTHON:-python3}" -m fishkernel tui "$ARCHIVE"
