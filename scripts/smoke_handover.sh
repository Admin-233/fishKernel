#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
"${PYTHON:-python3}" -m pytest tests/test_linux_v1.py::test_mock_handover_preserves_single_writer_and_offline_mode
