from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loop_harness.landlock import restrict_mutating_filesystem_access


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4 or arguments[0] != "--scope" or arguments[2] != "--":
        return 64
    scope = Path(arguments[1])
    target = arguments[3:]
    try:
        restrict_mutating_filesystem_access(scope)
    except RuntimeError as exc:
        print(f"cannot isolate command filesystem access: {exc}", file=sys.stderr)
        return 70
    try:
        os.execvpe(target[0], target, os.environ)
    except OSError as exc:
        print(f"cannot exec contained command: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
