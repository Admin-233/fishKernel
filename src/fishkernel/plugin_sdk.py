"""Small stdio protocol-v1 helper for independently authored Linux plugins."""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import select
import sys
import threading
import uuid

from .platform import require_runtime


def _execute(connection, function, arguments):
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = function(arguments)
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 256 * 1024:
            raise ValueError("Tool result exceeds 256 KiB")
        connection.send(encoded)
    except BaseException as exc:
        connection.send(json.dumps({"error": {"code": "TOOL_ERROR", "message": str(exc)[:1000]}}))
    finally:
        connection.close()


class Plugin:
    def __init__(self, tools: dict, *, on_start=None, on_event=None, on_ack=None):
        self.tools = tools
        self.on_start, self.on_event, self.on_ack = on_start, on_event, on_ack
        self.config = {}
        self._write_lock = threading.Lock()
        self._calls = {}

    def send(self, message):
        value = json.dumps(message, ensure_ascii=False, allow_nan=False)
        if len(value.encode()) > 512 * 1024 - 1:
            raise ValueError("Plugin frame too large")
        with self._write_lock:
            sys.stdout.write(value + "\n")
            sys.stdout.flush()

    def input(self, text, *, external_id=None, metadata=None):
        identity = uuid.uuid4().hex
        self.send({"type": "input", "id": identity, "text": text,
                   "external_id": external_id, "metadata": metadata or {}})
        return identity

    def subscribe(self, events, *, after_seq=0):
        self.send({"type": "subscribe", "events": events, "after_seq": after_seq})

    def _cancel(self, identity):
        entry = self._calls.pop(identity, None)
        if entry is None:
            return
        process, pipe = entry
        process.terminate()
        process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join(0.2)
        pipe.close()
        self.send({"type": "result", "id": identity, "error": {"code": "CANCELLED"}, "not_executed": False})

    def run(self):
        require_runtime()
        context = multiprocessing.get_context("spawn")
        initialized = False
        buffer = b""
        try:
            while True:
                readable, _, _ = select.select([sys.stdin], [], [], 0.02)
                if readable:
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        return
                    buffer += chunk
                    if len(buffer) > 512 * 1024 and b"\n" not in buffer:
                        raise ValueError("Invalid host frame")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) >= 512 * 1024:
                        raise ValueError("Invalid host frame")
                    message = json.loads(line)
                    kind = message.get("type")
                    if kind == "initialize":
                        if initialized or message.get("protocol") != 1:
                            raise ValueError("Unsupported initialization")
                        initialized = True
                        self.config = message.get("config", {})
                        self.send({"type": "ready", "protocol": 1, "generation": message["generation"]})
                        if self.on_start:
                            self.on_start(self)
                    elif kind == "ping":
                        self.send({"type": "pong"})
                    elif kind == "call" and initialized:
                        identity = message["id"]
                        if identity in self._calls or len(self._calls) >= 16:
                            raise ValueError("Duplicate or excessive calls")
                        function = self.tools.get(message.get("name"))
                        if function is None:
                            self.send({"type": "result", "id": identity, "error": {"code": "UNKNOWN_TOOL"}})
                        else:
                            parent, child = context.Pipe(duplex=False)
                            process = context.Process(target=_execute, args=(child, function, message.get("arguments", {})), daemon=True)
                            process.start()
                            child.close()
                            self._calls[identity] = (process, parent)
                    elif kind == "cancel":
                        self._cancel(message.get("id"))
                    elif kind == "event" and self.on_event:
                        self.on_event(message["event"], self)
                    elif kind == "ack" and self.on_ack:
                        self.on_ack(message, self)
                for identity, (process, pipe) in list(self._calls.items()):
                    payload = None
                    if pipe.poll():
                        try:
                            payload = json.loads(pipe.recv())
                        except (EOFError, ValueError):
                            payload = {"error": {"code": "WORKER_EXITED", "outcome_unknown": True}}
                    elif not process.is_alive():
                        payload = {"error": {"code": "WORKER_EXITED", "outcome_unknown": True}}
                    if payload is not None:
                        self._calls.pop(identity)
                        pipe.close()
                        process.join(0.2)
                        self.send({"type": "result", "id": identity, **payload})
        finally:
            for identity in list(self._calls):
                self._cancel(identity)
