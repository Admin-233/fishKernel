"""图片识别与历史管理测试。"""
from __future__ import annotations

import json
import zlib
import struct
from pathlib import Path

import pytest

from fishkernel.tools import (
    MAX_IMAGE_BYTES,
    ToolBox,
    _sniff_image,
)
from fishkernel.messages import MAX_LIVE_IMAGES, build_messages, image_keep_floor
from fishkernel.events import EventLog, read_events


@pytest.fixture
def toolbox(archive):
    from fishkernel.jobs import JobManager
    from fishkernel.control import Control

    return ToolBox(archive, JobManager(archive), Control(archive))


def make_png(path: Path, w: int = 8, h: int = 8) -> Path:
    """生成一张最小合法 PNG。"""
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes([200, 30, 30] * w)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    blob = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(blob)
    return path


def test_sniff_image_formats():
    assert _sniff_image(b"\x89PNG\r\n\x1a\n" + b"x" * 20) == "png"
    assert _sniff_image(b"\xff\xd8\xff\xe0" + b"x" * 20) == "jpeg"
    assert _sniff_image(b"GIF89a" + b"x" * 20) == "gif"
    assert _sniff_image(b"RIFF\x00\x00\x00\x00WEBP") == "webp"
    assert _sniff_image(b"hello world") is None


def test_read_file_returns_image_block(archive, toolbox):
    img = make_png(archive.root / "pic.png")
    box = toolbox
    result = box.dispatch("read_file", {"path": str(img)})

    assert result.ok is True
    assert len(result.images) == 1
    url = result.images[0]["url"]
    assert url.startswith("data:image/png;base64,")
    # 图片本体不能混进给模型看的 JSON 文本
    assert "base64" not in result.text
    assert "__images__" not in result.text


def test_read_file_rejects_oversized_image(archive, toolbox, monkeypatch):
    img = make_png(archive.root / "big.png")
    monkeypatch.setattr("fishkernel.tools.MAX_IMAGE_BYTES", 10)
    box = toolbox
    result = box.dispatch("read_file", {"path": str(img)})
    assert result.ok is False
    assert result.images == []


def test_image_projected_as_blocks(archive, toolbox):
    img = make_png(archive.root / "pic.png")
    box = toolbox
    result = box.dispatch("read_file", {"path": str(img)})

    log = EventLog(archive.events_path).open()
    log.append("user/message", text="看图", source="control")
    log.append(
        "assistant/message",
        text="",
        tool_calls=[{"id": "c1", "name": "read_file", "arguments": json.dumps({"path": str(img)})}],
        usage={"prompt_tokens": 10},
        pressure_tokens=10,
    )
    log.append(
        "tool/result", call_id="c1", name="read_file", ok=True,
        output=result.text, images=result.images,
    )
    log.close()

    messages = build_messages(read_events(archive.events_path), "sys")
    blocks = [m for m in messages if isinstance(m.get("content"), list)]
    assert len(blocks) == 1
    kinds = [b["type"] for b in blocks[0]["content"]]
    assert kinds == ["text", "image_url"]


def test_old_images_offloaded(archive, toolbox):
    """超过 MAX_LIVE_IMAGES 的旧图必须降级成占位符，不能再占用上下文。"""
    img = make_png(archive.root / "pic.png")
    box = toolbox
    total = MAX_LIVE_IMAGES + 2

    log = EventLog(archive.events_path).open()
    for i in range(total):
        cid = f"c{i}"
        log.append("user/message", text=f"第{i}张", source="control")
        log.append(
            "assistant/message",
            text="",
            tool_calls=[{"id": cid, "name": "read_file", "arguments": "{}"}],
            usage={"prompt_tokens": 10},
            pressure_tokens=10,
        )
        result = box.dispatch("read_file", {"path": str(img)})
        log.append(
            "tool/result", call_id=cid, name="read_file", ok=True,
            output=result.text, images=result.images,
        )
    log.close()

    events = read_events(archive.events_path)
    messages = build_messages(events, "sys")
    live = sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "image_url"
    )
    assert live == MAX_LIVE_IMAGES, f"应只保留 {MAX_LIVE_IMAGES} 张真图，实际 {live}"

    text = "\n".join(str(m.get("content")) for m in messages)
    assert "image offloaded" in text


def test_image_keep_floor():
    from fishkernel.events import Event

    def img(seq):
        return Event(seq, "", "tool/result", {"images": [{"url": "x", "path": "p"}]})

    def plain(seq):
        return Event(seq, "", "tool/result", {"output": "no image"})

    # 图片数量不超上限 → 不卸载
    assert image_keep_floor([img(1), img(2)], keep=4) == 0
    # 超上限 → 分界线是倒数第 keep 张
    events = [img(1), img(2), img(3), plain(4), img(5)]
    assert image_keep_floor(events, keep=2) == 3
