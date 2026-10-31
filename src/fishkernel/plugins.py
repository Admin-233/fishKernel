"""Versioned, supervised stdio plugins. Only the owner thread mutates core state."""
from __future__ import annotations

import json
import math
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator

from .atomic import write_atomic
from .platform import require_runtime, process_identity, terminate_group

MAX_FRAME = 512 * 1024
ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,23}$")


class PluginError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _local_schema(value):
    if isinstance(value, dict):
        if "$ref" in value and not str(value["$ref"]).startswith("#"):
            raise ValueError("Only local JSON Schema references are allowed")
        for child in value.values():
            _local_schema(child)
    elif isinstance(value, list):
        for child in value:
            _local_schema(child)


def manifest(root: Path, plugin_id: str) -> dict:
    if not ID.fullmatch(plugin_id):
        raise ValueError("Plugin ID must match [a-z][a-z0-9_]{0,23}")
    directory = root / "Plugins" / plugin_id
    path = directory / "plugin.json"
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Manifest too large")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("protocol") != 1 or raw.get("id") != plugin_id:
        raise ValueError("Invalid manifest ID or protocol version")
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError("command must be a nonempty argv array")
    timeout = raw.get("timeout", 30)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("timeout must be finite, in (0, 300]")
    tools = raw.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 32:
        raise ValueError("tools must be an array of at most 32 entries")
    seen = set()
    for tool in tools:
        if not isinstance(tool, dict) or not NAME.fullmatch(str(tool.get("name", ""))):
            raise ValueError("Invalid tool name")
        if tool["name"] in seen:
            raise ValueError("Duplicate tool name")
        seen.add(tool["name"])
        schema = tool.get("parameters", {"type": "object"})
        _local_schema(schema)
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("Tool parameters must describe an object")
        if not isinstance(tool.get("description", ""), str):
            raise ValueError("Tool description must be text")
    return raw


class PluginProcess:
    def __init__(self, root, definition):
        self.definition = definition
        self.id = definition["id"]
        self.generation = uuid.uuid4().hex
        self.root = root
        self.incoming = queue.Queue(maxsize=256)
        self.outgoing = queue.Queue(maxsize=256)
        self.process = None
        self.identity = None
        self.fault = None
        self.ready = False
        self.closed = False
        self.last_seen = time.monotonic()
        self.last_ping = 0.0
        self.responses = {}
        self.expected = set()
        self.subscriptions = set()
        self.cursor = 0
        self.config = {}

    def start(self):
        require_runtime()
        directory = self.root / "Plugins" / self.id
        config = directory / "config.json"
        if config.exists():
            if config.stat().st_size > 256 * 1024:
                raise ValueError("Plugin config too large")
            self.config = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(self.config, dict):
                raise ValueError("Plugin config must be an object")
        command = [sys.executable if x == "{python}" else x for x in self.definition["command"]]
        self.process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.identity = process_identity(self.process.pid)
        for stream, kind in ((self.process.stdout, "message"), (self.process.stderr, "stderr")):
            threading.Thread(target=self._read, args=(stream, kind), daemon=True).start()
        threading.Thread(target=self._write, daemon=True).start()
        self.send({"type": "initialize", "protocol": 1, "generation": self.generation, "config": self.config})

    def _read(self, stream, kind):
        try:
            with stream:
                while not self.closed:
                    line = stream.readline(MAX_FRAME + 1)
                    if not line:
                        return
                    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                        raise ValueError("oversized or unfinished frame")
                    value = json.loads(line) if kind == "message" else line.decode("utf-8", "replace")
                    self.incoming.put_nowait((kind, value))
        except (ValueError, OSError, queue.Full) as exc:
            if not self.closed:
                self.fault = f"{type(exc).__name__}: invalid or excessive plugin output"

    def _write(self):
        try:
            while not self.closed:
                frame = self.outgoing.get()
                if frame is None:
                    return
                self.process.stdin.write(frame)
                self.process.stdin.flush()
        except (OSError, ValueError):
            if not self.closed:
                self.fault = "plugin input pipe closed"

    def send(self, value):
        frame = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(frame) > MAX_FRAME:
            raise PluginError("FRAME_TOO_LARGE", "Plugin frame exceeds limit")
        try:
            self.outgoing.put_nowait(frame)
        except queue.Full:
            raise PluginError("BACKPRESSURE", "Plugin is not consuming its input") from None

    def stop(self):
        self.closed = True
        self.ready = False
        try:
            self.outgoing.put_nowait(None)
        except queue.Full:
            pass
        if self.process is not None:
            if self.process.poll() is None:
                if self.identity:
                    terminate_group(self.process.pid, self.identity)
                else:
                    self.process.kill()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


class PluginManager:
    def __init__(self, archive, control, events, logger):
        self.archive, self.control, self.events, self.logger = archive, control, events, logger
        self.instances = {}
        self.failures = {}
        self.retry_at = {}
        self.enabled = set()
        self.service = lambda: self.poll()
        self._polling = False
        self._last_notice = {}

    @property
    def registry_path(self):
        return self.archive.root / "Plugins" / "enabled.json"

    def start(self):
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or len(raw) > 32 or not all(isinstance(x, str) and ID.fullmatch(x) for x in raw):
                raise ValueError("enabled.json must contain at most 32 valid plugin IDs")
            self.enabled = set(raw)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._notice("registry", "INVALID_REGISTRY", str(exc))
            return
        for plugin_id in sorted(self.enabled):
            self._start(plugin_id)

    def _start(self, plugin_id, definition=None):
        plugin = None
        try:
            plugin = PluginProcess(self.archive.root, definition or manifest(self.archive.root, plugin_id))
            self.instances[plugin_id] = plugin
            plugin.start()
            return plugin
        except Exception as exc:
            if plugin:
                plugin.stop()
            self._failed(plugin_id, "START_FAILED", type(exc).__name__ + ": " + str(exc))
            return None

    def _redact(self, plugin_id, text):
        plugin = self.instances.get(plugin_id)
        def secrets(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if any(part in key.lower() for part in ("key", "token", "password", "secret")) and isinstance(item, str):
                        yield item
                    yield from secrets(item)
            elif isinstance(value, list):
                for item in value:
                    yield from secrets(item)
        if plugin:
            for secret in secrets(plugin.config):
                if secret:
                    text = text.replace(secret, "[redacted]")
        return text[:1000]

    def _notice(self, plugin_id, code, detail):
        text = self._redact(plugin_id, str(detail))
        key = (plugin_id, code)
        if time.monotonic() - self._last_notice.get(key, -100) < 10:
            return
        self._last_notice[key] = time.monotonic()
        self.logger.error(f"plugin {plugin_id}: {code}: {text}")
        self.events.append("plugin/error", plugin_id=plugin_id, code=code, message=text)
        self.events.append("system/message", kind="plugin", text=f"插件 {plugin_id}：{code}，{text}")

    def _failed(self, plugin_id, code, detail):
        plugin = self.instances.get(plugin_id)
        if plugin:
            plugin.stop()
        count = self.failures.get(plugin_id, 0) + 1
        self.failures[plugin_id] = count
        self.retry_at[plugin_id] = time.monotonic() + min(60, 2 ** count)
        self._notice(plugin_id, code, detail)
        self._save_status()

    def _save_status(self):
        write_atomic(self.archive.runtime_dir / "plugins" / "status.json", json.dumps(self.status(), ensure_ascii=False))

    def status(self):
        return [{"id": key, "enabled": key in self.enabled,
                 "state": ("ready" if self.instances.get(key) and self.instances[key].ready else
                           "isolated" if self.failures.get(key, 0) >= 3 else "starting" if key in self.enabled else "disabled"),
                 "failures": self.failures.get(key, 0),
                 "generation": self.instances[key].generation if key in self.instances else None}
                for key in sorted(self.enabled | set(self.instances))]

    def poll(self):
        if self._polling:
            return
        self._polling = True
        try:
            now = time.monotonic()
            for key in list(self.enabled):
                plugin = self.instances.get(key)
                if not plugin or plugin.closed:
                    if self.failures.get(key, 0) < 3 and now >= self.retry_at.get(key, 0):
                        self._start(key)
                    continue
                try:
                    for _ in range(64):
                        try:
                            kind, value = plugin.incoming.get_nowait()
                        except queue.Empty:
                            break
                        if kind == "stderr":
                            self._stderr(key, value)
                        else:
                            self._message(plugin, value)
                    if plugin.fault:
                        raise PluginError("PROTOCOL", plugin.fault)
                    if plugin.process.poll() is not None:
                        raise PluginError("EXITED", f"exit code {plugin.process.returncode}")
                    if now - plugin.last_seen > (20 if plugin.ready else 5):
                        raise PluginError("HEARTBEAT_TIMEOUT", "Plugin stopped responding")
                    if now - plugin.last_ping >= 5:
                        plugin.send({"type": "ping"})
                        plugin.last_ping = now
                    if plugin.ready and plugin.subscriptions:
                        self._publish(plugin)
                except Exception as exc:
                    self._failed(key, getattr(exc, "code", "PLUGIN_ERROR"), str(exc))
        finally:
            self._polling = False

    def _stderr(self, key, value):
        path = self.archive.runtime_dir / "plugins" / f"{key}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1024 * 1024:
            path.replace(path.with_suffix(".previous.log"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(self._redact(key, value).rstrip() + "\n")

    def _message(self, plugin, message):
        if not isinstance(message, dict):
            raise ValueError("Plugin frame must be an object")
        kind = message.get("type")
        if kind == "ready":
            if message.get("protocol") != 1 or message.get("generation") != plugin.generation:
                raise ValueError("Handshake mismatch")
            plugin.ready = True
            self._save_status()
        elif kind == "pong":
            pass
        elif not plugin.ready:
            raise ValueError("Message before ready")
        elif kind == "result":
            identity = message.get("id")
            if identity in plugin.expected:
                plugin.responses[identity] = message
            else:
                self.events.append("plugin/late_result", plugin_id=plugin.id, request_id=str(identity)[:100])
        elif kind == "input":
            external = message.get("external_id")
            if external is not None and (not isinstance(external, str) or len(external) > 256):
                raise ValueError("Invalid external_id")
            try:
                identity = self.control.push_message(message.get("text"), source="plugin:" + plugin.id,
                                                     external_id=external, metadata=message.get("metadata"))
                reply = {"type": "ack", "id": message.get("id"), "input_id": identity, "ok": True}
            except (ValueError, OSError) as exc:
                reply = {"type": "ack", "id": message.get("id"), "ok": False, "error": type(exc).__name__}
            plugin.send(reply)
        elif kind == "subscribe":
            types = message.get("events", [])
            cursor = message.get("after_seq", 0)
            if not isinstance(types, list) or len(types) > 32 or not all(isinstance(x, str) for x in types):
                raise ValueError("Invalid subscription")
            if not isinstance(cursor, int) or cursor < 0:
                raise ValueError("Invalid subscription cursor")
            plugin.subscriptions = set(types)
            plugin.cursor = cursor
        else:
            raise ValueError("Unknown plugin message type")
        plugin.last_seen = time.monotonic()

    def _publish(self, plugin):
        from .events import read_events
        for event in read_events(self.archive.events_path, plugin.cursor)[:32]:
            if event.type in plugin.subscriptions or "*" in plugin.subscriptions:
                plugin.send({"type": "event", "event": json.loads(event.to_json())})
            plugin.cursor = event.seq

    def specs(self):
        result = []
        for key, plugin in sorted(self.instances.items()):
            if key not in self.enabled or not plugin.ready:
                continue
            for tool in plugin.definition.get("tools", []):
                result.append({"type": "function", "function": {
                    "name": f"plugin_{key}__{tool['name']}", "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object"})}})
        return result

    def manage(self, action, plugin_id=None):
        if action == "list":
            return {"plugins": self.status()}
        if action not in {"inspect", "validate", "enable", "disable", "restart", "reload"}:
            raise ValueError("Unknown plugin action")
        if not isinstance(plugin_id, str) or not ID.fullmatch(plugin_id):
            raise ValueError("Invalid plugin ID")
        definition = None if action == "disable" else manifest(self.archive.root, plugin_id)
        if action in {"inspect", "validate"}:
            return {"manifest": definition, "valid": True}
        previous = self.instances.get(plugin_id)
        if action == "enable" and previous and previous.ready:
            return {"plugins": self.status()}
        if previous:
            previous.stop()
        if action == "disable":
            self.enabled.discard(plugin_id)
        else:
            if len(self.enabled | {plugin_id}) > 32:
                raise ValueError("Maximum 32 enabled plugins")
            self.enabled.add(plugin_id)
            self.failures[plugin_id] = 0
        write_atomic(self.registry_path, json.dumps(sorted(self.enabled)))
        if action != "disable":
            plugin = self._start(plugin_id, definition)
            deadline = time.monotonic() + 5
            while plugin and not plugin.ready and not plugin.closed and time.monotonic() < deadline:
                self.service()
                time.sleep(0.02)
            if not plugin or not plugin.ready:
                raise PluginError("NOT_READY", "Plugin failed to become ready; inspect logs/status")
        self._save_status()
        return {"plugins": self.status()}

    def call(self, name, args):
        match = re.fullmatch(r"plugin_([a-z][a-z0-9_]{0,23})__([a-zA-Z][a-zA-Z0-9_]{0,23})", name)
        if not match:
            raise PluginError("UNKNOWN_TOOL", name)
        key, tool_name = match.groups()
        plugin = self.instances.get(key)
        if not plugin or not plugin.ready:
            raise PluginError("UNAVAILABLE", "Plugin is unavailable")
        tool = next((t for t in plugin.definition.get("tools", []) if t["name"] == tool_name), None)
        if tool is None:
            raise PluginError("UNKNOWN_TOOL", name)
        Draft202012Validator(tool.get("parameters", {"type": "object"})).validate(args)
        identity = uuid.uuid4().hex
        plugin.expected.add(identity)
        plugin.send({"type": "call", "id": identity, "name": tool_name, "arguments": args})
        deadline = time.monotonic() + plugin.definition.get("timeout", 30)
        cancelled = False
        try:
            while True:
                self.service()
                if identity in plugin.responses:
                    result = plugin.responses.pop(identity)
                    if cancelled:
                        return {"ok": False, "error": {"code": "INTERRUPTED", "outcome_unknown": not result.get("not_executed", False)}}
                    if "error" in result:
                        return {"ok": False, "error": result["error"]}
                    return {"ok": True, "result": result.get("result")}
                if plugin.closed:
                    raise PluginError("OUTCOME_UNKNOWN", "Plugin exited during the call; do not automatically repeat side effects")
                now = time.monotonic()
                if not cancelled and (self.control.has_pending() or self.control.stop_requested()
                                      or self.control.read_state()["run"] != "run"):
                    plugin.send({"type": "cancel", "id": identity})
                    cancelled, deadline = True, now + 1
                if now >= deadline:
                    self._failed(key, "CANCEL_TIMEOUT" if cancelled else "CALL_TIMEOUT", "Terminated unresponsive plugin; call outcome unknown")
                    return {"ok": False, "error": {"code": "INTERRUPTED" if cancelled else "TIMEOUT", "outcome_unknown": True}}
                time.sleep(0.02)
        finally:
            plugin.expected.discard(identity)

    def close(self):
        for plugin in self.instances.values():
            plugin.stop()
