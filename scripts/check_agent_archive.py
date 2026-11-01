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


def check(path: Path, *, resume: bool = False) -> dict:
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
        report = {"archive_sha256": checksum, "events": len(events),
                "memory_applied_seq": snapshot.applied_seq,
                "data_files_unchanged": all((root / n).read_bytes() == data for n, data in before.items()),
                "unknown_tool_results": sum(m["role"] == "tool" and "TOOL_OUTCOME_UNKNOWN" in m["content"] for m in messages)}
        assert report["data_files_unchanged"], "Memory verification changed original Agent data"
        if resume:
            from fishkernel.llm import ScriptedClient
            from fishkernel.loop import Kernel
            # Use synthetic offline configuration; never copy credentials or executables.
            archive.config_path.write_text("name = compatibility-check\nmodel = compatibility-test\n"
                                           "api_key = sk-test\ncontext_window = 1048576\n"
                                           "max_output_tokens = 8192\nturn_delay = 0\n", encoding="utf-8")
            (archive.runtime_dir / "memory-checkpoint.json").unlink()
            work = root / "Work"
            work.mkdir()
            (work / "periodic.sh").write_text("#!/bin/sh\ntouch legacy-hook-executed\n", encoding="utf-8")
            sentinel = root / "agent-owned" / "note.txt"
            sentinel.parent.mkdir()
            sentinel.write_bytes(b"preserve unknown data")
            for _ in range(2):
                client = ScriptedClient(default_text="offline compatibility verification")
                assert Kernel(root, client=client, max_turns=1, echo=False).run() == 0
                assert before["Memory/Agent.txt"].decode().strip() in client.calls[0]["messages"][0]["content"]
                assert before["Memory/LongTermMemory.txt"].decode().strip() in client.calls[0]["messages"][0]["content"]
            after_events = read_events(archive.events_path)
            assert archive.events_path.read_bytes().startswith(before["Runtime/events.jsonl"])
            assert all((root / name).read_bytes() == value for name, value in before.items() if name != "Runtime/events.jsonl")
            assert not (root / "legacy-hook-executed").exists()
            assert sentinel.read_bytes() == b"preserve unknown data"
            assert len([e for e in after_events if e.data.get("kind") == "migration_v2"]) == 1
            assert len({e.seq for e in after_events}) == len(after_events)
            assert MemoryStore(archive).inspect(after_events).applied_seq == snapshot.applied_seq
            report.update(resume_and_restart_passed=True, original_event_prefix_preserved=True,
                          legacy_hook_executed=False, unknown_files_preserved=True)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--resume", action="store_true", help="Linux: migrate and run twice with an offline client")
    args = parser.parse_args()
    print(json.dumps(check(args.archive, resume=args.resume), indent=2))
