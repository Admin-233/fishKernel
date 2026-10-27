"""事件日志归档测试。

重点不是「归档能跑通」，而是**不安全时它必须拒绝**。
归档是不可逆的破坏性操作，所以每条拒绝路径都要有测试守着。
"""

from __future__ import annotations

import pytest

from fishkernel.events import clear_parse_cache, read_events
from fishkernel.messages import build_messages
from fishkernel.trim import (
    ARCHIVE_PREFIX,
    TrimError,
    apply_trim,
    list_archives,
    plan_trim,
    stats,
)


def _seed_with_compaction(log, *, turns: int = 4, tail: int = 3):
    """造一段「有压缩、压缩点之后还有新对话」的历史。"""
    for index in range(turns):
        log.append("user/message", text=f"老问题 {index}", source="test")
        log.append("assistant/message", text=f"老回答 {index}", tool_calls=[])

    events = read_events(log.path)
    to_seq = events[-1].seq
    log.append(
        "compaction/summary",
        summary="老对话的摘要",
        from_seq=1,
        to_seq=to_seq,
        freed_tokens=500,
    )
    log.append("compaction/end", ok=True)

    for index in range(tail):
        log.append("user/message", text=f"新问题 {index}", source="test")
        log.append("assistant/message", text=f"新回答 {index}", tool_calls=[])
    return to_seq


# ---------------------------------------------------------------- 正常路径


def test_plan_trims_only_before_cutoff(archive, log):
    to_seq = _seed_with_compaction(log)
    events = read_events(archive.events_path)
    plan = plan_trim(archive)

    assert plan.cutoff_seq == to_seq
    assert all(event.seq <= to_seq for event in plan.archived)
    assert all(event.seq > to_seq for event in plan.kept)
    # summary 事件本身必须留下（它是投影起点）
    assert any(event.type == "compaction/summary" for event in plan.kept)
    assert len(plan.archived) + len(plan.kept) == len(events)


def test_projection_unchanged_after_trim(archive, log):
    """这是归档唯一的核心保证：上下文投影必须逐字节一致。"""
    _seed_with_compaction(log)
    before = build_messages(read_events(archive.events_path), archive.system_text())

    plan = plan_trim(archive)
    apply_trim(archive, plan, stamp="20260101-000000")
    clear_parse_cache(archive.events_path)

    after = build_messages(read_events(archive.events_path), archive.system_text())
    assert before == after


def test_trim_writes_archive_file(archive, log):
    to_seq = _seed_with_compaction(log)
    plan = plan_trim(archive)
    path = apply_trim(archive, plan, stamp="20260101-120000")

    assert path is not None and path.exists()
    assert path.name == f"{ARCHIVE_PREFIX}20260101-120000.jsonl"
    text = path.read_text(encoding="utf-8")
    # 第一行是元信息头，不能被当成普通事件
    assert '"_trim": true' in text
    assert str(to_seq) in text


def test_trim_shrinks_events_file(archive, log):
    _seed_with_compaction(log, turns=10, tail=2)
    before = stats(archive)
    plan = plan_trim(archive)
    apply_trim(archive, plan, stamp="20260101-000000")
    clear_parse_cache(archive.events_path)
    after = stats(archive)

    assert after["events"] < before["events"]
    assert after["bytes"] < before["bytes"]
    assert after["last_seq"] == before["last_seq"], "seq 不该被重编号"


def test_trim_idempotent_when_nothing_left(archive, log):
    _seed_with_compaction(log, turns=3, tail=2)
    plan = plan_trim(archive)
    apply_trim(archive, plan, stamp="20260101-000000")
    clear_parse_cache(archive.events_path)

    # 再归档一次：切分点之前已经空了，应当什么都不做
    again = plan_trim(archive)
    assert again.empty
    assert apply_trim(archive, again, stamp="20260101-000001") is None
    assert len(list_archives(archive)) == 1


# ---------------------------------------------------------------- 拒绝路径


def test_refuses_without_compaction(archive, log):
    log.append("user/message", text="只有对话，没有压缩", source="test")
    with pytest.raises(TrimError, match="还没有完成过压缩"):
        plan_trim(archive)


def test_refuses_empty_log(archive):
    with pytest.raises(TrimError, match="为空"):
        plan_trim(archive)


def test_refuses_summary_without_to_seq(archive, log):
    log.append("user/message", text="q", source="test")
    log.append("compaction/summary", summary="摘要", from_seq=1)  # 没有 to_seq
    log.append("user/message", text="q2", source="test")
    with pytest.raises(TrimError, match="to_seq"):
        plan_trim(archive)


def test_refuses_unclosed_compaction(archive, log):
    _seed_with_compaction(log)
    log.append("compaction/start", trigger="auto", threshold=1)
    with pytest.raises(TrimError, match="未闭合"):
        plan_trim(archive)


def test_nothing_written_when_refused(archive, log):
    """被拒绝时绝不能碰 events.jsonl。"""
    _seed_with_compaction(log)
    log.append("compaction/start", trigger="auto", threshold=1)
    clear_parse_cache(archive.events_path)
    before = archive.events_path.read_bytes()

    with pytest.raises(TrimError):
        plan_trim(archive)

    assert archive.events_path.read_bytes() == before
    assert list_archives(archive) == []


def test_refuses_when_projection_would_change(archive, log, monkeypatch):
    """最关键的守卫：如果归档会改变投影，必须拒绝而不是硬干。

    这里人为让 kept 的投影结果「看起来不同」来模拟未来的投影逻辑变更
    （例如某天新增了对被归档事件类型的依赖）。工具必须宁可放弃也不写盘。
    """
    _seed_with_compaction(log)
    clear_parse_cache(archive.events_path)
    before = archive.events_path.read_bytes()

    import fishkernel.trim as trim_module

    real = trim_module._projection_signature
    calls = {"n": 0}

    def fake(events, system_text):
        calls["n"] += 1
        result = real(events, system_text)
        # 第二次调用（归档后）返回被篡改的结果
        return result if calls["n"] == 1 else result + [{"role": "user", "content": "多出来的"}]

    monkeypatch.setattr(trim_module, "_projection_signature", fake)

    with pytest.raises(TrimError, match="投影会发生变化"):
        plan_trim(archive)

    assert archive.events_path.read_bytes() == before
    assert list_archives(archive) == []


# ---------------------------------------------------------------- 其他


def test_history_refreshed_after_trim(archive, log):
    _seed_with_compaction(log)
    plan = plan_trim(archive)
    apply_trim(archive, plan, stamp="20260101-000000")
    clear_parse_cache(archive.events_path)

    history = archive.read_text(archive.history_path)
    # 被归档的老对话不该再出现在 History 里
    assert "老问题 0" not in history
    # 保留区的新对话应当在
    assert "新问题 0" in history


def test_stats_shape(archive, log):
    _seed_with_compaction(log)
    info = stats(archive)
    assert info["events"] > 0
    assert info["bytes"] > 0
    assert info["last_seq"] >= info["first_seq"]
    assert info["archives"] == 0


# ---- trim 与并发写入 ---------------------------------------------------------
# 真实 bug（2026-10-27）：apply_trim 用 write_atomic 整体替换 events.jsonl，
# 但没有任何锁检查。如果内核同时在追加事件，那些新事件会随旧文件一起被覆盖。
# 实测并发写入 300 条时永久丢失 7 条（seq 6038..6044）。


def _archive_with_compressible_history(tmp_path):
    """造一个「已压缩过、因此有可归档内容」的存档。"""
    import json

    from fishkernel.archive import create_archive

    root = tmp_path / "trimable"
    create_archive(root, name="t", api_key="sk-t")

    events = [
        {"seq": 1, "ts": "", "type": "user/message", "data": {"text": "老消息 1"}},
        {"seq": 2, "ts": "", "type": "user/message", "data": {"text": "老消息 2"}},
        {"seq": 3, "ts": "", "type": "compaction/start", "data": {}},
        {"seq": 4, "ts": "", "type": "compaction/summary",
         "data": {"summary": "摘要", "from_seq": 1, "to_seq": 2}},
        {"seq": 5, "ts": "", "type": "compaction/end", "data": {}},
        {"seq": 6, "ts": "", "type": "user/message", "data": {"text": "新消息"}},
    ]
    (root / "Runtime" / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8")

    from fishkernel.archive import Archive
    from fishkernel.events import clear_parse_cache

    clear_parse_cache(root / "Runtime" / "events.jsonl")
    return Archive(root)


def test_apply_trim_refuses_when_kernel_running(tmp_path):
    """内核持锁时必须拒绝，而不是静默丢数据。"""
    import pytest

    from fishkernel.lock import InstanceLock
    from fishkernel.trim import TrimError, apply_trim, plan_trim

    archive = _archive_with_compressible_history(tmp_path)
    plan = plan_trim(archive)

    holder = InstanceLock(archive.lock_path)
    holder.acquire(timeout=0.0)
    try:
        with pytest.raises(TrimError) as excinfo:
            apply_trim(archive, plan)
        assert "内核正在运行" in str(excinfo.value)
    finally:
        holder.release()


def test_apply_trim_works_when_no_kernel(tmp_path):
    """没有内核持锁时照常工作（别把修复做成永久拒绝）。"""
    from fishkernel.trim import apply_trim, plan_trim

    archive = _archive_with_compressible_history(tmp_path)
    plan = plan_trim(archive)
    result = apply_trim(archive, plan)
    assert result is not None, "正常情况应当产出归档文件"


def test_concurrent_writes_survive_when_trim_refused(tmp_path):
    """被拒绝之后，并发写入的事件一条都不能少。"""
    import threading
    import time

    import pytest

    from fishkernel.events import EventLog, clear_parse_cache, read_events
    from fishkernel.lock import InstanceLock
    from fishkernel.trim import TrimError, apply_trim, plan_trim

    archive = _archive_with_compressible_history(tmp_path)
    plan = plan_trim(archive)
    written: list[int] = []
    stop = threading.Event()

    def writer() -> None:
        log = EventLog(archive.events_path).open()
        try:
            for index in range(60):
                if stop.is_set():
                    break
                written.append(log.append("user/message", text=f"w{index}").seq)
                time.sleep(0.002)
        finally:
            log.close()

    thread = threading.Thread(target=writer)
    thread.start()
    time.sleep(0.05)

    holder = InstanceLock(archive.lock_path)
    holder.acquire(timeout=0.0)
    try:
        with pytest.raises(TrimError):
            apply_trim(archive, plan)
    finally:
        holder.release()

    stop.set()
    thread.join(timeout=30)

    clear_parse_cache(archive.events_path)
    surviving = {event.seq for event in read_events(archive.events_path)}
    lost = [seq for seq in written if seq not in surviving]
    assert lost == [], f"被拒绝后仍有事件丢失：{lost[:8]}"
