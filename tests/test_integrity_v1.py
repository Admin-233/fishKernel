"""Data-integrity regressions using synthetic archives, without external tools."""
import json
import pytest

from fishkernel.control import Control
from fishkernel.events import Event, read_events, clear_parse_cache
from fishkernel.memory import MemoryStore, MemoryConflict
from fishkernel.loop import Kernel
from fishkernel.llm import ScriptedClient
from fishkernel.tools import ToolBox
from fishkernel.jobs import JobManager


def test_constructor_does_not_open_or_repair_event_log(archive):
    archive.events_path.write_bytes(b'{"unfinished":')
    kernel = Kernel(archive.root, client=ScriptedClient(), echo=False)
    assert not kernel.events._opened
    assert archive.events_path.read_bytes() == b'{"unfinished":'


def test_incremental_reader_retains_partial_tail(archive):
    raw = (Event(1, "", "user/message", {"text": "中文"}).to_json() + "\n").encode()
    archive.events_path.write_bytes(raw[:27])
    assert read_events(archive.events_path) == []
    with archive.events_path.open("ab") as handle:
        handle.write(raw[27:])
    assert [e.seq for e in read_events(archive.events_path)] == [1]
    clear_parse_cache(archive.events_path)
    assert [e.seq for e in read_events(archive.events_path)] == [1]


def test_event_preserves_unknown_fields():
    raw = {"seq": 1, "ts": "", "type": "future/event", "data": {"v": 3}, "schema": 9}
    assert json.loads(Event.from_json(json.dumps(raw)).to_json()) == raw


def inbox(archive, *items):
    archive.inbox_path.write_text("".join(json.dumps(x) + "\n" for x in items), encoding="utf-8")


def test_inbox_read_does_not_ack_and_commit_deduplicates(archive, log):
    inbox(archive, {"id": "a", "text": "hello"}, {"id": "a", "text": "hello"})
    control = Control(archive)
    assert control.poll_inbox() == ["hello", "hello"]
    assert Control(archive).poll_inbox() == ["hello", "hello"]
    assert control.consume_wake()
    assert len(control.commit_inbox(log)) == 1
    assert control.poll_inbox() == []
    assert len([e for e in read_events(archive.events_path) if e.type == "user/message"]) == 1


def test_inbox_crash_after_event_before_ack(archive, log, monkeypatch):
    inbox(archive, {"id": "a", "text": "hello"})
    control = Control(archive)
    def crash(offset):
        raise OSError("simulated acknowledgement failure")
    monkeypatch.setattr(control, "set_inbox_offset", crash)
    with pytest.raises(OSError):
        control.commit_inbox(log)
    assert Control(archive).commit_inbox(log) == []
    assert len(read_events(archive.events_path)) == 1


def test_legacy_memory_already_fully_synced_is_not_appended_twice(archive, log):
    log.append("memory/write", mode="replace", content="base")
    log.append("memory/write", mode="append", content="next\n")
    original = "base\n\nnext\n"
    archive.write_long_term_memory(original)
    store = MemoryStore(archive)
    state = store.recover(read_events(archive.events_path))
    assert state.applied_seq == 2 and state.content == original
    log.append("memory/write", mode="append", content="new")
    for _ in range(2):
        state = store.recover(read_events(archive.events_path))
    assert state.content == "base\n\nnext\n\nnew"


def test_memory_crash_between_checkpoint_and_projection(archive, log, monkeypatch):
    store = MemoryStore(archive)
    store.recover([])
    log.append("memory/write", mode="append", content="fact")
    def crash(self, text):
        raise OSError("simulated projection failure")
    with monkeypatch.context() as m:
        m.setattr(type(archive), "write_long_term_memory", crash)
        with pytest.raises(OSError):
            store.recover(read_events(archive.events_path))
    assert store.recover(read_events(archive.events_path)).content == "fact"
    assert archive.read_text(archive.long_term_memory_path) == "fact"


def test_unmatched_legacy_memory_is_preserved(archive, log):
    archive.write_long_term_memory("manual value")
    log.append("memory/write", mode="replace", content="different")
    with pytest.raises(MemoryConflict):
        MemoryStore(archive).recover(read_events(archive.events_path))
    assert archive.read_text(archive.long_term_memory_path) == "manual value"
    assert not MemoryStore(archive).path.exists()


def test_memory_cumulative_limit(archive, log):
    box = ToolBox(archive, JobManager(archive), Control(archive), log)
    assert box.dispatch("memory", {"mode": "append", "content": "x" * 40000}).ok
    assert not box.dispatch("memory", {"mode": "append", "content": "y" * 40000}).ok
    assert archive.read_text(archive.long_term_memory_path) == "x" * 40000
