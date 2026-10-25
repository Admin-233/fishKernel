from __future__ import annotations

import pytest

from fishkernel.llm import Completion, ScriptedClient
from fishkernel.lock import InstanceLock, LockError
from fishkernel.loop import Kernel


def run_once(archive) -> None:
    Kernel(
        archive.root,
        client=ScriptedClient([Completion(text="ok")]),
        max_turns=1,
        echo=False,
        error_backoff_base=0,
    ).run()


def test_second_instance_is_rejected(archive):
    first = InstanceLock(archive.lock_path)
    first.acquire(timeout=0)
    try:
        second = InstanceLock(archive.lock_path)
        with pytest.raises(LockError):
            second.acquire(timeout=0.1)
    finally:
        first.release()

    third = InstanceLock(archive.lock_path)
    third.acquire(timeout=0.1)
    third.release()


def test_lock_released_after_run(archive):
    run_once(archive)
    lock = InstanceLock(archive.lock_path)
    lock.acquire(timeout=0.1)
    lock.release()


def test_successor_argv(archive):
    from fishkernel.restart import successor_argv

    argv = successor_argv(archive, python="/usr/bin/python3")
    assert argv[:4] == ["/usr/bin/python3", "-m", "fishkernel", "run"]
    assert argv[4] == str(archive.root)
