#!/usr/bin/env python3
"""检查长期记忆的磁盘文件与事件流是否一致。

## 为什么需要

`memory` 工具写的内容先进 `events.jsonl`，**只在压缩时才落盘**到
`LongTermMemory.txt`。而 `_flush_pending_memory` 遇到 `replace` 会覆盖整个文件。

于是有个陷阱：如果你曾经用 `replace` 写过记忆，之后又**手工编辑**了磁盘文件
（或反过来），两者就不一致了。压缩一旦触发，磁盘上的新版会被事件流里的旧
`replace` 覆盖——而且**没有任何报错**。

这个坑我在 2026-10-27 真的踩到了：磁盘 6068 字节的完整记忆，会
被事件流里 1620 字符的旧版覆盖。

## 用法

    python3 scripts/check_memory_sync.py <存档目录>

退出码 0 = 一致（或无需检查），1 = 不一致，2 = 参数/读取错误。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def replay(writes: list[dict]) -> str:
    """按 seq 顺序回放 memory/write，得到磁盘应有的内容。

    规则与内核的 _flush_pending_memory 一致：replace 覆盖此前全部，append 追加。
    """
    state = ""
    for event in writes:
        data = event.get("data") or {}
        mode = str(data.get("mode") or "append")
        content = str(data.get("content") or "")
        if not content.strip():
            continue
        if mode == "replace":
            state = content
        else:
            state = (state.rstrip() + "\n\n" + content) if state.strip() else content
    return state.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"用法：{argv[0]} <存档目录>", file=sys.stderr)
        return 2

    archive = Path(argv[1]).expanduser().resolve()
    memory_path = archive / "Memory" / "LongTermMemory.txt"
    events_path = archive / "Runtime" / "events.jsonl"

    if not events_path.is_file():
        print(f"✗ 找不到事件日志：{events_path}", file=sys.stderr)
        return 2

    disk = memory_path.read_text(encoding="utf-8") if memory_path.is_file() else ""

    last_replace = None
    replace_count = 0
    append_count = 0
    writes: list[dict] = []
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "memory/write":
            continue
        writes.append(event)
        data = event.get("data") or {}
        if data.get("mode") == "replace":
            replace_count += 1
            last_replace = event
        else:
            append_count += 1

    print(f"存档：{archive}")
    print(f"  磁盘 LongTermMemory.txt：{len(disk)} 字符")
    print(f"  事件流：{replace_count} 次 replace，{append_count} 次 append")

    if replace_count == 0:
        print("  ✓ 没有 replace 事件，append 只增不减，磁盘内容即最终内容")
        return 0

    content = str(last_replace.get("data", {}).get("content") or "")
    print(f"  最后一个 replace：seq {last_replace.get('seq')}，{len(content)} 字符")

    # 判断标准必须和 sync_memory.sh 一致：比对「按 seq 回放全部 memory/write」的结果。
    #
    # 早先这里只比对最后一个 replace 的内容，导致和 sync_memory.sh 打架：
    # 如果最后一个 replace 之后还有 append，回放结果 = replace + append，
    # 而这里拿 replace 原文去比，必然报不一致 —— 两个工具一个说 ✓ 一个说 ✗，
    # 看的人（包括我自己）会不知道信哪个。
    replayed = replay(writes)
    if replayed == disk.strip():
        print("  ✓ 一致 —— 磁盘内容与回放结果相同，压缩不会改变记忆")
        return 0

    # 不一致：给出可操作的诊断
    print("  ✗ 不一致！压缩触发时，磁盘内容会被事件流里的 replace 覆盖。", file=sys.stderr)
    a, b = content.strip(), disk.strip()
    for index in range(min(len(a), len(b))):
        if a[index] != b[index]:
            print(f"    首个差异在字符 {index}：", file=sys.stderr)
            print(f"      事件流：…{a[max(0, index - 30):index + 30]!r}", file=sys.stderr)
            print(f"      磁盘：  …{b[max(0, index - 30):index + 30]!r}", file=sys.stderr)
            break
    else:
        longer = "磁盘" if len(b) > len(a) else "事件流"
        print(f"    前缀相同，{longer}更长（事件 {len(a)} / 磁盘 {len(b)}）", file=sys.stderr)

    print()
    print("  修复方式（二选一）：", file=sys.stderr)
    print("    a) 想让磁盘内容生效：用 memory 工具再 replace 一次当前内容，", file=sys.stderr)
    print("       让事件流最后一条 replace 就是你要的版本；", file=sys.stderr)
    print("    b) 想让事件流内容生效：把最后那条 replace 的 content 写回磁盘。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
