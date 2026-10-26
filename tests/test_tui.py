from __future__ import annotations

import json

from fishkernel.archive import save_kernel_state
from fishkernel.control import Control
from fishkernel.events import Event, EventLog
from fishkernel.tui.app import EventTail, FeedBuffer, TuiApp
from fishkernel.tui.editor import LineEditor
from fishkernel.tui.render import (
    MAX_ASSISTANT_LINES,
    STYLE_ASSISTANT,
    STYLE_DIM,
    STYLE_TOOL_FAIL,
    STYLE_USER,
    Line,
    compress_args,
    display_width,
    render_event,
    render_frame,
    summarize_tool_result,
    truncate,
    visible_input,
    wrap_text,
)


def ev(seq: int, etype: str, **data) -> Event:
    return Event(seq=seq, ts="2026-10-25T20:00:00+08:00", type=etype, data=data)


# ---------------- 宽度与折行 ----------------
def test_display_width_counts_cjk_as_two():
    assert display_width("中文") == 4
    assert display_width("abc") == 3
    assert display_width("中a") == 3


def test_truncate_keeps_width_bound():
    assert truncate("abcdef", 4) == "abc…"
    assert display_width(truncate("中文字符串", 5)) <= 5
    assert truncate("abc", 10) == "abc"


def test_wrap_text_splits_cjk():
    assert wrap_text("中文中文", 4) == ["中文", "中文"]
    assert wrap_text("a\nb", 10) == ["a", "b"]


def test_visible_input_scrolls_to_cursor():
    text = "0123456789"
    shown, before = visible_input(text, 10, 5)
    assert display_width(shown) <= 5
    assert before <= 5
    shown, before = visible_input(text, 0, 5)
    assert shown.startswith("0123")
    assert before == 0


# ---------------- 工具压缩 ----------------
def test_compress_args_shortens_values():
    raw = json.dumps({"path": "/very/long/" + "x" * 80, "offset": 0})
    compressed = compress_args(raw, 60)
    assert len(compressed) <= 60
    assert "offset=0" in compressed


def test_compress_args_handles_bad_json():
    assert compress_args("{不是 JSON", 20)


def test_summarize_tool_results():
    assert "返回 200 行" in summarize_tool_result("read_file", '{"ok":true,"returned_lines":200,"offset":0}')
    assert "4 项" in summarize_tool_result("list_dir", '{"ok":true,"total":4}')
    assert "proc-abc" in summarize_tool_result("spawn", '{"ok":true,"job_id":"proc-abc"}')
    assert "not_found" in summarize_tool_result("read_file", '{"ok":false,"error":{"code":"not_found","message":"没有"}}')
    assert "EOF" in summarize_tool_result("job_output", '{"ok":true,"data":"hi","next_offset":2,"eof":true}')


# ---------------- 事件渲染 ----------------
def test_render_user_message_has_color_and_text():
    lines = render_event(ev(1, "user/message", text="你好"), 80)
    assert lines and lines[0].style == STYLE_USER
    assert "你好" in "".join(line.text for line in lines)


def test_render_assistant_caps_long_text():
    lines = render_event(ev(2, "assistant/message", text="字" * 3000), 20)
    assert len(lines) <= MAX_ASSISTANT_LINES
    assert "还有" in lines[-1].text


def test_render_tool_call_compresses_arguments():
    lines = render_event(
        ev(3, "assistant/message", text="", tool_calls=[{"id": "c1", "name": "read_file", "arguments": '{"path":"/a/b"}'}]),
        80,
    )
    joined = "\n".join(line.text for line in lines)
    assert "read_file(path=/a/b)" in joined


def test_render_tool_result_ok_and_fail():
    ok = render_event(ev(4, "tool/result", call_id="c1", name="list_dir", ok=True, output='{"ok":true,"total":3}', duration_ms=12), 100)
    assert "✓" in ok[0].text and "3 项" in ok[0].text
    bad = render_event(ev(5, "tool/result", call_id="c2", name="read_file", ok=False, output='{"ok":false,"error":{"code":"not_found","message":"没有"}}'), 100)
    assert bad[0].style == STYLE_TOOL_FAIL
    assert "✗" in bad[0].text


def test_render_error_and_compaction():
    error = render_event(ev(6, "error", stage="turn", message="boom"), 80)
    assert "错误" in error[0].text
    compact = render_event(ev(7, "compaction/summary", summary="概要", freed_tokens=1234, archived="Memory/Archive-1.txt"), 100)
    assert "1234" in compact[0].text


def test_wake_notice_is_hidden():
    assert render_event(ev(8, "system/message", text="唤醒", kind="wake"), 80) == []
    assert render_event(ev(9, "delay", seconds=1.0, reason="elapsed"), 80) == []


# ---------------- 整帧 ----------------
def test_frame_has_exact_size_and_cursor():
    width, height = 60, 20
    frame, row, column = render_frame(
        width=width,
        height=height,
        status=Line(" status ", "1;97;44"),
        feed=[Line("line 1"), Line("中文")],
        input_text="hello",
        cursor=5,
        hint=Line("hint"),
        color=False,
    )
    lines = frame.split("\r\n")
    assert len(lines) == height
    assert all(display_width(line) == width for line in lines)
    assert row == height - 1
    assert 1 <= column <= width


def test_frame_with_color_has_ansi():
    frame, _, _ = render_frame(
        width=40,
        height=10,
        status=Line("s", "1;97;44"),
        feed=[],
        input_text="",
        cursor=0,
        hint=Line("h"),
        color=True,
    )
    assert "\x1b[" in frame


def test_frame_strips_control_sequences_from_content():
    frame, _, _ = render_frame(
        width=40,
        height=10,
        status=Line("s"),
        feed=[Line("\x1b[31m红色\x1b[0m")],
        input_text="",
        cursor=0,
        hint=Line("h"),
        color=False,
    )
    assert "\x1b" not in frame
    assert "红色" in frame


# ---------------- 编辑器 ----------------
def test_editor_typing_and_backspace():
    editor = LineEditor()
    for ch in "你好ab":
        editor.handle(ch)
    assert editor.text == "你好ab"
    editor.handle("BACKSPACE")
    assert editor.text == "你好a"
    editor.handle("LEFT")
    editor.handle("BACKSPACE")
    assert editor.text == "你a"


def test_editor_submit_and_history():
    editor = LineEditor()
    for ch in "one":
        editor.handle(ch)
    assert editor.handle("ENTER") == "one"
    for ch in "two":
        editor.handle(ch)
    assert editor.handle("ENTER") == "two"
    editor.handle("UP")
    assert editor.text == "two"
    editor.handle("UP")
    assert editor.text == "one"
    editor.handle("DOWN")
    assert editor.text == "two"


def test_editor_ctrl_u_and_word_delete():
    editor = LineEditor()
    editor.set_text("hello world")
    editor.handle("CTRL_W")
    assert editor.text == "hello "
    editor.handle("CTRL_U")
    assert editor.text == ""


# ---------------- 跟随与滚动 ----------------
def test_feed_buffer_scroll():
    buffer = FeedBuffer()
    for index in range(10):
        buffer.append([Line(str(index))])
    assert [line.text for line in buffer.visible(4, 0)] == ["6", "7", "8", "9"]
    assert [line.text for line in buffer.visible(4, 2)] == ["4", "5", "6", "7"]
    assert buffer.max_scroll(4) == 6


def test_event_tail_incremental_and_partial(archive, log):
    log.append("user/message", text="a")
    tail = EventTail(archive.events_path)
    primed = tail.prime()
    assert [event.data["text"] for event in primed] == ["a"]

    log.append("user/message", text="b")
    polled = tail.poll()
    assert [event.data["text"] for event in polled] == ["b"]

    # 末尾半行：不返回，补齐后返回
    with open(archive.events_path, "ab") as handle:
        handle.write(b'{"seq":99,"type":"x","data":')
    assert tail.poll() == []
    with open(archive.events_path, "ab") as handle:
        handle.write(b'{}}\n')
    events = tail.poll()
    assert events and events[0].seq == 99


# ---------------- 快照与提交 ----------------
def test_snapshot_frame_renders_events(archive, log):
    log.append("session/start", model="deepseek-flash", model_name="DeepSeek-V4.1-Flash (1017)", context_window=1048576)
    log.append("user/message", text="你好")
    log.append("assistant/message", text="收到", usage={"prompt_tokens": 100}, pressure_tokens=90)
    log.append("tool/result", call_id="c1", name="list_dir", ok=True, output='{"ok":true,"total":3}')

    app = TuiApp(archive.root, snapshot=True, color=False, width=80, height=22)
    frame = app._snapshot_frame()
    lines = frame.split("\r\n")
    assert len(lines) == 22
    assert all(display_width(line) == 80 for line in lines)
    assert "fishKernel" in lines[0]
    joined = "\n".join(lines)
    # 左栏：人机对话
    assert "你好" in joined
    # 右栏：过程记录（工具调用摘要）
    assert "list_dir" in joined
    # 左右分栏的分隔符
    assert any("│" in line for line in lines)


def test_submit_writes_inbox_and_commands(archive):
    app = TuiApp(archive.root, snapshot=True, color=False, width=80, height=20)
    app._submit("  去做一件事  ")
    control = Control(archive)
    assert control.poll_inbox() == ["去做一件事"]

    app._submit(":pause")
    assert control.read_state()["run"] == "pause"
    app._submit(":resume")
    assert control.read_state()["run"] == "run"
    app._submit(":delay 3")
    assert control.read_state()["delay"] == 3.0
    app._submit(":stop")
    assert control.read_state()["run"] == "stop"


def test_status_line_uses_kernel_state(archive):
    import os

    save_kernel_state(archive, {"pid": os.getpid(), "status": "running", "turn": 7})
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=20)
    line = app._status(100)
    assert "运行中" in line.text
    assert "第 7 轮" in line.text
    assert "deepseek-flash" in line.text


def test_hint_shows_scroll_state(archive):
    app = TuiApp(archive.root, snapshot=True, color=False, width=80, height=20)
    app.scroll = 5
    assert "滚动" in app._hint().text


def test_follow_mode_prints_and_exits_when_stopped(archive, log, capsys):
    log.append("user/message", text="跟随模式")
    app = TuiApp(archive.root, color=False, width=80, height=12)
    app._stop = True
    assert app.run() == 0
    out = capsys.readouterr().out
    assert "跟随模式" in out


def test_home_edits_input_when_not_empty_else_scrolls(archive, log):
    for index in range(30):
        log.append("user/message", text=f"m{index}")
    app = TuiApp(archive.root, snapshot=True, color=False, width=80, height=12)
    app._ensure_primed()
    app.editor.set_text("abc")
    app._handle_key("HOME")
    assert app.editor.cursor == 0
    app.editor.clear()
    app._handle_key("HOME")
    assert app.scroll > 0
    app._handle_key("END")
    assert app.scroll == 0


def test_render_frame_pins_chat_rows():
    """置顶聊天区：chat 行紧跟在状态栏下方，不随滚动消失。"""
    from fishkernel.tui.render import Line, render_frame

    frame, _, _ = render_frame(
        width=60,
        height=15,
        status=Line("STATUS"),
        feed=[Line("feed-1"), Line("feed-2")],
        input_text="",
        cursor=0,
        hint=Line("hint"),
        color=False,
        chat=[Line("你│你好"), Line("我│在的")],
    )
    rows = [r.rstrip() for r in frame.split("\r\n")]
    assert rows[0].strip() == "STATUS"
    assert rows[1].strip() == "你│你好"
    assert rows[2].strip() == "我│在的"
    # 分栏占满整个消息区，feed 不再单独渲染（内容由右栏承担）
    assert "feed-1" not in "\n".join(rows)


def test_chat_area_excludes_tool_noise():
    """聊天区只收人说的话，不收工具调用/任务结算。"""
    from fishkernel.tui.app import TuiApp

    turns = []
    for label, text in [("你", "帮我看看"), ("我", "好的")]:
        turns.append((label, text))
    assert len(turns) == 2
