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


# ---- read_events 解析缓存的正确性 ----
def test_parse_cache_returns_same_as_fresh_read(archive):
    """缓存命中的结果必须与全新解析完全一致。"""
    from fishkernel.events import clear_parse_cache, read_events

    log = EventLog(archive.events_path).open()
    for i in range(5):
        log.append("user/message", text=f"m{i}")
    log.close()

    clear_parse_cache()
    first = read_events(archive.events_path)
    second = read_events(archive.events_path)     # 命中缓存
    assert [e.seq for e in first] == [e.seq for e in second]
    assert [e.data["text"] for e in first] == [e.data["text"] for e in second]


def test_parse_cache_sees_appended_events(archive):
    """追加新事件后，缓存必须能读到新增部分。"""
    from fishkernel.events import clear_parse_cache, read_events

    clear_parse_cache()
    log = EventLog(archive.events_path).open()
    log.append("user/message", text="a")
    assert len(read_events(archive.events_path)) == 1

    log.append("user/message", text="b")
    events = read_events(archive.events_path)
    assert [e.data["text"] for e in events] == ["a", "b"]
    log.close()


def test_parse_cache_invalidated_on_truncate(archive):
    """文件被截断（如 create_archive 清空）后缓存必须失效。"""
    from fishkernel.events import clear_parse_cache, read_events, truncate_partial_tail

    clear_parse_cache()
    log = EventLog(archive.events_path).open()
    log.append("user/message", text="a")
    log.close()
    assert len(read_events(archive.events_path)) == 1

    # 模拟写到一半崩掉：文件末尾没有换行
    with open(archive.events_path, "a", encoding="utf-8") as f:
        f.write('{"seq":2,"ts":"","type":"user/message","data":{"text":"b"')
    assert truncate_partial_tail(archive.events_path) is True
    clear_parse_cache(archive.events_path)
    events = read_events(archive.events_path)
    assert [e.data["text"] for e in events] == ["a"]


def test_parse_cache_invalidated_on_replacement(archive):
    """文件被整体替换（inode 变化）后缓存必须失效。"""
    from fishkernel.events import clear_parse_cache, read_events

    clear_parse_cache()
    log = EventLog(archive.events_path).open()
    log.append("user/message", text="old")
    log.close()
    assert [e.data["text"] for e in read_events(archive.events_path)] == ["old"]

    # 用 write_atomic 整体替换（inode 会变）
    from fishkernel.atomic import write_atomic
    write_atomic(archive.events_path, '{"seq":1,"ts":"","type":"user/message","data":{"text":"new"}}\n')
    assert [e.data["text"] for e in read_events(archive.events_path)] == ["new"]
