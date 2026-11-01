#!/usr/bin/env bash
# Deterministic process/tool integration; no model key required.
set -euo pipefail
cd "$(dirname "$0")/.."
"${PYTHON:-python3}" -m pytest tests/test_jobs.py tests/test_tools.py::test_spawn_then_job_output tests/test_runtime_v1.py::test_real_sdk_tool_and_external_input
