"""长输出溢出：完整内容落盘，模型只拿到有界预览与取回指引。"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .atomic import write_atomic
from .constants import SPILL_KEEP_BYTES, SPILL_KEEP_FILES


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())[:48].strip("-.")
    return cleaned or "output"


def save_text(spill_dir: Path, text: str, suggested_name: str = "output") -> dict[str, object]:
    """保存完整文本，返回 {locator, bytes, hint}；locator 相对存档根目录的 Runtime/spill。"""
    spill_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}-{_slug(suggested_name)}-{time.monotonic_ns() % 10**6:06d}.txt"
    path = spill_dir / filename
    write_atomic(path, text)
    encoded = len(text.encode("utf-8"))
    return {
        "locator": f"Runtime/spill/{filename}",
        "bytes": encoded,
        "hint": (
            f"完整内容共 {encoded} 字节，已存到 {path}。"
            "用 read_file(path=该路径, offset=行号, limit=行数) 分段读取。"
        ),
    }


def preview(text: str, *, limit_chars: int = 8_000) -> tuple[str, bool]:
    """返回 (预览, 是否被截断)。预览保留首尾，让模型知道内容结构。"""
    if len(text) <= limit_chars:
        return text, False
    head = limit_chars * 2 // 3
    tail = limit_chars - head
    return text[:head] + "\n\n...[中间内容已省略，完整内容见溢出文件]...\n\n" + text[-tail:], True


def cleanup(spill_dir: Path, *, keep_files: int = SPILL_KEEP_FILES, keep_bytes: int = SPILL_KEEP_BYTES) -> int:
    """删除过旧的溢出文件；返回删除数量。"""
    if not spill_dir.is_dir():
        return 0
    entries = sorted(
        (path for path in spill_dir.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    total = 0
    for index, path in enumerate(entries):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        if index < keep_files and total <= keep_bytes:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
