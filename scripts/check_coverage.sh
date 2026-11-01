#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m coverage run --source=src/fishkernel -m pytest
"$PYTHON" -m coverage report --fail-under="${1:-70}"
