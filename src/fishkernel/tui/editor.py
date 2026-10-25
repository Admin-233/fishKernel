"""单行输入框的编辑逻辑（纯 Python，可单测）。"""

from __future__ import annotations

MAX_HISTORY = 100


class LineEditor:
    def __init__(self, history: list[str] | None = None):
        self.text = ""
        self.cursor = 0
        self.history: list[str] = list(history or [])[-MAX_HISTORY:]
        self._history_index: int | None = None
        self._draft = ""

    # ---- 基本编辑 ----
    def reset(self) -> None:
        self.text = ""
        self.cursor = 0
        self._history_index = None
        self._draft = ""

    def clear(self) -> None:
        self.reset()

    def set_text(self, text: str) -> None:
        self.text = text
        self.cursor = len(text)

    def insert(self, chunk: str) -> None:
        if not chunk:
            return
        self.text = self.text[: self.cursor] + chunk + self.text[self.cursor :]
        self.cursor += len(chunk)

    def backspace(self) -> None:
        if self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]

    def delete_word(self) -> None:
        if self.cursor == 0:
            return
        index = self.cursor
        while index > 0 and self.text[index - 1].isspace():
            index -= 1
        while index > 0 and not self.text[index - 1].isspace():
            index -= 1
        self.text = self.text[:index] + self.text[self.cursor :]
        self.cursor = index

    def move_left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def move_right(self) -> None:
        self.cursor = min(len(self.text), self.cursor + 1)

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.text)

    def clear_text(self) -> None:
        self.text = ""
        self.cursor = 0

    # ---- 历史 ----
    def history_prev(self) -> None:
        if not self.history:
            return
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self.history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._apply_history()

    def history_next(self) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self.history) - 1:
            self._history_index += 1
        else:
            self._history_index = None
            self.text = self._draft
            self.cursor = len(self.text)
            return
        self._apply_history()

    def _apply_history(self) -> None:
        assert self._history_index is not None
        self.text = self.history[self._history_index]
        self.cursor = len(self.text)

    # ---- 按键分发 ----
    def handle(self, key: str) -> str | None:
        """处理一个按键；按 Enter 时返回提交文本，其余返回 None。"""
        if key == "ENTER":
            value = self.text.strip()
            if value:
                if not self.history or self.history[-1] != value:
                    self.history.append(value)
                    self.history = self.history[-MAX_HISTORY:]
            self.reset()
            return value
        if key == "BACKSPACE":
            self.backspace()
        elif key == "DELETE":
            self.delete()
        elif key == "LEFT":
            self.move_left()
        elif key == "RIGHT":
            self.move_right()
        elif key == "HOME" or key == "CTRL_A":
            self.home()
        elif key == "END" or key == "CTRL_E":
            self.end()
        elif key == "UP":
            self.history_prev()
        elif key == "DOWN":
            self.history_next()
        elif key == "CTRL_U":
            self.clear_text()
        elif key == "CTRL_W":
            self.delete_word()
        elif key == "CTRL_K":
            self.text = self.text[: self.cursor]
        elif len(key) == 1 and ord(key) >= 32:
            self.insert(key)
        return None
