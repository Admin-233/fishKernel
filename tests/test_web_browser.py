import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fishkernel import web_page


def test_shipped_browser_control_flow(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute browser JavaScript")
    html = next(value for value in vars(web_page).values() if isinstance(value, str) and "<script>" in value)
    script = tmp_path / "web.js"
    script.write_text(re.search(r"<script>(.*?)</script>", html, re.S).group(1), encoding="utf-8")
    subprocess.run([node, str(Path(__file__).with_name("web_page_harness.js")), str(script)], check=True, timeout=10)
