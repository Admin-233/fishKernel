"""事件 → messages 投影。纯函数，便于测试与复现。"""

from __future__ import annotations

import json
from typing import Any

from .events import Event, find_last_summary_index

SUMMARY_TAG = "<compacted-summary>"
RUNTIME_PREFIX = "[runtime] "


class ProjectionError(Exception):
    """事件结构与工具配对无法投影。"""


def _tool_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in data.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        arguments = call.get("arguments")
        call_id = call.get("id")
        if not name or not call_id:
            continue
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        calls.append(
            {
                "id": str(call_id),
                "type": "function",
                "function": {"name": str(name), "arguments": arguments},
            }
        )
    return calls


# 上下文里最多保留多少张「真图」。更早的降级成占位符。
# 一张 1024x768 的图约 1000~2000 token，留太多会挤占对话空间。
MAX_LIVE_IMAGES = 4


def images_path_of(event: Event) -> str:
    """从 tool/result 事件里取图片路径，用于占位符提示。"""
    images = event.data.get("images") or []
    if images and isinstance(images[0], dict):
        return str(images[0].get("path", "?"))
    return "?"


def image_keep_floor(events: list[Event], keep: int = MAX_LIVE_IMAGES) -> int:
    """返回「真图保留区」的起点 seq：seq >= 该值的事件里图片才真正带上。

    两道闸：张数上限 keep，以及总估算预算 MAX_LIVE_IMAGE_TOKENS（按张数兜底）。
    图片总数不超过任何一道时返回 0，表示一张都不卸载。
    """
    seqs = [e.seq for e in events if e.type == "tool/result" and (e.data.get("images") or [])]
    if len(seqs) <= keep:
        return 0
    return seqs[-keep]


def _image_is_offloaded(event: Event, keep_seq: int) -> bool:
    """该事件的图片是否应被卸载（只剩占位符）。"""
    return keep_seq > 0 and event.seq < keep_seq


def _unknown_outcome(call: dict[str, Any]) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "TOOL_OUTCOME_UNKNOWN",
                "message": f"工具 {call['function']['name']} 的执行结果缺失（内核可能在上次执行中途退出），请把这次调用视为结果未知。",
            },
        },
        ensure_ascii=False,
    )


def build_messages(events: list[Event], system_text: str) -> list[dict[str, Any]]:
    """把事件投影成 OpenAI chat messages。

    - 第 0 条固定是 system。
    - 从最近一次 compaction/summary 之后开始；该概要投影成一条 user 消息。
    - assistant 的 tool_calls 必须配齐 tool 结果；缺失时补 TOOL_OUTCOME_UNKNOWN。
    """
    head: list[dict[str, Any]] = [{"role": "system", "content": system_text}]
    # 只保留最近 MAX_LIVE_IMAGES 张真图，更早的降级成占位符（见 image_keep_floor）
    keep_seq = image_keep_floor(events)
    start = find_last_summary_index(events)

    if start >= 0:
        summary_event = events[start]
        summary = str(summary_event.data.get("summary", "")).strip()
        to_seq = summary_event.data.get("to_seq")
        if summary:
            head.append(
                {
                    "role": "user",
                    "content": f"{SUMMARY_TAG}\n{summary}\n</compacted-summary>",
                }
            )
        # 摘要事件写在日志末尾，而被压缩区之后的"保留尾部"在它前面；
        # 因此按 to_seq 取尾部，而不是按摘要事件的位置。
        body = [event for event in events if event.seq > to_seq] if isinstance(to_seq, int) else events[start + 1 :]
    else:
        body = events

    tail: list[dict[str, Any]] = []
    index = 0
    while index < len(body):
        event = body[index]
        if event.type == "user/message":
            tail.append({"role": "user", "content": str(event.data.get("text", ""))})
        elif event.type == "assistant/message":
            calls = _tool_calls(event.data)
            text = str(event.data.get("text") or "")
            if not calls and not text.strip():
                index += 1
                continue
            message: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                message["tool_calls"] = calls
                tail.append(message)
                results: dict[str, Event] = {}
                cursor = index + 1
                while cursor < len(body) and body[cursor].type == "tool/result":
                    result_event = body[cursor]
                    call_id = str(result_event.data.get("call_id", ""))
                    if call_id:
                        results[call_id] = result_event
                    cursor += 1
                for call in calls:
                    result_event = results.get(call["id"])
                    if result_event is None:
                        tail.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": _unknown_outcome(call),
                            }
                        )
                    else:
                        output = str(result_event.data.get("output", ""))
                        images = result_event.data.get("images") or []
                        if images and _image_is_offloaded(result_event, keep_seq):
                            # 历史图片降级：一张图动辄上千 token，全留着会撑爆上下文。
                            # 参照 dsh 的 image-offload：只保留最近 keep 张真图，
                            # 更早的换成一行占位符，模型仍知道"这里曾有张图"。
                            images = []
                            output = (
                                output.rstrip()
                                + '\n[image offloaded: 历史图片已卸载，'
                                + f'路径 {images_path_of(result_event)}，需要时再 read_file 一次]'
                            )
                        if images:
                            # 带图片的工具结果：文本部分照常给，图片用 image 块附在
                            # 紧随其后的 user 消息里（tool 角色只接受字符串内容）。
                            tail.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": output,
                                }
                            )
                            blocks: list[dict[str, Any]] = [
                                {
                                    "type": "text",
                                    "text": f"（以上是 read_file 对图片 {images[0].get('path', '')} 的说明，图片本身在下面）",
                                }
                            ]
                            for img in images:
                                url = img.get("url")
                                if not url:
                                    continue
                                blocks.append(
                                    {"type": "image_url", "image_url": {"url": url}}
                                )
                            if len(blocks) > 1:
                                tail.append({"role": "user", "content": blocks})
                        else:
                            tail.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": output,
                                }
                            )
                index = cursor
                continue
            tail.append(message)
        elif event.type == "tool/result":
            # 没有对应 assistant 调用的孤儿结果：跳过，避免破坏配对
            pass
        elif event.type == "system/message":
            text = str(event.data.get("text", "")).strip()
            if text:
                tail.append({"role": "user", "content": RUNTIME_PREFIX + text})
        elif event.type == "delay":
            # 延时事件保留在事件流里供审计与 TUI 展示，但**不再投影进上下文**。
            # 原因：每轮一条「[runtime] 等待 N 秒」几乎不携带信息，
            # 长期运行会把上下文撑爆（实测 200 多轮就从 13 万涨到 46 万 token）。
            pass
        elif event.type == "memory/write":
            # 已登记、尚未落盘：压缩时会写进 LongTermMemory.txt
            mode = event.data.get("mode", "append")
            content = str(event.data.get("content", "")).strip()
            tail.append(
                {
                    "role": "user",
                    "content": f"{RUNTIME_PREFIX}长期记忆已登记（{mode}，压缩时落盘）：{content}",
                }
            )
        index += 1

    return head + _merge_runtime_user_messages(tail)


def _is_runtime(content: object) -> bool:
    return isinstance(content, str) and content.startswith(RUNTIME_PREFIX)


def _merge_runtime_user_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只合并双方都是 [runtime] 通知的连续 user 消息，绝不吞掉真实用户发言。"""
    merged: list[dict[str, Any]] = []
    for message in messages:
        if (
            merged
            and message["role"] == "user"
            and merged[-1]["role"] == "user"
            and _is_runtime(message.get("content"))
            and _is_runtime(merged[-1].get("content"))
        ):
            merged[-1]["content"] = merged[-1]["content"] + "\n" + str(message.get("content", ""))
        else:
            merged.append(message)
    return merged


def conversation_units(events: list[Event]) -> list[list[Event]]:
    """把最近一次压缩之后的事件切成"不可分单元"。

    一个单元是：一条 user 消息；或一条 assistant 消息 + 紧随其后的全部 tool 结果；
    或一条系统/运行时消息。其余事件（会话边界、压缩标记等）被忽略。
    """
    start = find_last_summary_index(events)
    if start >= 0:
        to_seq = events[start].data.get("to_seq")
        body = events[start + 1 :] if not isinstance(to_seq, int) else [e for e in events if e.seq > to_seq]
    else:
        body = events
    units: list[list[Event]] = []
    index = 0
    while index < len(body):
        event = body[index]
        if event.type in {"user/message", "system/message", "delay", "memory/write"}:
            units.append([event])
            index += 1
        elif event.type == "assistant/message":
            unit = [event]
            cursor = index + 1
            while cursor < len(body) and body[cursor].type == "tool/result":
                unit.append(body[cursor])
                cursor += 1
            units.append(unit)
            index = cursor
        else:
            index += 1
    return units


def project_units(units: list[list[Event]]) -> list[dict[str, Any]]:
    """把若干单元投影成消息（不含 system），供摘要调用使用。"""
    events = [event for unit in units for event in unit]
    return [message for message in build_messages(events, "") if message["role"] != "system"]
