"""Linux job trampoline: persistent file output without a kernel reader thread."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .atomic import write_atomic

OUTPUT_LIMIT = 16 * 1024 * 1024


def main():
    result_path, command = sys.argv[1:3]
    overflow = threading.Event()
    def capture(pipe, target):
        remaining = OUTPUT_LIMIT
        try:
            with pipe:
                while block := pipe.read1(8192):
                    accepted = block[:remaining]
                    target.write(accepted)
                    target.flush()
                    remaining -= len(accepted)
                    if len(accepted) < len(block):
                        overflow.set()
                        return
        except OSError:
            overflow.set()
    # This supervisor survives kernel replacement. Only stdout/stderr are capped;
    # files created intentionally by the command do not inherit a file-size limit.
    process = subprocess.Popen(command, shell=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    readers = [threading.Thread(target=capture, args=(pipe, target), daemon=True)
               for pipe, target in ((process.stdout, sys.stdout.buffer), (process.stderr, sys.stderr.buffer))]
    for reader in readers:
        reader.start()
    while True:
        if overflow.is_set():
            write_atomic(Path(result_path).with_name("output-limit"), "stdout/stderr limit exceeded")
            os.killpg(os.getpgrp(), signal.SIGKILL)
        if process.poll() is not None and not any(reader.is_alive() for reader in readers):
            # The readers may have set overflow while poll() was running.
            if overflow.is_set():
                continue
            break
        time.sleep(0.02)
    code = process.returncode
    # Separate result allows a replacement kernel to settle an adopted job.
    write_atomic(Path(result_path), str(code))
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
