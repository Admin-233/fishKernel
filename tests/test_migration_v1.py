import json
import pytest
from fishkernel.events import read_events
from fishkernel.migration import inspect, prepare


def test_migration_preserves_agent_and_does_not_load_old_tools(archive, log):
    (archive.runtime_dir / "format.json").unlink()
    log.append("memory/write", mode="replace", content="identity and facts")
    archive.write_long_term_memory("identity and facts\n")
    archive.tools_dir.mkdir()
    archive.register_path.write_text("invalid/old/tool\n", encoding="utf-8")
    work = archive.root / "Work"
    work.mkdir()
    (work / "periodic.sh").write_text("exit 99\n")
    registry = archive.root / "Plugins/enabled.json"
    registry.write_text('["old-model-plugin"]')
    before = archive.agent_path.read_bytes(), archive.long_term_memory_path.read_bytes()
    assert inspect(archive)["memory_applied_seq"] == 1
    prepare(archive, log)
    prepare(archive, log)
    assert before == (archive.agent_path.read_bytes(), archive.long_term_memory_path.read_bytes())
    assert archive.register_path.read_text(encoding="utf-8") == "invalid/old/tool\n"
    assert (work / "periodic.sh").is_file()
    assert json.loads(registry.read_text()) == []
    assert (archive.runtime_dir / "legacy-v1-baseline/Plugins/enabled.json").read_text() == '["old-model-plugin"]'
    assert "identity and facts" in archive.system_text()
    assert len([e for e in read_events(archive.events_path) if e.data.get("kind") == "migration_v2"]) == 1


def test_migration_recovers_after_notice_before_marker(archive, log, monkeypatch):
    import fishkernel.migration as migration
    (archive.runtime_dir / "format.json").unlink()
    log.append("user/message", text="preserve this")
    original = archive.events_path.read_bytes()
    real = migration.write_atomic
    def fail_marker(path, text):
        if path.name == "format.json":
            raise OSError("simulated crash")
        return real(path, text)
    with monkeypatch.context() as m:
        m.setattr(migration, "write_atomic", fail_marker)
        with pytest.raises(OSError):
            prepare(archive, log)
    prepare(archive, log)
    assert (archive.runtime_dir / "legacy-v1-baseline" / "Runtime/events.jsonl").read_bytes() == original
    assert len([e for e in read_events(archive.events_path) if e.data.get("kind") == "migration_v2"]) == 1
    assert json.loads((archive.runtime_dir / "format.json").read_text())["version"] == 2


def test_committed_compaction_repairs_projection_after_crash(archive, config, log, monkeypatch):
    from fishkernel.compress import Compressor
    from fishkernel.llm import ScriptedClient, Completion
    for i in range(6):
        log.append("user/message", text="long history " * 30)
        log.append("assistant/message", text="response " * 30)
    compressor = Compressor(archive, config, ScriptedClient([Completion(text="short summary")]), log)
    def fail(self, text):
        raise OSError("simulated digest projection failure")
    with monkeypatch.context() as m:
        m.setattr(type(archive), "write_digest", fail)
        with pytest.raises(OSError):
            compressor.compact(retain_override=1)
    assert any(e.type == "compaction/summary" for e in read_events(archive.events_path))
    assert compressor.recover_stale()
    assert archive.read_text(archive.digest_path) == "short summary"
    assert read_events(archive.events_path)[-1].type == "compaction/end"
