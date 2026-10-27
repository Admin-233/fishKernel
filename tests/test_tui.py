from __future__ import annotations

import json

from fishkernel.archive import save_kernel_state
from fishkernel.control import Control
from fishkernel.events import Event
from fishkernel.tui.app import EventTail, FeedBuffer, TuiApp
from fishkernel.tui.editor import LineEditor
from fishkernel.tui.render import (
    MAX_ASSISTANT_LINES,
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
    # 输入框为空时：Home/End 现在用于分栏翻页（跳到最早 / 回到最新）
    app._handle_key("HOME")
    assert app._left_page > 0
    app._handle_key("END")
    assert app._left_page == 0


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

    turns = []
    for label, text in [("你", "帮我看看"), ("我", "好的")]:
        turns.append((label, text))
    assert len(turns) == 2


# ---- 分栏翻页 ----
def test_left_pane_has_history_on_startup(archive, log):
    """启动时左栏必须能读到历史对话（不能只显示最近几条）。"""
    for i in range(20):
        log.append("user/message", text=f"问{i}", source="control")
        log.append("assistant/message", text=f"答{i}")
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=30)
    app._ensure_primed()
    app._refresh_chat()
    assert len(app._chat_turns) == 40, "应加载全部 20 问 20 答"


def test_paging_switches_focus_and_scrolls(archive, log):
    """Tab 切焦点，PgUp 往回翻，PgDn 往前翻，各自独立。"""
    for i in range(40):
        log.append("user/message", text=f"m{i}", source="control")
        # 右栏只收过程类事件，补 assistant + tool 才有内容可翻
        log.append("assistant/message", text=f"答{i}")
        log.append("tool/result", call_id=f"c{i}", name="list_dir", ok=True, output="{}")
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=20)
    app._ensure_primed()
    app._refresh_chat()

    assert app._focus == "left"
    app._handle_key("PGUP")
    left_after = app._left_page
    assert left_after > 0
    assert app._right_page == 0, "左栏翻页不该影响右栏"

    app._handle_key("TAB")
    assert app._focus == "right"
    app._handle_key("PGUP")
    assert app._right_page > 0
    assert app._left_page == left_after, "右栏翻页不该影响左栏"

    app._handle_key("PGDN")
    assert app._right_page < max(1, len(app._detail_lines))


def test_paging_clamped_at_boundaries(archive, log):
    """翻到边界不能越界（既不能负数，也不能超过总数）。"""
    for i in range(10):
        log.append("user/message", text=f"m{i}", source="control")
    app = TuiApp(archive.root, snapshot=True, color=False, width=80, height=12)
    app._ensure_primed()
    app._refresh_chat()

    # 疯狂往回翻
    for _ in range(100):
        app._handle_key("PGUP")
    assert 0 <= app._left_page <= max(0, len(app._chat_turns) - 1)

    # 疯狂往前翻
    for _ in range(100):
        app._handle_key("PGDN")
    assert app._left_page == 0


def test_poll_refreshes_split_panes(archive, log):
    """新事件到达后，左右两栏必须跟着更新，不能停在启动那一刻。"""
    log.append("user/message", text="第一条", source="control")
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=20)
    app._ensure_primed()
    assert len(app._chat_turns) == 1

    log.append("assistant/message", text="回复")
    assert app._poll() is True, "有新事件时必须返回 changed=True"
    assert len(app._chat_turns) == 2, "左栏应包含新增的助手回复"
    assert app._chat_turns[-1] == ("我", "回复")

    log.append("user/message", text="又问", source="control")
    app._poll()
    assert len(app._chat_turns) == 3
    assert app._chat_turns[-1] == ("你", "又问")


def test_poll_keeps_right_pane_in_sync(archive, log):
    """右栏（过程记录）也要跟着刷新。"""
    log.append("user/message", text="hi", source="control")
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=20)
    app._ensure_primed()
    before = len(app._detail_lines)

    log.append("tool/result", call_id="c1", name="list_dir", ok=True, output="{}")
    app._poll()
    assert len(app._detail_lines) > before, "工具结果应出现在右栏"


def test_left_pane_wraps_instead_of_truncating(archive, log):
    """左栏必须折行显示全文，不能截断成省略号。

    历史：这里曾有一行 `left_w = max(20, int(100 * app.CHAT_SPLIT) - 1) if False else 41`。
    `if False` 让右边的 41 生效，左边的分支成了死代码——而它引用的 `app` 在那时
    还没定义（F821）。只要有人把条件改成 True 就会 NameError。
    现在宽度从 TuiApp.CHAT_SPLIT 真正推导出来，不再写死数字。
    """
    from fishkernel.tui.render import display_width, wrap_text

    long_text = "这是一段相当长的中文内容用来验证左栏是否会折行显示全部文字而不是简单截断"
    width, height = 100, 30
    log.append("user/message", text=long_text, source="control")
    app = TuiApp(archive.root, snapshot=True, color=False, width=width, height=height)
    app._ensure_primed()

    # 与 _chat_lines() 保持同一套推导：左栏宽 = max(20, int(width*CHAT_SPLIT)-1)
    left_w = max(20, int(width * TuiApp.CHAT_SPLIT) - 1)
    prefix = "你│"
    left_body_w = max(4, left_w - display_width(prefix))

    rows = app._snapshot_frame().split("\r\n")
    # 注意：分隔栏用的 "│" 和左栏标签前缀 "你│" 是同一个字符。
    # 早先这里写 row.split("│", 1)[0]，切到的是标签"你"本身，于是每条都变成
    # 空串、被下面的 if not body: continue 全部跳过——整套断言等于没跑。
    # 必须从右侧切分才能拿到完整的左栏。
    left_parts: list[str] = []
    for row in rows[1:]:
        if "│" not in row:
            continue
        left_col = row.rsplit("│", 1)[0]
        # 去掉标签前缀（"你│" / "我│"），保留正文（含缩进）
        for label in ("你│", "我│"):
            if left_col.startswith(label):
                left_col = left_col[len(label):]
                break
        left_parts.append(left_col.rstrip())

    assert left_parts, "没有解析出任何左栏内容，测试前提不成立"

    # 1) 不出现省略号 —— 说明是折行而不是截断
    assert "…" not in "".join(left_parts), "左栏不该用省略号截断"

    # 2) 每一行去掉缩进后，都必须是原文里的连续片段（没被吃字）
    bodies = [part.strip() for part in left_parts if part.strip()]
    assert bodies, "左栏解析出内容后全为空，断言会变成空转"
    for body in bodies:
        assert body in long_text, f"行内容 {body!r} 不在原文中，说明被吃字了"

    # 3) 拼回去必须与原文一致：这才证明是「折行」而不是「丢字」。
    expected_lines = wrap_text(long_text, left_body_w)
    assert len(expected_lines) >= 2, "这篇宽度下本应折成多行，测试前提不成立"
    assert bodies == expected_lines, (
        f"左栏折行结果与预期不符：\n  得到 {bodies!r}\n  期望 {expected_lines!r}"
    )

def test_wrapped_rows_fit_pane_width(archive, log):
    """折行后每一行都不能超过左栏宽度。"""
    from fishkernel.tui.render import display_width

    log.append("user/message", text="中" * 200, source="control")
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=30)
    app._ensure_primed()
    left_w = max(20, int(100 * app.CHAT_SPLIT) - 1)

    for row in app._snapshot_frame().split("\r\n")[1:]:
        if "│" in row:
            left = row.split("│", 1)[0]
            assert display_width(left) <= left_w, f"左栏行超宽 {display_width(left)} > {left_w}"


def test_left_pane_preserves_newlines(archive, log):
    """左栏必须保留原文的换行结构，不能把多行压成一行。"""
    log.append(
        "user/message",
        source="control",
        text="第一行\n第二行\n\n第四行（前面有空行）\n- 列表一\n- 列表二",
    )
    # 高度要够：消息区 = height - 4，要装下 6 行内容
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=40)
    app._ensure_primed()

    # 左栏宽度 = CHAT_SPLIT * width - 1，用固定列宽切，避免与「你│」前缀混淆
    left_w = max(20, int(100 * app.CHAT_SPLIT) - 1)
    left_bodies = []
    for row in app._snapshot_frame().split("\r\n")[1:]:
        if not row.strip():
            continue
        body = row[:left_w].rstrip()
        # 去掉「你│」「我│」「系统│」这类前缀，保留正文
        for pfx in ("你│", "我│", "系统│"):
            if body.startswith(pfx):
                body = body[len(pfx):]
                break
        left_bodies.append(body.strip())

    text = "\n".join(left_bodies)
    # 每一行都应独立出现
    for expected in ["第一行", "第二行", "第四行", "- 列表一", "- 列表二"]:
        assert expected in text, f"换行被吃掉，找不到 {expected!r}"
    # 关键：内容不能全挤在同一行
    assert not any("第一行" in b and "第二行" in b for b in left_bodies), "多行被压成一行了"


def test_page_step_is_one(archive):
    """PgUp/PgDn 每次翻 1 行，逐行阅读。"""
    app = TuiApp(archive.root, snapshot=True, color=False, width=100, height=40)
    assert app._page_step() == 1


def test_snapshot_fills_message_area(archive, log):
    """快照模式必须把整个消息区填满（height - 4 行）。

    历史：_snapshot_frame() 里算出 `chat = self._chat_lines(w, body_h)` 却没用，
    传给 render_frame 的是 `self._chat_lines(w)`（没有 height），于是
    _chat_lines 退化成 CHAT_ROWS or 8 行。render_frame 按 height-4 切片，
    窗口高 30 时只显示 8 行消息区，剩下 18 行是空白。
    交互路径一直是传 body_h 的，所以只有 --snapshot 暴露这个问题。
    """
    width, height = 100, 30
    # 塞够多消息，足以填满消息区
    for index in range(40):
        log.append("user/message", text=f"第 {index} 条消息", source="control")

    app = TuiApp(archive.root, snapshot=True, color=False, width=width, height=height)
    app._ensure_primed()
    rows = app._snapshot_frame().split("\r\n")

    # 1) 总行数符合约定
    assert len(rows) == height, f"帧高 {len(rows)}，应为 {height}"

    # 2) 除状态/分隔/输入/提示这 4 行外，消息区每一行都必须有左栏内容
    body = rows[1:height - 3]
    assert len(body) == height - 4
    empty = [i for i, row in enumerate(body) if not row.split("│", 1)[0].strip()]
    assert not empty, (
        f"消息区有 {len(empty)}/{len(body)} 行是空的（行号 {empty[:5]}）；"
        "说明 _chat_lines 没拿到正确高度"
    )
