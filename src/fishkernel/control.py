"""控制通道：存档内的状态文件 + 消息队列。

控制器（`fishkernel ctl` / `say`）与内核可以是任意进程，甚至不在同一台机器（共享目录即可）。
内核每轮与延时期间都会读状态，因此 pause / stop 最多 0.2s 生效。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .archive import Archive
from .atomic import write_atomic

VALID_RUN_STATES = {"run", "pause", "stop"}
POLL_INTERVAL_SECONDS = 0.2


class Control:
    def __init__(self, archive: Archive):
        self.archive = archive
        self._state_cache: dict[str, object] = {}
        self._offset_cache: int | None = None

    # ---- 状态 ----
    def read_state(self) -> dict[str, object]:
        try:
            raw = json.loads(self.archive.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"run": "run", "delay": None}
        if not isinstance(raw, dict):
            return {"run": "run", "delay": None}
        run = raw.get("run")
        run = run if run in VALID_RUN_STATES else "run"
        delay = raw.get("delay")
        if not isinstance(delay, (int, float)) or delay < 0:
            delay = None
        return {"run": run, "delay": float(delay) if delay is not None else None}

    def write_state(self, *, run: str | None = None, delay: float | None = None) -> dict[str, object]:
        state = self.read_state()
        if run is not None:
            if run not in VALID_RUN_STATES:
                raise ValueError(f"无效的运行状态：{run}")
            state["run"] = run
        if delay is not None:
            if delay < 0:
                raise ValueError("延时不能为负")
            state["delay"] = float(delay)
        payload = {"run": state["run"], "delay": state["delay"], "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        write_atomic(self.archive.state_path, json.dumps(payload, ensure_ascii=False))
        return state

    # ---- 用户消息 ----
    def inbox_offset(self) -> int:
        try:
            return int(self.archive.inbox_offset_path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def set_inbox_offset(self, offset: int) -> None:
        write_atomic(self.archive.inbox_offset_path, str(max(0, offset)))

    def poll_inbox(self) -> list[str]:
        """增量读取 inbox，返回新消息；只消费完整行。"""
        path = self.archive.inbox_path
        if not path.exists():
            return []
        offset = self.inbox_offset()
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size <= offset:
            return []
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(size - offset)
        consumed = offset
        texts: list[str] = []
        for raw_line in chunk.splitlines(keepends=True):
            if not raw_line.endswith(b"\n"):
                break  # 半行，留到下次
            consumed += len(raw_line)
            try:
                payload = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("text"), str) and payload["text"].strip():
                texts.append(payload["text"])
        if consumed != offset:
            self.set_inbox_offset(consumed)
        return texts

    def push_message(self, text: str) -> None:
        path = self.archive.inbox_path
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "text": text}, ensure_ascii=False)
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")

    # ---- 可中断等待 ----
    def interruptible_sleep(self, seconds: float) -> tuple[float, str]:
        """睡到时间用尽，或被 pause/stop 打断。

        返回 (实际睡了多久, 原因)，原因 ∈ {"elapsed", "pause", "stop"}。
        """
        if seconds <= 0:
            state = self.read_state()
            reason = "stop" if state["run"] == "stop" else ("pause" if state["run"] == "pause" else "elapsed")
            return 0.0, reason
        deadline = time.monotonic() + seconds
        slept = 0.0
        while True:
            state = self.read_state()
            if state["run"] == "stop":
                return slept, "stop"
            if state["run"] == "pause":
                return slept, "pause"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return slept, "elapsed"
            step = min(POLL_INTERVAL_SECONDS, remaining)
            time.sleep(step)
            slept += step
