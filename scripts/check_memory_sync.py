"""Read-only Agent memory baseline check. No repairs or private contents are printed."""
import argparse
import json
from pathlib import Path
from fishkernel.archive import Archive
from fishkernel.memory import MemoryConflict
from fishkernel.migration import inspect

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    try:
        report = inspect(Archive(parser.parse_args().archive.expanduser().resolve()))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except MemoryConflict as exc:
        print(str(exc))
        return 1
    except (OSError, ValueError) as exc:
        print(type(exc).__name__ + ': ' + str(exc))
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
