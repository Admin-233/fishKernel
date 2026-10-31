"""Read-only compatibility check; copies only data files to a disposable directory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile

from fishkernel.archive import Archive
from fishkernel.events import read_events
from fishkernel.memory import MemoryStore
from fishkernel.messages import build_messages


def check(path: Path) -> dict:
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    with tarfile.open(path, "r:gz") as source, tempfile.TemporaryDirectory(prefix="fish-check-") as tmp:
        root = Path(tmp)
        names = ["Runtime/events.jsonl", "Memory/LongTermMemory.txt", "Memory/Agent.txt", "Memory/Digest.txt"]
        for name in names:
            matches = [m for m in source.getmembers() if m.isfile() and m.name.endswith("/" + name)]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one {name}")
            member = matches[0]
            if member.size > 128 * 1024 * 1024:
                raise ValueError("Archive member exceeds verification limit")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.extractfile(member).read())
        archive = Archive(root)
        before = {name: (root / name).read_bytes() for name in names}
        events = read_events(archive.events_path)
        snapshot = MemoryStore(archive).recover(events)
        MemoryStore(archive).recover(events)
        messages = build_messages(events, archive.system_text())
        return {"archive_sha256": checksum, "events": len(events),
                "memory_applied_seq": snapshot.applied_seq,
                "data_files_unchanged": all((root / n).read_bytes() == data for n, data in before.items()),
                "unknown_tool_results": sum(m["role"] == "tool" and "TOOL_OUTCOME_UNKNOWN" in m["content"] for m in messages)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    print(json.dumps(check(parser.parse_args().archive), indent=2))
