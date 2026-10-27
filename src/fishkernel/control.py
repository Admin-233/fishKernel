"""控制通道：存档内的状态文件 + 消息队列。

控制器（`fishkernel ctl` / `say`）与内核可以是任意进程，甚至不在同一台机器（共享目录即可）。
内核每轮与延时期间都会读状态，因此 pause / stop 最多 0.2s 生效。
"""

from __future__ import annotations

import json
import time

from .archive import Archive
from .atomic import write_atomic

VALID_RUN_STATES = {"run", "pause", "stop"}

# stop 是"对当前这次运行"的一次性指令，不是持久配置。
# 内核一旦停下来退出，state.json 里残留的 stop 会让下次启动立刻退出，
# 表现就是"必须 :resume 才能启动"。所以启动时要把它清成 run。
TRANSIENT_RUN_STATES = {"stop"}
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

    def reset_stop_on_start(self) -> bool:
        """内核启动时调用：把上次遗留的 stop 清成 run，避免一启动就退出。

        只处理 stop；pause 是有意保留的（用户可以暂停后重启，仍保持暂停）。
        """
        state = self.read_state()
        if state.get("run") in TRANSIENT_RUN_STATES:
            self.write_state(run="run")
            return True
        return False

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

    # ---- 唤醒 / 重启请求 ----
    def request_wake(self) -> None:
        write_atomic(self.archive.wake_path, time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def consume_wake(self) -> bool:
        """存在唤醒请求就消费掉（只生效一次）。"""
        try:
            self.archive.wake_path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    def request_restart(self, reason: str = "manual") -> None:
        write_atomic(
            self.archive.restart_path,
            json.dumps({"reason": reason, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False),
        )

    def consume_restart(self) -> dict[str, object] | None:
        path = self.archive.restart_path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        try:
            path.unlink()
        except OSError:
            pass
        return payload if isinstance(payload, dict) else {}

    # ---- 可中断等待 ----
    def interruptible_sleep(self, seconds: float) -> tuple[float, str]:
        """睡到时间用尽，或被 pause / stop / wake 打断。

        返回 (实际睡了多久, 原因)，原因 ∈ {"elapsed", "pause", "stop", "wake"}。
        """
        if seconds <= 0:
            state = self.read_state()
            if state["run"] == "stop":
                return 0.0, "stop"
            if state["run"] == "pause":
                return 0.0, "pause"
            if self.consume_wake():
                return 0.0, "wake"
            return 0.0, "elapsed"
        deadline = time.monotonic() + seconds
        slept = 0.0
        while True:
            state = self.read_state()
            if state["run"] == "stop":
                return slept, "stop"
            if state["run"] == "pause":
                return slept, "pause"
            if self.consume_wake():
                return slept, "wake"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return slept, "elapsed"
            step = min(POLL_INTERVAL_SECONDS, remaining)
            time.sleep(step)
            slept += step
