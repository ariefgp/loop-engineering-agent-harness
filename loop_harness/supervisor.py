from __future__ import annotations

import ctypes
import os
import select
import signal
import sys


_GATE_TIMEOUT_SECONDS = 10


def _arm_parent_death_signal() -> bool:
    """Ask Linux to terminate this process when its current parent exits."""
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        return False
    return os.getppid() == parent


def main(argv: list[str] | None = None) -> int:
    if not _arm_parent_death_signal():
        return 70
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3 or arguments[1] != "--":
        return 64
    try:
        gate_fd = int(arguments[0])
    except ValueError:
        return 64
    target = arguments[2:]
    ready, _, _ = select.select([gate_fd], [], [], _GATE_TIMEOUT_SECONDS)
    if not ready or os.read(gate_fd, 2) != b"GO":
        return 70
    os.close(gate_fd)
    try:
        os.execvpe(target[0], target, os.environ)
    except OSError as exc:
        print(f"cannot exec profile: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
