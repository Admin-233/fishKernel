"""Legacy Agent migration, preserving data and never executing archive programs."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .archive import Archive, ArchiveError
from .atomic import write_atomic
from .events import read_events
from .memory import MemoryStore

FORMAT = 2
DATA_FILES = ("Memory/Agent.txt", "Memory/LongTermMemory.txt", "Memory/Digest.txt", "Memory/History.txt", "Runtime/events.jsonl")
BASELINE_FILES = (*DATA_FILES, "Plugins/enabled.json")


def inspect(archive: Archive) -> dict:
    archive.validate()
    events = read_events(archive.events_path)
    snapshot = MemoryStore(archive).inspect(events)
    return {"target_format": FORMAT, "events": len(events), "memory_applied_seq": snapshot.applied_seq,
            "data_hashes": {name: hashlib.sha256((archive.root / name).read_bytes()).hexdigest()
                            for name in DATA_FILES if (archive.root / name).is_file()},
            "extensions": "legacy extensions are preserved but not loaded"}


def prepare(archive: Archive, events) -> dict:
    """Caller holds the instance lock. Checkpoint and marker updates are repeatable."""
    marker = archive.runtime_dir / "format.json"
    if marker.exists():
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("version") != FORMAT:
            raise ArchiveError("Unsupported archive format version")
        MemoryStore(archive).recover(read_events(archive.events_path))
        return data
    report = inspect(archive)  # Detect unresolved memory before changing anything.
    baseline = archive.runtime_dir / "legacy-v1-baseline"
    for name in BASELINE_FILES:
        source, target = archive.root / name, baseline / name
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Baselines are immutable once created; no program files are executed.
            write_atomic(target, source.read_bytes())
    MemoryStore(archive).recover(read_events(archive.events_path))
    # A pre-existing model-created Plugins directory is not an authorization to
    # execute old extensions. Preserve its registry in the baseline before reset.
    write_atomic(archive.root / "Plugins" / "enabled.json", "[]")
    if not any(e.type == "system/message" and e.data.get("kind") == "migration_v2"
               for e in read_events(archive.events_path)):
        events.append("system/message", kind="migration_v2",
                      text="Agent 的人格、记忆和历史已保留。旧 Tools/Work 扩展不再自动加载或执行；请使用统一插件接口重新适配，原文件仍可读取。")
    data = {"version": FORMAT, "legacy_import": report}
    write_atomic(marker, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def migrate_copy(source: Archive, destination: Path) -> dict:
    from .lock import InstanceLock
    from .events import EventLog
    destination = destination.expanduser().resolve()
    if destination == source.root or source.root in destination.parents:
        raise ArchiveError("Migration destination must be outside the source archive")
    with InstanceLock(source.lock_path):
        report = inspect(source)
        for name in ("basic.instance", "Memory", "Runtime", "Plugins", *BASELINE_FILES):
            if (source.root / name).is_symlink():
                raise ArchiveError("Migration requires core data paths to be real files/directories, not symbolic links")
        if destination.exists():
            raise ArchiveError("Destination exists; inspect/resume that copy instead of overwriting it")
        shutil.copytree(source.root, destination, symlinks=True)
    archive = Archive(destination)
    with InstanceLock(archive.lock_path), EventLog(archive.events_path) as events:
        prepare(archive, events)
    return {"destination": str(destination), **report}
