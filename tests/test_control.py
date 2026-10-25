from __future__ import annotations

import json

from fishkernel.control import Control


def test_state_defaults(archive):
    control = Control(archive)
    assert control.read_state() == {"run": "run", "delay": None}


def test_state_roundtrip(archive):
    control = Control(archive)
    control.write_state(run="pause")
    assert control.read_state()["run"] == "pause"
    control.write_state(delay=2.5)
    state = control.read_state()
    assert state["run"] == "pause"
    assert state["delay"] == 2.5


def test_state_tolerates_garbage(archive):
    archive.state_path.write_text("{坏掉的 JSON", encoding="utf-8")
    assert Control(archive).read_state() == {"run": "run", "delay": None}


def test_interruptible_sleep_pause_and_stop(archive):
    control = Control(archive)
    control.write_state(run="pause")
    slept, why = control.interruptible_sleep(0.4)
    assert why == "pause"
    assert slept < 0.3

    control.write_state(run="stop")
    slept, why = control.interruptible_sleep(0.4)
    assert why == "stop"

    control.write_state(run="run")
    slept, why = control.interruptible_sleep(0.05)
    assert why == "elapsed"
    assert slept >= 0.03


def test_inbox_incremental(archive):
    control = Control(archive)
    control.push_message("第一条")
    control.push_message("第二条")
    assert control.poll_inbox() == ["第一条", "第二条"]
    assert control.poll_inbox() == []
    control.push_message("第三条")
    assert control.poll_inbox() == ["第三条"]


def test_inbox_partial_line_waits(archive):
    control = Control(archive)
    archive.inbox_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"text": "半行"}, ensure_ascii=False).encode("utf-8")
    cut = len(line) // 2
    with open(archive.inbox_path, "ab") as handle:
        handle.write(line[:cut])
    assert control.poll_inbox() == []
    with open(archive.inbox_path, "ab") as handle:
        handle.write(line[cut:] + b"\n")
    assert control.poll_inbox() == ["半行"]
