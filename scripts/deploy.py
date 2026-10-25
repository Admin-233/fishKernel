"""推送 fishKernel 到测试机并跑测试。

用法：
    python scripts/deploy.py                 # 单测
    python scripts/deploy.py --real-api      # 额外跑真实 API 联调（读 FISHKERNEL_KEY）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"}


def make_tarball(repo: Path, target: Path) -> None:
    with tarfile.open(target, "w:gz") as tar:
        for path in sorted(repo.rglob("*")):
            if path.is_dir():
                if path.name in EXCLUDE_DIRS or any(part in EXCLUDE_DIRS for part in path.relative_to(repo).parts):
                    continue
                continue
            rel = path.relative_to(repo)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.suffix == ".pyc" or path.name.endswith(".egg-info"):
                continue
            tar.add(path, arcname=str(rel))


def ssh_env(password: str, workdir: Path) -> dict[str, str]:
    askpass = workdir / "askpass.cmd"
    askpass.write_text(f"@echo off\r\necho {password}\r\n", encoding="ascii")
    env = dict(os.environ)
    env["SSH_ASKPASS"] = str(askpass)
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env["DISPLAY"] = "localhost:0"
    return env


def run(cmd: list[str], env: dict[str, str], *, quiet: bool = False) -> int:
    if not quiet:
        print("$", " ".join(cmd[:3]), "...", flush=True)
    result = subprocess.run(cmd, env=env, check=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(description="推送 fishKernel 到隔离测试机并运行测试")
    parser.add_argument("--host", default=os.environ.get("FISHKERNEL_TEST_HOST"), help="测试机地址（或 FISHKERNEL_TEST_HOST）")
    parser.add_argument("--user", default=os.environ.get("FISHKERNEL_TEST_USER"), help="测试机用户（或 FISHKERNEL_TEST_USER）")
    parser.add_argument(
        "--password",
        default=os.environ.get("FISHKERNEL_TEST_PASSWORD"),
        help="SSH/ sudo 密码；不填则走 SSH 密钥 + sudo -n（或 FISHKERNEL_TEST_PASSWORD）",
    )
    parser.add_argument("--remote-dir", default=None, help="默认 /home/<user>/fishKernel")
    parser.add_argument("--real-api", action="store_true")
    parser.add_argument("--api-key", default=os.environ.get("FISHKERNEL_KEY"))
    args = parser.parse_args(argv)

    if not args.host or not args.user:
        print("缺少测试机地址或用户：用 --host/--user，或设置 FISHKERNEL_TEST_HOST/FISHKERNEL_TEST_USER", file=sys.stderr)
        return 2

    repo = Path(__file__).resolve().parent.parent
    remote_dir = args.remote_dir or f"/home/{args.user}/fishKernel"
    target = f"{args.user}@{args.host}"
    ssh_opts = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "NumberOfPasswordPrompts=1",
    ]
    if args.password:
        ssh_opts += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]

    workdir = Path(tempfile.mkdtemp(prefix="fishkernel-deploy-"))
    env = ssh_env(args.password, workdir) if args.password else dict(os.environ)
    tarball = workdir / "fishkernel.tar.gz"

    print("== 打包 ==", flush=True)
    make_tarball(repo, tarball)
    print(f"   {tarball.stat().st_size:,} 字节", flush=True)

    print("== 上传 ==", flush=True)
    code = run(["scp", *ssh_opts, "-q", str(tarball), f"{target}:/tmp/fishkernel.tar.gz"], env)
    if code != 0:
        print("scp 失败", file=sys.stderr)
        return code

    print("== 远端测试 ==", flush=True)
    sudo_env = f"FISHKERNEL_SUDO_PASSWORD='{args.password}' " if args.password else ""
    remote = (
        f"{sudo_env}"
        f"bash -c 'mkdir -p {remote_dir} && tar -xzf /tmp/fishkernel.tar.gz -C {remote_dir} "
        f"&& bash {remote_dir}/scripts/remote_test.sh'"
    )
    code = run(["ssh", *ssh_opts, "-T", target, remote], env)
    if code != 0:
        print("远端测试失败", file=sys.stderr)
        return code

    if args.real_api:
        if not args.api_key:
            print("缺少 --api-key 或环境变量 FISHKERNEL_KEY", file=sys.stderr)
            return 2
        print("== 真实 API 联调 ==", flush=True)
        code = run(
            ["ssh", *ssh_opts, "-T", target, f"FISHKERNEL_KEY='{args.api_key}' bash {remote_dir}/scripts/smoke_real.sh"],
            env,
        )
        if code != 0:
            return code

    print("全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
