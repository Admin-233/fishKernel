"""Explicit SSH test deployment of tracked source; uses existing SSH authentication."""
import argparse
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile

def make_tarball(repo, target):
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=repo).decode().split('\0')
    with tarfile.open(target, 'w:gz') as tar:
        for name in filter(None, names):
            path = repo / name
            if path.is_symlink() or repo not in path.resolve().parents:
                raise ValueError('Refusing source outside repository: ' + name)
            if path.is_file():
                tar.add(path, arcname=name, recursive=False)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='SSH destination, e.g. user@host')
    parser.add_argument('--remote-dir', required=True, help='New absolute test directory; must not exist')
    args = parser.parse_args()
    if args.host.startswith('-') or not args.remote_dir.startswith('/'):
        parser.error('Invalid SSH target or destination')
    repo = Path(__file__).resolve().parents[1]
    remote = shlex.quote(args.remote_dir)
    command = f'umask 077; mkdir -- {remote} && tar -xzf - -C {remote} && cd {remote} && bash scripts/remote_test.sh'
    with tempfile.TemporaryDirectory(prefix='fishkernel-source-') as tmp:
        tarball = Path(tmp) / 'source.tar.gz'
        make_tarball(repo, tarball)
        with tarball.open('rb') as source:
            return subprocess.run(['ssh', '-T', '-o', 'ConnectTimeout=15', args.host, command], stdin=source).returncode

if __name__ == '__main__':
    raise SystemExit(main())
