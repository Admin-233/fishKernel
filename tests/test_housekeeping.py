"""内核的运维钩子（housekeeping）。

背景：这台机器的 cron 对 fish 用户根本不执行任务（2026-10-27 实测：
systemctl 显示 active、crontab 有内容、进程也在，但放一个每分钟写日志的
探针等 90 秒，日志文件没被创建）。所以「每小时自动备份」从未发生。

修法是把钩子放进内核循环，每轮结束时调用存档里的 Work/periodic.sh。
这样不依赖系统服务，也不依赖 Agent 自己记得调用。
"""

from __future__ import annotations

import stat
import time

from fishkernel.archive import create_archive


class _StubLogger:
    """最小 logger：钩子会调 info 和 warn，两者都要有。"""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.messages.append(("info", message))

    def warn(self, message: str) -> None:
        self.messages.append(("warn", message))


def _make_loop(archive, log):
    """构造一个最小可用的 Kernel（钩子只需要 archive 和带 warn 的 logger）。"""
    from fishkernel.loop import Kernel

    loop = Kernel.__new__(Kernel)
    loop.archive = archive
    loop.logger = _StubLogger()
    return loop


def _install_hook(archive, body: str) -> None:
    work = archive.root / "Work"
    work.mkdir(parents=True, exist_ok=True)
    hook = work / "periodic.sh"
    hook.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def test_hook_runs_when_present(tmp_path, log):
    archive = create_archive(tmp_path / "a", name="t", api_key="sk-t")
    marker = tmp_path / "ran.txt"
    _install_hook(archive, f'echo yes >> "{marker}"\n')

    _make_loop(archive, log)._run_housekeeping()

    assert marker.is_file(), "钩子没有被执行"
    assert marker.read_text().strip() == "yes"


def test_hook_absent_is_noop(tmp_path, log):
    archive = create_archive(tmp_path / "a", name="t", api_key="sk-t")
    # 没有 Work/periodic.sh，不应抛异常
    _make_loop(archive, log)._run_housekeeping()


def test_hook_failure_does_not_propagate(tmp_path, log):
    """钩子失败绝不能拖垮主循环 —— 这是硬要求。"""
    archive = create_archive(tmp_path / "a", name="t", api_key="sk-t")
    _install_hook(archive, "exit 7\n")

    _make_loop(archive, log)._run_housekeeping()      # 不该抛


def test_hook_skipped_when_backup_fresh(tmp_path, log):
    """备份还新鲜时不该 fork —— 不然每轮都付一次进程启动成本。"""
    archive = create_archive(tmp_path / "a", name="t", api_key="sk-t")
    marker = tmp_path / "ran.txt"
    _install_hook(archive, f'echo yes >> "{marker}"\n')

    work = archive.root / "Work"
    now = int(time.time())
    (work / ".last_periodic").write_text(f"backup={now}\nidentity={now}\n", encoding="utf-8")

    _make_loop(archive, log)._run_housekeeping()
    assert not marker.is_file(), "备份新鲜时仍执行了钩子"


def test_hook_runs_when_backup_stale(tmp_path, log):
    archive = create_archive(tmp_path / "a", name="t", api_key="sk-t")
    marker = tmp_path / "ran.txt"
    _install_hook(archive, f'echo yes >> "{marker}"\n')

    work = archive.root / "Work"
    stale = int(time.time()) - 2 * 60 * 60          # 2 小时前
    (work / ".last_periodic").write_text(f"backup={stale}\nidentity={stale}\n", encoding="utf-8")

    _make_loop(archive, log)._run_housekeeping()
    assert marker.is_file(), "备份过期时应当执行钩子"


def test_hook_runs_when_backup_stale_by_content(tmp_path, log):
    """时间没到、但备份落后于最新提交时，钩子仍要执行。

    真实 bug（2026-10-27）：前置过滤只看时间戳，备份是 1 分钟前
    （< 55 分钟）就直接 return，于是「内容驱动」永远触发不了 ——
    刚提交完代码，备份就一直落后，直到 55 分钟过去才补上。
    实测：提交后等了 60 秒、内核跑了多轮，备份时间戳纹丝不动。
    """

    archive = create_archive(tmp_path / "a", name="t", api_key="sk-t")
    marker = tmp_path / "ran.txt"
    work = archive.root / "Work"
    work.mkdir(parents=True, exist_ok=True)

    # 钩子脚本
    (work / "periodic.sh").write_text(
        f'#!/usr/bin/env bash\necho yes >> "{marker}"\n', encoding="utf-8")
    (work / "periodic.sh").chmod(0o755)

    # check_fresh.sh：故意返回失败（模拟「备份落后」）
    (work / "check_fresh.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (work / "check_fresh.sh").chmod(0o755)

    # 备份时间戳是刚刚 —— 时间维度上「不需要备份」
    now = int(time.time())
    (work / ".last_periodic").write_text(f"backup={now}\nidentity={now}\n", encoding="utf-8")

    _make_loop(archive, log)._run_housekeeping()
    assert marker.is_file(), "备份落后时应当执行钩子，但前置过滤只看时间就 return 了"


def test_hook_skipped_when_time_fresh_and_content_fresh(tmp_path, log):
    """时间没到且备份是最新的 → 确实不用跑（避免每轮 fork）。"""
    archive = create_archive(tmp_path / "a", name="t", api_key="sk-t")
    marker = tmp_path / "ran.txt"
    work = archive.root / "Work"
    work.mkdir(parents=True, exist_ok=True)

    (work / "periodic.sh").write_text(
        f'#!/usr/bin/env bash\necho yes >> "{marker}"\n', encoding="utf-8")
    (work / "periodic.sh").chmod(0o755)

    # check_fresh.sh 返回成功（备份是最新的）
    (work / "check_fresh.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (work / "check_fresh.sh").chmod(0o755)

    now = int(time.time())
    (work / ".last_periodic").write_text(f"backup={now}\nidentity={now}\n", encoding="utf-8")

    _make_loop(archive, log)._run_housekeeping()
    assert not marker.is_file(), "时间和内容都新鲜时不该执行钩子"
