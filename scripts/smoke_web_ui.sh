#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
"${PYTHON:-python3}" -m pytest tests/test_web_browser.py tests/test_ui_v1.py
