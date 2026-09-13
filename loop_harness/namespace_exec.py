from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loop_harness.landlock import restrict_mutating_filesystem_access


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4 or arguments[0] != "--scope":
        return 64
    scope = Path(arguments[1])
    allowed_roots: list[Path] = []
    position = 2
    while position < len(arguments) and arguments[position] != "--":
        if arguments[position] != "--allow-write" or position + 1 >= len(arguments):
            return 64
        allowed_roots.append(Path(arguments[position + 1]))
        position += 2
    target = arguments[position + 1 :]
    if position >= len(arguments) or not target:
        return 64
    try:
        restrict_mutating_filesystem_access(scope, allowed_roots)
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
