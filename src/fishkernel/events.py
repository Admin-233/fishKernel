"""事件日志：Runtime/events.jsonl 是唯一事实来源。

格式：一行一个 JSON 对象 {"seq", "ts", "type", "data"}。
只追加；读取时容忍末尾半行（崩溃残留）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .atomic import append_line

_SCAN_CHUNK = 64 * 1024
_SCAN_MAX_BYTES = 64 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> "Event | None":
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        seq = raw.get("seq")
        etype = raw.get("type")
        if not isinstance(seq, int) or not isinstance(etype, str):
            return None
        data = raw.get("data")
        return cls(seq=seq, ts=str(raw.get("ts", "")), type=etype, data=data if isinstance(data, dict) else {})


def truncate_partial_tail(path: Path) -> bool:
    """若文件末尾不是换行（半行），截断到最后一个换行。返回是否截断。"""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb+") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return False
        # 从后往前找换行
        chunk = 4096
        pos = size
        while pos > 0:
            start = max(0, pos - chunk)
            handle.seek(start)
            block = handle.read(pos - start)
            idx = block.rfind(b"\n")
            if idx != -1:
                handle.truncate(start + idx + 1)
                return True
            pos = start
        handle.truncate(0)
        return True


class EventLog:
    """进程内的事件追加器。打开时修复半行并记录 seq 基线。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._seq = 0
        self._opened = False

    def open(self) -> "EventLog":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        truncate_partial_tail(self.path)
        last = _last_seq(self.path)
        self._seq = last
        self._opened = True
        return self

    def __enter__(self) -> "EventLog":
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._opened = False

    @property
    def last_seq(self) -> int:
        return self._seq

    def append(self, event_type: str, **data: Any) -> Event:
        if not self._opened:
            raise RuntimeError("EventLog 未打开")
        self._seq += 1
        event = Event(seq=self._seq, ts=now_iso(), type=event_type, data=data)
        append_line(self.path, event.to_json())
        return event


def _last_seq(path: Path) -> int:
    """反向分块扫描，找到最后一条可解析的完整事件。

    不能只读末尾 64 KiB：如果最后一行本身就超过窗口，会一条都解析不出来然后返回 0，
    导致重启后从 seq=1 重写、与历史重复。
    """
    size = path.stat().st_size
    if size == 0:
        return 0
    tail = b""
    scanned = 0
    with open(path, "rb") as handle:
        position = size
        while position > 0 and scanned < _SCAN_MAX_BYTES:
            step = min(_SCAN_CHUNK, position)
            position -= step
            handle.seek(position)
            chunk = handle.read(step)
            scanned += step
            buffer = chunk + tail
            lines = buffer.split(b"\n")
            if position > 0:
                tail = lines[0]  # 跨块的半行，留给下一轮
                complete = lines[1:]
            else:
                tail = b""
                complete = lines
            for line in reversed(complete):
                line = line.strip()
                if not line:
                    continue
                event = Event.from_json(line.decode("utf-8", errors="replace"))
                if event is not None:
                    return event.seq
        if tail.strip():
            event = Event.from_json(tail.strip().decode("utf-8", errors="replace"))
            if event is not None:
                return event.seq
    return 0


def read_events(path: Path | str, after_seq: int = 0) -> list[Event]:
    path = Path(path)
    if not path.exists():
        return []
    events: list[Event] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            event = Event.from_json(line)
            if event is None:
                continue
            if event.seq > after_seq:
                events.append(event)
    return events


def iter_events(path: Path | str, after_seq: int = 0) -> Iterator[Event]:
    yield from read_events(path, after_seq)


def find_last_summary_index(events: list[Event]) -> int:
    for index in range(len(events) - 1, -1, -1):
        if events[index].type == "compaction/summary":
            return index
    return -1
