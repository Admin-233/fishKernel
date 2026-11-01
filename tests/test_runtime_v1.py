import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fishkernel.config import config_from_values
from fishkernel.coordinator import CoordinatedClient
from fishkernel.events import read_events
from fishkernel.llm import Completion, ToolCall
from fishkernel.loop import Kernel
from fishkernel.plugins import PluginManager, PluginProcess, PluginError, manifest
from fishkernel.control import Control


def definition(archive, command=None):
    directory = archive.root / "Plugins" / "test"
    directory.mkdir(parents=True, exist_ok=True)
    raw = {"id": "test", "protocol": 1, "command": command or ["{python}", "main.py"],
           "tools": [{"name": "echo", "parameters": {"type": "object"}}]}
    (directory / "plugin.json").write_text(json.dumps(raw))
    return directory, raw


def manager(archive, log):
    return PluginManager(archive, Control(archive), log, SimpleNamespace(error=lambda text: None))


def test_manifest_rejects_nonlocal_schema_and_duplicate_names(archive):
    directory, raw = definition(archive)
    raw["tools"][0]["parameters"]["$ref"] = "https://invalid/schema.json"
    (directory / "plugin.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="local"):
        manifest(archive.root, "test")
    raw["tools"][0]["parameters"].pop("$ref")
    raw["tools"].append(raw["tools"][0])
    (directory / "plugin.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="Duplicate"):
        manifest(archive.root, "test")


def test_message_queue_is_bounded(archive):
    _, raw = definition(archive)
    process = PluginProcess(archive.root, raw)
    for _ in range(256):
        process.send({"type": "ping"})
    with pytest.raises(PluginError, match="consuming"):
        process.send({"type": "ping"})


def test_message_queue_is_also_bounded_by_bytes(archive):
    _, raw = definition(archive)
    process = PluginProcess(archive.root, raw)
    for _ in range(8):
        process.send({"type":"data", "body":"x" * 500000})
    with pytest.raises(PluginError, match="consuming"):
        process.send({"type":"data", "body":"x" * 500000})


def test_plugin_error_is_visible_and_rate_limited(archive, log):
    host = manager(archive, log)
    host._failed("test", "CRASH", "failed")
    host._failed("test", "CRASH", "failed")
    events = read_events(archive.events_path)
    assert len([e for e in events if e.type == "plugin/error"]) == 1
    assert any(e.type == "system/message" and "CRASH" in e.data["text"] for e in events)
    assert host.failures["test"] == 2


def test_model_work_runs_off_owner_thread_and_is_not_cancelled(archive, log):
    owner = threading.get_ident()
    calls = []
    def complete(messages, **kwargs):
        assert threading.get_ident() != owner
        time.sleep(0.12)
        return Completion(text="finished", usage={"prompt_tokens": 4, "completion_tokens": 1})
    def service():
        assert threading.get_ident() == owner
        calls.append(True)
    client = CoordinatedClient(SimpleNamespace(complete=complete), service, log, config_from_values({}))
    response = client.complete([])
    assert response.text == "finished" and len(calls) >= 2
    usage = read_events(archive.events_path)
    assert len(usage) == 1 and usage[0].type == "llm/usage"
    assert response.usage_call_id == usage[0].data["call_id"]


def test_pending_input_interrupts_all_unexecuted_tools(archive):
    kernel = Kernel(archive.root, client=SimpleNamespace(), echo=False)
    kernel.events.open()
    archive.inbox_path.write_text(json.dumps({"id": "new", "text": "change direction"}) + "\n")
    kernel._run_tool_calls(Completion(tool_calls=[ToolCall("a", "spawn", '{"command":"must not run"}'),
                                                 ToolCall("b", "sleep", '{"seconds":10}')]))
    results = [e for e in read_events(archive.events_path) if e.type == "tool/result"]
    assert [e.data["call_id"] for e in results] == ["a", "b"]
    assert all(json.loads(e.data["output"])["error"]["not_executed"] for e in results)
    assert not list(archive.jobs_dir.glob("*/meta.json"))


def test_old_generation_call_is_not_dispatched_to_replacement(archive, monkeypatch):
    kernel = Kernel(archive.root, client=SimpleNamespace(), echo=False)
    kernel.events.open()
    kernel._tool_generations = {"plugin_test__echo":"old"}
    monkeypatch.setattr(kernel.plugins, "generations", lambda: {"plugin_test__echo":"new"})
    def forbidden(*args, **kwargs):
        raise AssertionError("stale call was dispatched")
    monkeypatch.setattr(kernel.toolbox, "dispatch", forbidden)
    try:
        kernel._run_tool_calls(Completion(tool_calls=[ToolCall("call", "plugin_test__echo", "{}")]))
        event = read_events(archive.events_path)[-1]
        assert json.loads(event.data["output"])["error"] == {"code":"PLUGIN_CHANGED", "not_executed":True}
    finally:
        kernel.events.close()


LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux process backend")


@LINUX
def test_real_sdk_tool_and_external_input(archive, log):
    import shutil
    source = Path(__file__).resolve().parents[1] / "examples" / "plugins" / "demo"
    shutil.copytree(source, archive.root / "Plugins" / "demo")
    host = manager(archive, log)
    try:
        host.manage("enable", "demo")
        result = host.call("plugin_demo__echo", {"text": "hello"})
        assert result == {"ok": True, "result": {"text": "hello"}}
        host.control.push_message("wake", external_id="one")
        result = host.call("plugin_demo__wait", {"seconds": 20})
        assert result["ok"] is False and result["error"]["code"] == "INTERRUPTED"
        assert host.instances["demo"].ready
    finally:
        host.close()


@LINUX
def test_real_plugin_exit_does_not_crash_host(archive, log):
    definition(archive, ["{python}", "-c", "raise SystemExit(7)"])
    host = manager(archive, log)
    try:
        with pytest.raises(PluginError):
            host.manage("enable", "test")
        assert host.failures["test"] >= 1
        assert any(e.type == "system/message" for e in read_events(archive.events_path))
    finally:
        host.close()
