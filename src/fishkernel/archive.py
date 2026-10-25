"""存档：目录布局、初始化、读取与投影。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .atomic import write_atomic
from .constants import DEFAULT_API_BASE, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, DEFAULT_TURN_DELAY
from .events import Event

DEFAULT_AGENT_PROMPT = """你是「{name}」，一个在这台机器上长期自主运行的 Agent。

## 你的环境
- 存档目录（也是你的家目录）：{root}
- 你的记忆在 Memory/：Agent.txt（本条设定）、LongTermMemory.txt（长期记忆）、Digest.txt（压缩后的历史概要）
- Tools/ToolsRegister.txt 登记了你可以使用的命令行工具，它们的用法在各自目录的 Usage.txt
- 你拥有这台机器的全部权限：可以读任何文件、执行任何命令

## 你如何行动
- 想执行命令：调用 `spawn` 启动进程（异步），拿到 job_id；用 `job_output` / `wait_job` 取结果，用 `job_kill` 终止。
- 想读文件：用 `read_file`，大文件用 offset / limit 分段读；列目录用 `list_dir`。
- 想记住跨压缩也不该丢的事：用 `memory` 工具写入长期记忆。
- 想等待：用 `sleep`（可被用户打断），不要在回复里空等。

## 你的节奏
内核会无限循环：每轮把你的回复解析成工具调用并执行，然后唤醒你继续。没有工具调用的轮次会先延时再唤醒。
不要原地空转：每一轮要么推进目标，要么用 `sleep` 明确等待，要么向用户说明你在等什么。
"""

DEFAULT_TOOLS_REGISTER = """# 每行一个已启用的工具目录名；# 起始为注释。
# 例如：
# net-tools
"""


class ArchiveError(Exception):
    """存档缺失或损坏。"""


@dataclass(frozen=True)
class Archive:
    root: Path

    # ---- 路径 ----
    @property
    def config_path(self) -> Path:
        return self.root / "basic.instance"

    @property
    def memory_dir(self) -> Path:
        return self.root / "Memory"

    @property
    def agent_path(self) -> Path:
        return self.memory_dir / "Agent.txt"

    @property
    def long_term_memory_path(self) -> Path:
        return self.memory_dir / "LongTermMemory.txt"

    @property
    def digest_path(self) -> Path:
        return self.memory_dir / "Digest.txt"

    @property
    def history_path(self) -> Path:
        return self.memory_dir / "History.txt"

    def archive_record_path(self, stamp: str) -> Path:
        return self.memory_dir / f"Archive-{stamp}.txt"

    @property
    def tools_dir(self) -> Path:
        return self.root / "Tools"

    @property
    def register_path(self) -> Path:
        return self.tools_dir / "ToolsRegister.txt"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "Runtime"

    @property
    def events_path(self) -> Path:
        return self.runtime_dir / "events.jsonl"

    @property
    def kernel_json_path(self) -> Path:
        return self.runtime_dir / "kernel.json"

    @property
    def log_path(self) -> Path:
        return self.runtime_dir / "kernel.log"

    @property
    def control_dir(self) -> Path:
        return self.runtime_dir / "control"

    @property
    def state_path(self) -> Path:
        return self.control_dir / "state.json"

    @property
    def inbox_path(self) -> Path:
        return self.control_dir / "inbox.jsonl"

    @property
    def inbox_offset_path(self) -> Path:
        return self.control_dir / "inbox.offset"

    @property
    def wake_path(self) -> Path:
        return self.control_dir / "wake"

    @property
    def restart_path(self) -> Path:
        return self.control_dir / "restart"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "kernel.lock"

    @property
    def jobs_dir(self) -> Path:
        return self.runtime_dir / "jobs"

    @property
    def spill_dir(self) -> Path:
        return self.runtime_dir / "spill"

    # ---- 生命周期 ----
    def ensure_runtime(self) -> None:
        for path in (self.runtime_dir, self.control_dir, self.jobs_dir, self.spill_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.history_path.touch(exist_ok=True)

    def validate(self) -> None:
        if not self.root.is_dir():
            raise ArchiveError(f"存档目录不存在：{self.root}")
        if not self.config_path.is_file():
            raise ArchiveError(f"不是有效的存档：缺少 {self.config_path.name}")
        if not self.agent_path.is_file():
            # 允许缺失，但要有目录
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            if not self.agent_path.exists():
                raise ArchiveError(f"存档缺少 {self.agent_path}")

    # ---- 读取 ----
    def read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise ArchiveError(f"读取 {path} 失败：{exc}") from exc

    def registered_tools(self) -> list[str]:
        raw = self.read_text(self.register_path)
        names: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "/" in line or "\\" in line:
                raise ArchiveError(f"ToolsRegister.txt 里的名字不能带路径分隔符：{line!r}")
            if line not in names:
                names.append(line)
        return names

    def tool_docs(self) -> str:
        """已登记工具的 Usage.txt，带标题拼接。"""
        blocks: list[str] = []
        for index, name in enumerate(self.registered_tools(), start=1):
            usage = self.tools_dir / name / "Usage.txt"
            if not usage.is_file():
                blocks.append(f"## 工具 {index}：{name}\n（缺少 Usage.txt）")
                continue
            blocks.append(f"## 工具 {index}：{name}\n{usage.read_text(encoding='utf-8').strip()}")
        return "\n\n".join(blocks)

    def system_text(self) -> str:
        parts = [self.read_text(self.agent_path).strip()]
        docs = self.tool_docs()
        if docs:
            parts.append("# 工具说明\n\n" + docs)
        long_term = self.read_text(self.long_term_memory_path).strip()
        if long_term:
            parts.append("# 长期记忆\n\n" + long_term)
        digest = self.read_text(self.digest_path).strip()
        if digest:
            parts.append("# 历史概要（上一次压缩）\n\n" + digest)
        return "\n\n".join(part for part in parts if part)

    # ---- 写 ----
    def write_long_term_memory(self, text: str) -> None:
        write_atomic(self.long_term_memory_path, text)

    def write_digest(self, text: str) -> None:
        write_atomic(self.digest_path, text)

    def append_history(self, line: str) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8", newline="") as handle:
            handle.write(line.rstrip("\n") + "\n")

    def rewrite_history(self, events: list[Event]) -> None:
        write_atomic(self.history_path, render_history(events))


def render_event_line(event: Event) -> str | None:
    """把事件渲染成 History.txt 的一行；不可读事件返回 None。"""
    if event.type == "user/message":
        return f"用户：{event.data.get('text', '')}"
    if event.type == "assistant/message":
        text = event.data.get("text", "") or ""
        calls = event.data.get("tool_calls") or []
        if calls:
            rendered = ", ".join(f"{call.get('name')}({call.get('arguments')})" for call in calls)
            return f"助手：{text}\n[调用] {rendered}"
        return f"助手：{text}"
    if event.type == "tool/result":
        output = str(event.data.get("output", ""))
        if len(output) > 600:
            output = output[:400] + " ... " + output[-150:]
        status = "成功" if event.data.get("ok") else "失败"
        return f"[工具 {event.data.get('name')} {status}] {output}"
    if event.type == "system/message":
        return f"[系统] {event.data.get('text', '')}"
    if event.type == "delay":
        return f"[等待] {event.data.get('seconds')} 秒"
    if event.type == "compaction/summary":
        return f"[压缩] 已压缩至 seq {event.data.get('to_seq')}，释放约 {event.data.get('freed_tokens')} tokens"
    if event.type == "session/handover":
        return f"[交班] 新进程 {event.data.get('target')}（旧 pid {event.data.get('pid')}）"
    return None


def render_history(events: list[Event]) -> str:
    lines: list[str] = []
    for event in events:
        line = render_event_line(event)
        if line is not None:
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def create_archive(
    root: Path | str,
    *,
    name: str,
    model: str = DEFAULT_MODEL,
    api_base: str = DEFAULT_API_BASE,
    api_key: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    turn_delay: float = DEFAULT_TURN_DELAY,
) -> Archive:
    root = Path(root).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ArchiveError(f"目标目录非空，拒绝覆盖：{root}")
    archive = Archive(root=root)
    for path in (archive.memory_dir, archive.tools_dir, archive.runtime_dir):
        path.mkdir(parents=True, exist_ok=True)
    archive.ensure_runtime()

    config_lines = [
        "# fishKernel 存档配置（key = value，# 起始为注释）",
        f"name = {name}",
        f"model = {model}",
        f"api_base = {api_base}",
        f"api_key = {api_key or 'sk-...'}",
        f"max_tokens = {max_tokens}",
        f"turn_delay = {turn_delay}",
    ]
    write_atomic(archive.config_path, "\n".join(config_lines) + "\n")
    write_atomic(archive.agent_path, DEFAULT_AGENT_PROMPT.format(name=name, root=str(root)))
    write_atomic(archive.long_term_memory_path, "")
    write_atomic(archive.digest_path, "")
    write_atomic(archive.register_path, DEFAULT_TOOLS_REGISTER)
    write_atomic(archive.history_path, "")
    write_atomic(archive.events_path, "")

    # 给个示例工具目录，示范 Usage.txt 格式
    example = archive.tools_dir / "shell"
    example.mkdir(parents=True, exist_ok=True)
    write_atomic(
        example / "Usage.txt",
        "名称：shell\n用途：执行任意 shell 命令。\n用法：spawn(command=\"你的命令\")；用 job_output(job_id) 取结果。\n",
    )
    return archive


def load_kernel_state(archive: Archive) -> dict:
    try:
        raw = json.loads(archive.kernel_json_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_kernel_state(archive: Archive, state: dict) -> None:
    write_atomic(archive.kernel_json_path, json.dumps(state, ensure_ascii=False, indent=2))
