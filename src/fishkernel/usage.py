"""Usage ledger independent of active context trimming; event log remains recoverable source."""
from .atomic import append_line
from .events import read_events, truncate_partial_tail


def relevant(event):
    if event.type == "llm/usage":
        return True
    if event.type == "assistant/message":
        return not event.data.get("usage_call_id") and bool(event.data.get("usage"))
    return event.type == "compaction/summary" and not event.data.get("projection_version") and bool(event.data.get("usage"))


def identity(event):
    return event.data.get("call_id") if event.type == "llm/usage" else f"legacy:{event.seq}:{event.type}"


def usage_events(archive):
    ledger = read_events(archive.runtime_dir / "usage.jsonl")
    records = {identity(e): e for e in ledger}
    for event in read_events(archive.events_path):
        if relevant(event):
            records.setdefault(identity(event), event)
    return sorted(records.values(), key=lambda e: e.seq)


def sync_usage(archive):
    path = archive.runtime_dir / "usage.jsonl"
    truncate_partial_tail(path)
    seen = {identity(e) for e in read_events(path)}
    for event in read_events(archive.events_path):
        if relevant(event) and identity(event) not in seen:
            append_line(path, event.to_json())
            seen.add(identity(event))
