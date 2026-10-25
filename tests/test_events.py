from __future__ import annotations

import json

from fishkernel.events import EventLog, read_events, truncate_partial_tail


def test_append_and_read(archive):
    log = EventLog(archive.events_path).open()
    assert log.last_seq == 0
    log.append("user/message", text="你好")
    log.append("assistant/message", text="在")
    log.close()

    events = read_events(archive.events_path)
    assert [event.seq for event in events] == [1, 2]
    assert events[0].data["text"] == "你好"


def test_seq_continues_after_reopen(archive):
    log = EventLog(archive.events_path).open()
    log.append("a")
    log.append("b")
    log.close()

    log2 = EventLog(archive.events_path).open()
    assert log2.last_seq == 2
    event = log2.append("c")
    assert event.seq == 3
    log2.close()


def test_truncate_partial_tail(archive):
    path = archive.events_path
    path.write_text('{"seq":1,"type":"a","data":{}}\n{"seq":2,"type":"b"', encoding="utf-8")
    assert truncate_partial_tail(path) is True
    events = read_events(path)
    assert len(events) == 1


def test_read_events_skips_bad_lines(archive):
    path = archive.events_path
    path.write_text(
        json.dumps({"seq": 1, "ts": "", "type": "x", "data": {}}) + "\n不是 JSON\n",
        encoding="utf-8",
    )
    events = read_events(path)
    assert len(events) == 1
    assert events[0].type == "x"


def test_event_json_roundtrip():
    from fishkernel.events import Event

    event = Event(seq=5, ts="t", type="user/message", data={"text": "中"})
    parsed = Event.from_json(event.to_json())
    assert parsed == event


def test_last_seq_survives_huge_last_line(archive):
    path = archive.events_path
    lines = [json.dumps({"seq": index, "ts": "", "type": "x", "data": {}}) for index in range(1, 6)]
    huge = json.dumps({"seq": 6, "ts": "", "type": "x", "data": {"text": "x" * 100_000}})
    path.write_text("\n".join(lines + [huge]) + "\n", encoding="utf-8")
    log = EventLog(path).open()
    assert log.last_seq == 6
    log.close()
