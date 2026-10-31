"""Recoverable memory projection. Checkpoints include content, not just an offset."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .archive import Archive, ArchiveError
from .atomic import write_atomic
from .events import Event


class MemoryConflict(ArchiveError):
    """An existing memory file cannot be matched without guessing its baseline."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_write(current: str, mode: str, content: str) -> str:
    if not content.strip():
        return current
    if mode == "replace":
        return content
    if mode != "append":
        raise MemoryConflict(f"Unknown memory operation: {mode}")
    return current.rstrip() + "\n\n" + content if current.strip() else content


@dataclass(frozen=True)
class MemorySnapshot:
    content: str
    applied_seq: int
    imported: bool = False


class MemoryStore:
    def __init__(self, archive: Archive):
        self.archive = archive
        self.path = archive.runtime_dir / "memory-checkpoint.json"

    def inspect(self, events: list[Event]) -> MemorySnapshot:
        disk = self.archive.read_text(self.archive.long_term_memory_path)
        writes = [e for e in events if e.type == "memory/write"]
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError) as exc:
            raise MemoryConflict(f"Invalid memory checkpoint: {exc}") from exc
        imported = raw is None
        if raw is not None:
            if (not isinstance(raw, dict) or raw.get("version") != 1
                    or not isinstance(raw.get("content"), str)
                    or not isinstance(raw.get("applied_seq"), int)
                    or raw.get("sha256") != digest(raw["content"])):
                raise MemoryConflict("Invalid memory checkpoint schema or checksum")
            content, seq = raw["content"], raw["applied_seq"]
        elif not writes or not disk.strip():
            content, seq = disk, 0
        else:
            # Both historical writers existed: raw replay and stripped kernel replay.
            candidates = []
            for stripped in (False, True):
                replay = ""
                for event in writes:
                    value = str(event.data.get("content") or "")
                    replay = apply_write(replay, str(event.data.get("mode") or "append"),
                                         value.strip() if stripped else value)
                    if replay.strip() == disk.strip():
                        candidates.append(event.seq)
            if not candidates:
                raise MemoryConflict("Existing memory does not match event replay; preserve files and select a baseline before migration")
            content, seq = disk, max(candidates)
        for event in writes:
            if event.seq > seq:
                content = apply_write(content, str(event.data.get("mode") or "append"),
                                      str(event.data.get("content") or ""))
                seq = event.seq
        return MemorySnapshot(content, seq, imported)

    def recover(self, events: list[Event]) -> MemorySnapshot:
        snapshot = self.inspect(events)
        payload = {"version": 1, "applied_seq": snapshot.applied_seq,
                   "content": snapshot.content, "sha256": digest(snapshot.content)}
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        if not self.path.exists() or self.path.read_text(encoding="utf-8") != serialized:
            # Checkpoint is the commit. A crash before projection is repaired on retry.
            write_atomic(self.path, serialized)
        if self.archive.read_text(self.archive.long_term_memory_path) != snapshot.content:
            self.archive.write_long_term_memory(snapshot.content)
        return snapshot
