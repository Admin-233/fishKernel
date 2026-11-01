#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
"${PYTHON:-python3}" -m pytest tests/test_tui.py tests/test_ui_v1.py tests/test_linux_v1.py::test_tui_can_enter_and_exit_real_pty
