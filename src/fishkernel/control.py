"""控制通道：存档内的状态文件 + 消息队列。

控制器（`fishkernel ctl` / `say`）与内核可以是任意进程，甚至不在同一台机器（共享目录即可）。
内核每轮与延时期间都会读状态，因此 pause / stop 最多 0.2s 生效。
"""

from __future__ import annotations

import json
import time
import math
import hashlib
import uuid

from .archive import Archive
from .atomic import write_atomic, append_line
from .lock import InstanceLock
from .events import read_events

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
        if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
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
        with InstanceLock(self.archive.control_dir / "state.lock"):
            return self._write_state(run=run, delay=delay)

    def _write_state(self, *, run=None, delay=None):
        state = self.read_state()
        if run is not None:
            if run not in VALID_RUN_STATES:
                raise ValueError(f"无效的运行状态：{run}")
            state["run"] = run
        if delay is not None:
            if not math.isfinite(delay) or delay < 0:
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

    def peek_inbox(self) -> tuple[list[dict], int]:
        """Read complete records without acknowledging them."""
        offset = self.inbox_offset()
        try:
            with self.archive.inbox_path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(16 * 1024 * 1024)
        except FileNotFoundError:
            return [], offset
        items = []
        for line in chunk.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            raw = json.loads(line)
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                raise ValueError("Malformed complete inbox record")
            raw.setdefault("id", "legacy-" + hashlib.sha256(str(offset).encode() + line).hexdigest())
            raw.setdefault("source", "control")
            if not isinstance(raw["id"], str) or not raw["id"]:
                raise ValueError("Invalid input ID")
            items.append(raw)
            offset += len(line)
        return items, offset

    def poll_inbox(self) -> list[str]:
        """Compatibility read API; acknowledgement requires commit_inbox()."""
        return [item["text"] for item in self.peek_inbox()[0]]

    def commit_inbox(self, events) -> list[dict]:
        items, end = self.peek_inbox()
        if not items:
            return []
        receipts_path = self.archive.control_dir / "input-receipts.json"
        try:
            receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            receipts = {}
        if not isinstance(receipts, dict):
            raise ValueError("Malformed input receipts")
        # The log closes the crash window between event commit and receipt commit.
        for event in read_events(self.archive.events_path):
            if event.type == "user/message" and event.data.get("input_id"):
                receipts[event.data["input_id"]] = event.seq
        added = []
        for item in items:
            if item["id"] in receipts:
                continue
            event = events.append("user/message", text=item["text"], source=item["source"],
                                  input_id=item["id"], metadata=item.get("metadata", {}))
            receipts[item["id"]] = event.seq
            added.append(item)
        write_atomic(receipts_path, json.dumps(receipts, ensure_ascii=False))
        self.set_inbox_offset(end)
        return added

    def push_message(self, text: str, *, source: str = "control", external_id: str | None = None,
                     metadata: dict | None = None) -> str:
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 256 * 1024:
            raise ValueError("Input must contain 1..262144 UTF-8 bytes")
        identity = (hashlib.sha256(json.dumps([source, external_id]).encode()).hexdigest()
                    if external_id is not None else uuid.uuid4().hex)
        payload = {"id": identity, "source": source, "metadata": metadata or {},
                   "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "text": text}
        line = json.dumps(payload, ensure_ascii=False)
        if len(line.encode("utf-8")) > 512 * 1024:
            raise ValueError("Input envelope too large")
        with InstanceLock(self.archive.control_dir / "inbox.lock"):
            size = self.archive.inbox_path.stat().st_size if self.archive.inbox_path.exists() else 0
            if size - self.inbox_offset() + len(line.encode("utf-8")) > 16 * 1024 * 1024:
                raise ValueError("Inbox full; retry later")
            append_line(self.archive.inbox_path, line)
        return identity

    def has_pending(self) -> bool:
        return bool(self.peek_inbox()[0])

    # ---- 唤醒 / 重启请求 ----
    def request_wake(self) -> None:
        write_atomic(self.archive.wake_path, time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def consume_wake(self) -> bool:
        """存在唤醒请求就消费掉（只生效一次）。"""
        if self.has_pending():
            return True
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
        if not math.isfinite(seconds):
            raise ValueError("Sleep must be finite")
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
