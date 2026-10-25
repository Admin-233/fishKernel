"""selfguard：阻止模型把内核自己杀掉。

`spawn` 不设白名单是设计决定，但"终止内核自身/父进程/同进程组"永远不是有用的操作，
只会让 Agent 断线并留下半写状态。这里做两道粗筛：
  1. 结构化调用（job_kill）：检查目标 pid；
  2. 命令行（spawn）：按 `; | &` 分段，段内出现 kill 类命令且数字命中自身 pid。
"""

from __future__ import annotations

import os
import re

_KILL_RE = re.compile(r"\b(?:kill|killall|pkill|killpg|taskkill)\b", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[;|&\n]")


def _self_pids() -> set[int]:
    pids = {os.getpid()}
    try:
        pids.add(os.getppid())
    except OSError:
        pass
    if hasattr(os, "getpgid"):
        try:
            pids.add(os.getpgid(0))
        except OSError:
            pass
    return {pid for pid in pids if isinstance(pid, int) and pid > 0}


def check_pid(pid: int | None) -> str | None:
    """目标 pid 是否危险；危险时返回拒绝理由。"""
    if not isinstance(pid, int) or pid <= 0:
        return "无效的 pid"
    if pid == os.getpid():
        return f"拒绝终止内核自身（pid {pid}）"
    try:
        if pid == os.getppid():
            return f"拒绝终止内核的父进程（pid {pid}）"
    except OSError:
        pass
    if hasattr(os, "getpgid"):
        try:
            if os.getpgid(pid) == os.getpgid(0):
                return f"拒绝终止内核所在进程组中的进程（pid {pid}）"
        except OSError:
            pass
    return None


def check_signal_command(command: str) -> str | None:
    """命令行里是否包含"杀掉内核自己"的意图。"""
    if not command:
        return None
    pids = _self_pids()
    for segment in _SPLIT_RE.split(command):
        if not _KILL_RE.search(segment):
            continue
        for raw in re.findall(r"\d+", segment):
            try:
                number = int(raw)
            except ValueError:
                continue
            if number in pids:
                return f"拒绝执行会终止内核自身的命令（命中 pid {number}）：{segment.strip()[:120]}"
    return None
