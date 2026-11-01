"""Runtime contracts exercised with real Linux processes and persistent queues."""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time

import pytest

from fishkernel.control import Control
from fishkernel.events import read_events
from fishkernel.jobs import JobManager, OUTPUT_LIMIT
from fishkernel.llm import Completion, LLMError, ToolCall
from fishkernel.loop import Kernel
from fishkernel.plugins import PluginError
from test_runtime_v1 import definition, manager

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux runtime integration")


def test_input_during_generation_waits_then_prevents_tool_execution(archive):
    class Client:
        count = 0
        def complete(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                Control(archive).push_message("new instruction", external_id="same")
                time.sleep(0.15)
                return Completion(text="generation finished", tool_calls=[
                    ToolCall("skip", "spawn", '{"command":"touch must-not-exist"}')])
            assert any(m.get("content") == "new instruction" for m in messages)
            assert any(m.get("role") == "assistant" and m.get("content") == "generation finished" for m in messages)
            return Completion(text="processed new input")
    assert Kernel(archive.root, client=Client(), max_turns=2, echo=False).run() == 0
    assert not (archive.root / "must-not-exist").exists()
    result = next(e for e in read_events(archive.events_path) if e.type == "tool/result")
    assert json.loads(result.data["output"])["error"]["not_executed"] is True


def test_nonretryable_model_error_blocks_further_requests(archive):
    class Client:
        calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            raise LLMError("invalid model configuration", retryable=False)
    client = Client()
    assert Kernel(archive.root, client=client, max_turns=10, echo=False, error_backoff_base=0).run() == 2
    assert client.calls == 1


def test_job_output_and_exit_survive_original_manager_process(archive):
    code = "from pathlib import Path; from fishkernel.archive import Archive; from fishkernel.jobs import JobManager; import sys; print(JobManager(Archive(Path(sys.argv[1]))).start('sleep 0.3; echo survived').id, flush=True)"
    result = subprocess.run([sys.executable, "-c", code, str(archive.root)], capture_output=True, text=True, timeout=5, check=True)
    identity = result.stdout.strip()
    replacement = JobManager(archive)
    replacement.recover()
    done = replacement.wait(identity, 5)
    assert done.status == "completed" and done.exit_code == 0
    assert "survived" in replacement.output(identity)["data"]


def test_job_output_has_independent_supervisor_limit(archive):
    jobs = JobManager(archive)
    job = jobs.start("yes output")
    try:
        done = jobs.wait(job.id, 5)
        assert done.status == "output_limit"
        assert jobs.stdout_path(job.id).stat().st_size <= OUTPUT_LIMIT
    finally:
        jobs.kill(job.id)


@pytest.mark.parametrize("output", ["not-json", "[]", '{"type":"ready","protocol":99}'])
def test_malformed_plugin_is_contained(archive, log, output):
    definition(archive, ["{python}", "-c", f"import time; print({output!r}, flush=True); time.sleep(10)"])
    host = manager(archive, log)
    try:
        with pytest.raises(PluginError):
            host.manage("enable", "test")
        assert host.instances["test"].closed
        assert any(e.type == "plugin/error" for e in read_events(archive.events_path))
        log.append("system/message", text="kernel still writable")
    finally:
        host.close()


def test_unresponsive_plugin_tool_times_out(archive, log):
    directory, raw = definition(archive)
    raw["timeout"] = 0.2
    (directory / "plugin.json").write_text(json.dumps(raw))
    (directory / "main.py").write_text('''import json, sys, time
m = json.loads(sys.stdin.readline())
print(json.dumps({"type":"ready","protocol":1,"generation":m["generation"]}), flush=True)
time.sleep(30)
''')
    host = manager(archive, log)
    try:
        host.manage("enable", "test")
        result = host.call("plugin_test__echo", {})
        assert result["error"]["code"] == "TIMEOUT"
        assert result["error"]["outcome_unknown"] is True
        assert host.instances["test"].process.poll() is not None
    finally:
        host.close()


def test_sdk_handles_batched_frames_without_another_host_write(archive):
    script = Path(__file__).resolve().parents[1] / "examples/plugins/demo/main.py"
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        frames = [{"type":"initialize", "protocol":1,"generation":"test"}] + [{"type":"ping"}] * 3
        process.stdin.write(b"".join((json.dumps(x) + "\n").encode() for x in frames))
        process.stdin.flush()
        buffer = b""
        deadline = time.monotonic() + 3
        while buffer.count(b"\n") < 4 and time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.05)[0]:
                buffer += os.read(process.stdout.fileno(), 65536)
        messages = [json.loads(x) for x in buffer.splitlines()]
        assert [m["type"] for m in messages] == ["ready", "pong", "pong", "pong"]
    finally:
        process.kill()
        process.communicate(timeout=3)


def test_tui_can_enter_and_exit_real_pty(archive):
    import pty
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, "-m", "fishkernel", "tui", str(archive.root)],
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    try:
        assert select.select([master], [], [], 5)[0], "TUI produced no initial frame"
        os.read(master, 65536)
        os.write(master, b":q\r")
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)


def test_mock_handover_preserves_single_writer_and_offline_mode(archive):
    from fishkernel.archive import load_kernel_state
    from fishkernel.platform import terminate_group
    process = subprocess.Popen([sys.executable, "-m", "fishkernel", "run", str(archive.root), "--mock", "--quiet"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    control = Control(archive)
    control.write_state(delay=0.1)
    def wait_for(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            records = read_events(archive.events_path)
            if predicate(records):
                return records
            time.sleep(0.05)
        raise AssertionError("handover condition timed out")
    try:
        wait_for(lambda es: any(e.type == "assistant/message" for e in es))
        control.request_restart()
        records = wait_for(lambda es: sum(e.type == "session/start" for e in es) >= 2
                           and sum(e.type == "assistant/message" for e in es) >= 2)
        starts = [e.seq for e in records if e.type == "session/start"]
        old_end = next(e.seq for e in records if e.type == "session/end" and e.data["reason"] == "restart")
        assert starts[0] < old_end < starts[1]
        control.write_state(run="stop")
        records = wait_for(lambda es: len([e for e in es if e.type == "session/end"]) == 2)
        assert not [e for e in records if e.type == "error"]
        seqs = [e.seq for e in records]
        assert seqs == list(range(1, seqs[-1] + 1))
        assert process.wait(timeout=3) == 0
    finally:
        state = load_kernel_state(archive)
        if state.get("pid"):
            terminate_group(state["pid"], state.get("process_identity"))
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


@pytest.mark.parametrize("waiting", ["sleep", "wait_job", "plugin"])
def test_plugin_input_interrupts_wait_without_killing_background_job(archive, log, waiting):
    from fishkernel.tools import ToolBox
    source = Path(__file__).resolve().parents[1] / "examples/plugins/demo"
    destination = archive.root / "Plugins/demo"
    shutil.copytree(source, destination)
    script = destination / "main.py"
    script.write_text(script.read_text().replace('external_id="demo-timer-once"',
                                                'external_id="demo-timer-once", metadata={"sent_at":time.monotonic()}'))
    (destination / "config.json").write_text('{"input_after_seconds":0.3}')
    host = manager(archive, log)
    host.control.service = host.poll
    jobs = JobManager(archive)
    job = jobs.start("sleep 10")
    tools = ToolBox(archive, jobs, host.control, log)
    try:
        host.manage("enable", "demo")
        if waiting == "plugin":
            result = host.call("plugin_demo__wait", {"seconds":10})
            assert result["error"]["code"] == "INTERRUPTED"
        else:
            args = {"seconds":10} if waiting == "sleep" else {"job_id":job.id,"timeout_seconds":10}
            result = tools.dispatch(waiting, args)
            assert result.ok and json.loads(result.text)["interrupted"] == "wake"
        finished = time.monotonic()
        pending, _ = host.control.peek_inbox()
        assert finished - pending[0]["metadata"]["sent_at"] < (1.5 if waiting == "plugin" else 0.5)
        assert jobs.load(job.id).status == "running"
    finally:
        host.close()
        jobs.kill(job.id)


def test_bad_reload_config_keeps_previous_plugin(archive, log):
    source = Path(__file__).resolve().parents[1] / "examples/plugins/demo"
    destination = archive.root / "Plugins/demo"
    shutil.copytree(source, destination)
    host = manager(archive, log)
    try:
        host.manage("enable", "demo")
        generation = host.generations()
        (destination / "config.json").write_text('not-json')
        with pytest.raises(ValueError):
            host.manage("reload", "demo")
        assert host.generations() == generation
        assert host.call("plugin_demo__echo", {"text":"still working"})["ok"]
    finally:
        host.close()


def test_model_can_enable_and_call_plugin_through_standard_tools(archive):
    from fishkernel.llm import ScriptedClient
    source = Path(__file__).resolve().parents[1] / "examples/plugins/demo"
    shutil.copytree(source, archive.root / "Plugins/demo")
    client = ScriptedClient([
        Completion(tool_calls=[ToolCall("enable", "plugin_manage", '{"action":"enable","id":"demo"}')]),
        Completion(tool_calls=[ToolCall("echo", "plugin_demo__echo", '{"text":"new capability"}')]),
        Completion(text="extension completed"),
    ])
    assert Kernel(archive.root, client=client, max_turns=3, echo=False).run() == 0
    assert not any(t["function"]["name"] == "plugin_demo__echo" for t in client.calls[0]["tools"])
    assert any(t["function"]["name"] == "plugin_demo__echo" for t in client.calls[1]["tools"])
    results = [e for e in read_events(archive.events_path) if e.type == "tool/result"]
    assert len(results) == 2 and all(e.data["ok"] for e in results)
    assert "new capability" in results[-1].data["output"]


def test_plugin_crash_kills_descendants_before_reaping_leader(archive, log):
    from fishkernel.platform import process_alive
    directory, _ = definition(archive)
    (directory / "main.py").write_text('''import subprocess, sys, time
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
open('child.pid','w').write(str(p.pid))
time.sleep(0.1)
raise SystemExit(7)
''')
    host = manager(archive, log)
    try:
        with pytest.raises(PluginError):
            host.manage("enable", "test")
        child = int((directory / "child.pid").read_text())
        deadline = time.monotonic() + 1
        while process_alive(child) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not process_alive(child)
        assert host.status()[0]["exit_code"] == 7
    finally:
        host.close()
