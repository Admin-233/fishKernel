"""原子写在并发下的行为。

`write_atomic` 靠「临时文件 + os.replace」保证原子性。单线程测试只能证明
「写完之后内容对」，证明不了「多个写者同时写时读者永远看不到撕裂的内容」。

这个测试真的起多个进程同时写同一个文件，最后检查结果是完整的某一份，
而不是两份内容拼在一起。
"""

from __future__ import annotations

from multiprocessing import Process
from pathlib import Path

from fishkernel.atomic import write_atomic

PAYLOAD_SIZE = 2000


def _writer(target: str, writer_id: int, rounds: int) -> None:
    payload = f"writer{writer_id:02d}" + "x" * PAYLOAD_SIZE
    for _ in range(rounds):
        write_atomic(Path(target), payload + "\n")


def _reader(target: str, stop_flag, torn: list) -> None:
    """不停读文件，只要读到「不完整」的内容就记录 —— 这才是原子性要防的。"""
    import time as _t

    path = Path(target)
    while not stop_flag.is_set():
        try:
            data = path.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError):
            continue
        if not data:
            continue
        body = data.rstrip("\n")
        # 合法内容必定是 writerNN + 恰好 PAYLOAD_SIZE 个 x
        if not (body.startswith("writer") and body[8:] == "x" * PAYLOAD_SIZE):
            torn.append(body[:60])
            return
        _t.sleep(0.0005)


def test_concurrent_writers_never_tear(tmp_path):
    """多个写者 + 并发读者：读者永远不该看到「写了一半」的内容。

    只检查最终结果是不够的 —— 非原子写（先写一半、flush、再写另一半）
    最终结果也是完整的，但中间态会被读者看到。所以必须有读者参与。
    """
    from multiprocessing import Manager

    target = tmp_path / "target.txt"
    writers, rounds = 8, 10

    manager = Manager()
    stop_flag = manager.Event()
    torn = manager.list()

    readers = [Process(target=_reader, args=(str(target), stop_flag, torn)) for _ in range(3)]
    for reader in readers:
        reader.start()

    processes = [
        Process(target=_writer, args=(str(target), index, rounds))
        for index in range(writers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert not process.is_alive(), "写入进程卡住"

    stop_flag.set()
    for reader in readers:
        reader.join(timeout=30)

    assert list(torn) == [], f"读者看到了撕裂内容：{list(torn)[:2]}"

    content = target.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line]

    assert len(lines) == 1, f"文件被撕裂成 {len(lines)} 行（应只有最后一次写入的完整内容）"

    line = lines[0]
    assert line.startswith("writer"), "内容不是来自任何一个写者"
    prefix = line[:8]
    assert line[8:] == "x" * PAYLOAD_SIZE, f"{prefix} 写入的内容不完整"


def test_no_temp_files_left_behind(tmp_path):
    target = tmp_path / "target.txt"
    for index in range(5):
        write_atomic(target, f"content{index}")

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "target.txt"]
    assert leftovers == [], f"残留临时文件：{leftovers}"


def test_write_atomic_survives_same_second_writes(tmp_path):
    """连续快速写入（文件大小相同、mtime 同秒）时，最终内容必须是最后一次。"""
    target = tmp_path / "target.txt"
    for _ in range(50):
        write_atomic(target, "AAAA")
    write_atomic(target, "BBBB")
    assert target.read_text(encoding="utf-8") == "BBBB"
