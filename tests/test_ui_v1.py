import time
from fishkernel.events import Event
from fishkernel.tui.app import TuiApp
from fishkernel.tui.render import Line, render_frame
from fishkernel.tui.terminal import Terminal
from fishkernel.web import build_snapshot


def left_rows(app):
    return [line.text.rsplit("│", 1)[0].strip() for line in app._chat_lines(100, 8)]


def test_failed_message_submission_preserves_editor_text(archive, monkeypatch):
    app = TuiApp(archive.root, width=100, height=12)
    app.editor.set_text("preserve this input")
    def fail(text):
        raise OSError("queue unavailable")
    monkeypatch.setattr(app.control, "push_message", fail)
    app._handle_key("ENTER")
    assert app.editor.text == "preserve this input"
    assert "投递失败" in app._flash[0]


def test_scroll_moves_one_display_row_and_reaches_entire_message(archive):
    app = TuiApp(archive.root, width=100, height=12)
    app._chat_turns = [("你", "\n".join(f"row{i:02}" for i in range(30)))]
    assert left_rows(app)[0] == "row22"
    app._page_by(1)
    assert left_rows(app)[0] == "row21"
    assert left_rows(app)[-1] == "row28"
    app._page_to_start()
    assert "row00" in left_rows(app)[0]
    assert left_rows(app)[-1] == "row07"


def test_reading_anchor_survives_new_message(archive):
    app = TuiApp(archive.root, width=100, height=12)
    app.events.append(Event(1, "", "user/message", {"text": "\n".join(f"row{i:02}" for i in range(30))}))
    app._refresh_chat()
    app._page_by(5)
    before = left_rows(app)
    app.events.append(Event(2, "", "assistant/message", {"text": "new message\n" * 10}))
    app._refresh_chat()
    assert left_rows(app) == before


def test_input_shows_text_at_cursor():
    frame, row, column = render_frame(width=24, height=8, status=Line(""), feed=[],
                                      input_text="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", cursor=36,
                                      hint=Line(""), color=False)
    assert "XYZ" in frame.split("\r\n")[-2] and column <= 24


def test_escape_has_real_timeout(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    terminal = Terminal()
    terminal._pending = "\x1b"
    assert terminal._drain() == []
    clock[0] += 0.06
    assert terminal._drain() == ["ESC"]


def test_web_incremental_pagination_does_not_skip(archive, log):
    for i in range(805):
        log.append("user/message", text=str(i))
    first = build_snapshot(archive, after_seq=1)
    assert len(first["events"]) == 400
    assert first["next_cursor"] == 401 and first["has_more"]
    second = build_snapshot(archive, after_seq=first["next_cursor"])
    assert second["next_cursor"] == 801
    third = build_snapshot(archive, after_seq=second["next_cursor"])
    assert [e["seq"] for e in third["events"]] == [802, 803, 804, 805]
    assert not third["has_more"]
