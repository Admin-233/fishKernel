"""命令行：new / run / ctl / say / status / tui。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .archive import Archive, ArchiveError, create_archive, load_kernel_state, render_event_line
from .config import ConfigError, load_config
from .constants import DEFAULT_API_BASE, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, DEFAULT_TURN_DELAY
from .control import Control
from .events import read_events
from .jobs import JobManager
from .llm import ScriptedClient
from .lock import LockError
from .loop import Kernel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishkernel", description="让大模型无限循环运行的内核")
    parser.add_argument("--version", action="version", version=f"fishkernel {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="生成一份新存档")
    p_new.add_argument("directory", help="存档目录")
    p_new.add_argument("--name", default="agent")
    p_new.add_argument("--model", default=DEFAULT_MODEL)
    p_new.add_argument("--api-base", default=DEFAULT_API_BASE)
    p_new.add_argument("--api-key", default=None, help="不填则写占位符，运行时可用环境变量")
    p_new.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p_new.add_argument("--turn-delay", type=float, default=DEFAULT_TURN_DELAY)

    p_run = sub.add_parser("run", help="启动内核（无限循环）")
    p_run.add_argument("directory")
    p_run.add_argument("--max-turns", type=int, default=0, help="0 表示不限")
    p_run.add_argument("--once", action="store_true", help="只跑一轮（等价 --max-turns 1）")
    p_run.add_argument("--mock", action="store_true", help="不调用 API，用脚本客户端（离线自检）")
    p_run.add_argument("--quiet", action="store_true")

    p_ctl = sub.add_parser("ctl", help="控制运行状态")
    p_ctl.add_argument("directory")
    p_ctl.add_argument("action", choices=["pause", "resume", "stop", "delay", "wake", "restart"])
    p_ctl.add_argument("value", nargs="?", type=float, default=None, help="delay 的秒数")

    p_say = sub.add_parser("say", help="给 Agent 发一条用户消息")
    p_say.add_argument("directory")
    p_say.add_argument("text", nargs="+")

    p_status = sub.add_parser("status", help="查看状态与最近事件")
    p_status.add_argument("directory")
    p_status.add_argument("--tail", type=int, default=15)
    p_status.add_argument("--jobs", action="store_true", help="列出异步任务")

    p_tui = sub.add_parser("tui", help="实时观察并对话的终端界面")
    p_tui.add_argument("directory")
    p_tui.add_argument("--plain", action="store_true", help="不进入备用屏，按行输出")
    p_tui.add_argument("--snapshot", action="store_true", help="渲染一帧后退出（调试/截图）")
    p_tui.add_argument("--no-color", action="store_true", help="关闭颜色")
    p_tui.add_argument("--width", type=int, default=None, help="固定宽度（默认取终端）")
    p_tui.add_argument("--height", type=int, default=None, help="固定高度（默认取终端）")
    p_tui.add_argument("--refresh", type=float, default=0.2, help="刷新间隔秒（默认 0.2）")

    p_cost = sub.add_parser("cost", help="统计累计用量与花费")
    p_cost.add_argument("directory")
    p_cost.add_argument("--json", action="store_true", help="输出 JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "new":
            return _cmd_new(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "ctl":
            return _cmd_ctl(args)
        if args.command == "say":
            return _cmd_say(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "tui":
            return _cmd_tui(args)
        if args.command == "cost":
            return _cmd_cost(args)
    except (ArchiveError, ConfigError, ValueError, FileNotFoundError, LockError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    parser.error("未知命令")
    return 1


def _cmd_cost(args) -> int:
    """统计累计用量与花费。"""
    import json as _json

    from .events import read_events
    from .tokens import PRICE_CACHE_HIT, PRICE_CACHE_MISS, PRICE_OUTPUT, fold_usage

    archive = _open_archive(args.directory)
    totals = fold_usage(read_events(archive.events_path))

    if totals.calls == 0:
        print("还没有任何模型调用记录")
        return 0

    if args.json:
        print(_json.dumps({
            "calls": totals.calls,
            "prompt_tokens": totals.prompt_tokens,
            "cache_hit": totals.cache_hit,
            "cache_miss": totals.cache_miss,
            "output_tokens": totals.output_tokens,
            "reasoning_tokens": totals.reasoning_tokens,
            "calls_without_cache_data": totals.calls_without_cache_data,
            "cache_hit_rate": round(totals.cache_hit_rate, 4),
            "cost_yuan": round(totals.cost_yuan, 2),
        }, ensure_ascii=False, indent=2))
        return 0

    print("用量统计")
    print(f"  模型调用      {totals.calls:,} 次")
    print(f"  输入 token    {totals.prompt_tokens:,}")
    sampled = totals.calls - totals.calls_without_cache_data
    if sampled:
        note = f"（基于 {sampled:,}/{totals.calls:,} 条有缓存数据的记录）"
        print(f"  缓存命中      {totals.cache_hit_rate:.1%} {note}")
        print(f"    命中        {totals.cache_hit:,}")
        print(f"    未命中      {totals.cache_miss:,}")
    print(f"  输出 token    {totals.output_tokens:,}")
    if totals.reasoning_tokens:
        print(f"    其中推理    {totals.reasoning_tokens:,}")
    print()
    print(f"  估算花费      ¥{totals.cost_yuan:,.2f}")
    print(f"  （单价：命中 ¥{PRICE_CACHE_HIT}/M，未命中 ¥{PRICE_CACHE_MISS}/M，输出 ¥{PRICE_OUTPUT}/M）")
    return 0


def _open_archive(directory: str) -> Archive:
    archive = Archive(Path(directory).expanduser().resolve())
    archive.validate()
    archive.ensure_runtime()
    return archive


def _cmd_new(args: argparse.Namespace) -> int:
    archive = create_archive(
        args.directory,
        name=args.name,
        model=args.model,
        api_base=args.api_base,
        api_key=args.api_key,
        max_tokens=args.max_tokens,
        turn_delay=args.turn_delay,
    )
    print(f"已生成存档：{archive.root}")
    print(f"  配置：{archive.config_path}")
    print(f"  设定：{archive.agent_path}")
    print(f"  启动：fishkernel run {archive.root}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    mock = args.mock or os.environ.get("FISHKERNEL_MOCK") == "1"
    if args.mock:
        # 让交班接班进程也保持在 mock 模式（子进程继承环境变量）
        os.environ["FISHKERNEL_MOCK"] = "1"
    client = ScriptedClient(default_text="（mock）我收到了一条消息。") if mock else None
    kernel = Kernel(
        args.directory,
        client=client,
        max_turns=1 if args.once else args.max_turns,
        echo=not args.quiet,
    )
    return kernel.run()


def _cmd_ctl(args: argparse.Namespace) -> int:
    archive = _open_archive(args.directory)
    control = Control(archive)
    if args.action == "pause":
        state = control.write_state(run="pause")
    elif args.action == "resume":
        state = control.write_state(run="run")
    elif args.action == "stop":
        state = control.write_state(run="stop")
    elif args.action == "wake":
        control.request_wake()
        print("已请求唤醒。")
        return 0
    elif args.action == "restart":
        control.request_restart("cli")
        print("已请求交班重启，内核会在下一轮启动新进程并退出。")
        return 0
    else:  # delay
        if args.value is None:
            print(f"当前每轮延时：{control.read_state().get('delay')} 秒（未设置则用配置默认值）")
            return 0
        state = control.write_state(delay=args.value)
    print(json.dumps(state, ensure_ascii=False))
    return 0


def _cmd_say(args: argparse.Namespace) -> int:
    archive = _open_archive(args.directory)
    text = " ".join(args.text).strip()
    if not text:
        print("错误：消息不能为空", file=sys.stderr)
        return 2
    Control(archive).push_message(text)
    print("已投递。")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    archive = _open_archive(args.directory)
    config = load_config(archive.root)
    budget = config.budget
    kernel_state = load_kernel_state(archive)
    control = Control(archive).read_state()

    print(f"存档：{archive.root}")
    print(f"名称：{config.name}    模型：{config.spec.name} ({config.model})")
    print(f"窗口：{config.spec.context_window}  输出上限：{config.max_tokens}")
    print(f"压缩：阈值 {budget.threshold} / 保留 {budget.retain} tokens")
    print(
        "内核：{status}  pid={pid}  turn={turn}".format(
            status=kernel_state.get("status", "unknown"),
            pid=kernel_state.get("pid", "-"),
            turn=kernel_state.get("turn", 0),
        )
    )
    print(f"控制：run={control['run']}  delay={control['delay']}")
    if args.jobs:
        jobs = JobManager(archive).list()
        print(f"任务（{len(jobs)}）：")
        for job in jobs[-10:]:
            print(f"  {job.id}  {job.status:<10} exit={job.exit_code}  {job.command[:60]}")

    events = read_events(archive.events_path)
    calls, prompt_total, completion_total, last_prompt = _usage_totals(events)
    print(
        f"用量：请求 {calls} 次，prompt 累计 {prompt_total}，completion 累计 {completion_total}，"
        f"最近一次 prompt {last_prompt if last_prompt is not None else '-'}"
    )
    tail = events[-max(1, args.tail) :]
    print(f"最近 {len(tail)} 条事件（共 {len(events)}）：")
    for event in tail:
        line = render_event_line(event)
        if line is None:
            line = event.type + " " + json.dumps(event.data, ensure_ascii=False)[:160]
        print(f"  #{event.seq} {line}")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    from .tui import run_tui

    color = not args.no_color and os.environ.get("NO_COLOR") is None
    return run_tui(
        args.directory,
        plain=args.plain,
        snapshot=args.snapshot,
        color=color,
        width=args.width,
        height=args.height,
        refresh=args.refresh,
    )


def _usage_totals(events) -> tuple[int, int, int, int | None]:
    """汇总事件里的精确用量（来自 API usage）。"""
    calls = 0
    prompt_total = 0
    completion_total = 0
    last_prompt: int | None = None
    for event in events:
        if event.type != "assistant/message":
            continue
        usage = event.data.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, int):
            calls += 1
            prompt_total += prompt
            last_prompt = prompt
        if isinstance(completion, int):
            completion_total += completion
    return calls, prompt_total, completion_total, last_prompt


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
