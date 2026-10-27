"""崩溃恢复的端到端测试。

已有的单测覆盖了各个零件：`truncate_partial_tail`、`recover_stale`、
`TOOL_OUTCOME_UNKNOWN` 补全。但「进程被 SIGKILL 之后，重启能不能接着跑」
这条最重要的承诺一直是靠人工试出来的。

这里把它固化成自动化测试：真的起子进程、真的 kill -9、真的重启，
然后校验事件日志的完整性。

为什么必须用真进程：SEQ 基线来自 `EventLog.open()` 扫描文件末尾，
孤儿调用的补全发生在 `build_messages()`，两者的交互只有在「日志被外部
截断/追加」这种真实场景下才暴露。mock 掉进程就等于没测。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fishkernel.archive import create_archive
from fishkernel.events import clear_parse_cache, read_events
from fishkernel.messages import build_messages

FISHKERNEL = str(Path(sys.executable).with_name("fishkernel"))


def _seqs(path: Path) -> list[int]:
    clear_parse_cache(path)
    return [event.seq for event in read_events(path)]


def _spawn_kernel(archive_dir: Path, seconds: float = 2.5) -> None:
    """跑一小会儿再 SIGKILL——模拟断电，不给任何收尾机会。"""
    process = subprocess.Popen(
        [FISHKERNEL, "run", str(archive_dir), "--mock", "--quiet"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(seconds)
    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture
def crash_archive(tmp_path) -> Path:
    root = tmp_path / "崩溃存档"
    create_archive(root, name="崩溃测试", api_key="sk-test", turn_delay=0.1)
    return root


pytestmark = pytest.mark.skipif(
    not Path(FISHKERNEL).exists(), reason=f"找不到 fishkernel 可执行文件：{FISHKERNEL}"
)


def test_restart_after_sigkill_continues_seq(crash_archive):
    """SIGKILL 之后重启：seq 必须接着涨，不能从 1 重来。"""
    _spawn_kernel(crash_archive)
    before = _seqs(crash_archive / "Runtime" / "events.jsonl")
    assert before, "第一次运行没写出任何事件，测试前提不成立"

    _spawn_kernel(crash_archive)
    after = _seqs(crash_archive / "Runtime" / "events.jsonl")

    assert after[: len(before)] == before, "重启后历史事件被改写了"
    assert len(after) > len(before), "重启后没有继续写事件"
    assert after == sorted(after), "seq 不是单调递增"
    assert after == list(range(after[0], after[0] + len(after))), "seq 出现缺口"
    assert after[-1] > before[-1], "重启后 seq 回退了（会与历史重复）"


def test_restart_records_second_session(crash_archive):
    _spawn_kernel(crash_archive)
    _spawn_kernel(crash_archive)
    events = read_events(crash_archive / "Runtime" / "events.jsonl")
    starts = [event for event in events if event.type == "session/start"]
    assert len(starts) == 2, f"期望两次 session/start，实际 {len(starts)}"


def test_partial_tail_is_truncated_on_startup(crash_archive):
    """日志末尾有半行（写到一半崩溃）时，重启要能接上且不产生重复 seq。"""
    _spawn_kernel(crash_archive)
    events_path = crash_archive / "Runtime" / "events.jsonl"
    before = _seqs(events_path)

    # 人为追加一个写到一半的事件
    with open(events_path, "a", encoding="utf-8") as handle:
        handle.write('{"seq":999999,"ts":"2026-10-27T13:00:00+08:00","type":"user/mess')

    _spawn_kernel(crash_archive)
    after = _seqs(events_path)

    assert after[: len(before)] == before
    assert after == list(range(after[0], after[0] + len(after))), "半行导致 seq 出现缺口"
    assert 999999 not in after, "半行被当成合法事件读进去了"

    # 最后一行必须是完整 JSON（能被解析出来说明截断生效）
    last_line = events_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert json.loads(last_line)["type"], "最后一行不是合法事件"


def test_orphan_tool_call_gets_unknown_outcome(crash_archive):
    """assistant 写了 tool_call 但结果没落盘时，投影必须补 TOOL_OUTCOME_UNKNOWN。"""
    _spawn_kernel(crash_archive)
    events_path = crash_archive / "Runtime" / "events.jsonl"
    events = read_events(events_path)

    orphan = {
        "seq": events[-1].seq + 1,
        "ts": "2026-10-27T13:00:00+08:00",
        "type": "assistant/message",
        "data": {
            "text": "",
            "tool_calls": [{"id": "orphan-1", "name": "list_dir", "arguments": "{}"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    }
    with open(events_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(orphan, ensure_ascii=False) + "\n")

    # 重启时必须能正常起来，而不是被孤儿调用卡死
    _spawn_kernel(crash_archive)
    after = _seqs(events_path)
    assert after == list(range(after[0], after[0] + len(after)))

    archive = __import__("fishkernel.archive", fromlist=["Archive"]).Archive(crash_archive)
    messages = build_messages(read_events(events_path), archive.system_text())

    calls = [call["id"] for message in messages if message["role"] == "assistant"
             for call in (message.get("tool_calls") or [])]
    results = [message.get("tool_call_id") for message in messages if message["role"] == "tool"]
    assert "orphan-1" in calls
    assert "orphan-1" in results, "孤儿 tool_call 没有配对结果，API 会直接报错"

    paired = next(m for m in messages if m["role"] == "tool" and m.get("tool_call_id") == "orphan-1")
    assert "TOOL_OUTCOME_UNKNOWN" in paired["content"]
