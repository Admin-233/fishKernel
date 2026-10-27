"""终端按键解析（tui/terminal.py 的 _drain）。

terminal.py 整体覆盖率低是因为大量平台相关代码（termios / msvcrt），
那些确实不好在单测里跑。但 _drain() 是**纯逻辑**：把已缓冲的字节流
切成按键名，不碰任何系统调用。

这段值得测，因为它处理的是「转义序列可能被截断」这种真实场景 ——
终端读到的数据不保证是完整的按键序列。
"""

from __future__ import annotations

import pytest

from fishkernel.tui.terminal import Terminal


def _term_with_pending(pending: str) -> Terminal:
    """构造一个只用于解析的 Terminal，不进入 raw 模式。"""
    term = Terminal.__new__(Terminal)
    term._pending = pending
    return term


def test_plain_characters():
    term = _term_with_pending("abc")
    assert term._drain() == ["a", "b", "c"]
    assert term._pending == ""


def test_arrow_keys():
    term = _term_with_pending("\x1b[A\x1b[B")
    assert term._drain() == ["UP", "DOWN"]


def test_enter_and_tab():
    term = _term_with_pending("\r\t")
    assert term._drain() == ["ENTER", "TAB"]


def test_backspace_both_forms():
    """\\x7f 和 \\x08 都表示退格。"""
    term = _term_with_pending("\x7f\x08")
    assert term._drain() == ["BACKSPACE", "BACKSPACE"]


def test_ctrl_keys():
    term = _term_with_pending("\x01\x05\x0b")
    assert term._drain() == ["CTRL_A", "CTRL_E", "CTRL_K"]


def test_partial_escape_sequence_is_held_back():
    """只读到 \\x1b 时不能当 ESC 处理 —— 可能是箭头键的前半截。

    这是真实场景：终端读到的字节流可能在转义序列中间被截断。
    """
    term = _term_with_pending("\x1b")
    assert term._drain() == [], "不完整的转义序列应当留在缓冲里等我更多输入"
    assert term._pending == "\x1b"


def test_lone_escape_after_timeout_becomes_esc():
    """缓冲里累积到 3 个字符仍不成序列，就把第一个当 ESC。"""
    term = _term_with_pending("\x1bzz")
    keys = term._drain()
    assert "ESC" in keys


def test_mixed_sequence():
    term = _term_with_pending("a\x1b[Bb")
    assert term._drain() == ["a", "DOWN", "b"]


def test_unknown_escape_still_consumed():
    """未识别的转义序列不能卡住解析 —— 后续字符仍要能读出来。"""
    term = _term_with_pending("\x1b[99~x")
    keys = term._drain()
    assert "x" in keys, f"未知序列之后的普通字符应当仍能读出，实际得到 {keys}"


def test_empty_pending():
    term = _term_with_pending("")
    assert term._drain() == []


def test_is_tty_does_not_crash_when_not_a_tty():
    from fishkernel.tui.terminal import is_tty

    # 在 pytest 里 stdout 通常不是 tty；不管结果如何，不能抛异常
    assert isinstance(is_tty(), bool)
