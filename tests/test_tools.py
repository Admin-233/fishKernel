from __future__ import annotations

import json

from fishkernel.control import Control
from fishkernel.jobs import JobManager
from fishkernel.spill import preview, save_text
from fishkernel.tools import ToolBox


def make_toolbox(archive) -> ToolBox:
    return ToolBox(archive, JobManager(archive), Control(archive))


def test_read_file_with_offsets(archive):
    path = archive.root / "notes.txt"
    path.write_text("\n".join(f"第{index}行" for index in range(300)), encoding="utf-8")
    box = make_toolbox(archive)
    result = box.dispatch("read_file", json.dumps({"path": "notes.txt", "offset": 10, "limit": 5}))
    assert result.ok
    assert result.payload["returned_lines"] == 5
    assert result.payload["data"].splitlines()[0] == "第10行"
    assert result.payload["has_more"] is True


def test_read_file_missing(archive):
    result = make_toolbox(archive).dispatch("read_file", '{"path": "不存在.txt"}')
    assert not result.ok
    assert result.payload["error"]["code"] == "not_found"


def test_read_file_binary(archive):
    (archive.root / "bin.dat").write_bytes(b"\x00\x01\x02abc")
    result = make_toolbox(archive).dispatch("read_file", '{"path": "bin.dat"}')
    assert not result.ok
    assert result.payload["error"]["code"] == "binary_file"


def test_list_dir(archive):
    (archive.root / "a.txt").write_text("x", encoding="utf-8")
    (archive.root / "sub").mkdir()
    result = make_toolbox(archive).dispatch("list_dir", '{"path": "."}')
    assert result.ok
    names = {entry["name"] for entry in result.payload["entries"]}
    assert {"a.txt", "sub"} <= names


def test_unknown_tool(archive):
    result = make_toolbox(archive).dispatch("nope", "{}")
    assert not result.ok
    assert result.payload["error"]["code"] == "unknown_tool"


def test_invalid_arguments(archive):
    result = make_toolbox(archive).dispatch("read_file", "{不是JSON")
    assert not result.ok
    assert result.payload["error"]["code"] == "invalid_arguments"


def test_memory_append_and_replace(archive):
    box = make_toolbox(archive)
    box.dispatch("memory", json.dumps({"mode": "append", "content": "第一件事"}))
    box.dispatch("memory", json.dumps({"mode": "append", "content": "第二件事"}))
    text = archive.read_text(archive.long_term_memory_path)
    assert "第一件事" in text and "第二件事" in text

    box.dispatch("memory", json.dumps({"mode": "replace", "content": "只剩这个"}))
    assert archive.read_text(archive.long_term_memory_path).strip() == "只剩这个"


def test_memory_size_limit(archive):
    box = make_toolbox(archive)
    result = box.dispatch("memory", json.dumps({"content": "长" * 40_000}))
    assert not result.ok
    assert result.payload["error"]["code"] == "memory_too_large"


def test_sleep_respects_stop(archive):
    control = Control(archive)
    control.write_state(run="stop")
    result = make_toolbox(archive).dispatch("sleep", '{"seconds": 5}')
    assert result.ok
    assert result.payload["interrupted"] == "stop"


def test_spawn_then_job_output(archive):
    box = make_toolbox(archive)
    spawned = box.dispatch("spawn", json.dumps({"command": "echo via-tool"}))
    assert spawned.ok
    job_id = spawned.payload["job_id"]
    waited = box.dispatch("wait_job", json.dumps({"job_id": job_id, "timeout_seconds": 10}))
    assert waited.ok
    assert waited.payload["job"]["status"] == "completed"
    output = box.dispatch("job_output", json.dumps({"job_id": job_id}))
    assert "via-tool" in output.payload["data"]
    listed = box.dispatch("job_list", "{}")
    assert listed.payload["count"] >= 1


def test_spill_roundtrip(archive):
    info = save_text(archive.spill_dir, "完整内容" * 100, "测试")
    assert info["locator"].startswith("Runtime/spill/")
    path = archive.root / info["locator"]
    assert path.is_file()
    body, truncated = preview("x" * 20_000, limit_chars=100)
    assert truncated is True
    assert "中间内容已省略" in body


def test_tool_result_text_stays_valid_json(archive):
    from fishkernel.constants import MAX_INLINE_RESULT_CHARS
    from fishkernel.tools import ToolResult

    result = ToolResult(
        ok=True,
        payload={"ok": True, "data": "长" * 100_000, "nested": {"blob": "y" * 20_000}},
        name="job_output",
        spill_dir=archive.spill_dir,
    )
    text = result.text
    parsed = json.loads(text)  # 关键：永远是合法 JSON
    assert parsed["ok"] is True
    assert parsed["data"]["locator"].startswith("Runtime/spill/")
    assert len(text) <= MAX_INLINE_RESULT_CHARS
    assert (archive.root / parsed["data"]["locator"]).is_file()


def test_read_file_spills_remainder(archive):
    path = archive.root / "huge.txt"
    path.write_text("x" * 300_000, encoding="utf-8")
    result = make_toolbox(archive).dispatch("read_file", json.dumps({"path": "huge.txt", "limit": 5}))
    assert result.ok
    payload = result.payload
    assert payload["has_more"] is True
    assert "spill" in payload
    assert (archive.root / payload["spill"]["locator"]).is_file()
