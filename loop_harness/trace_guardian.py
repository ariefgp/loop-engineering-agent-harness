from __future__ import annotations

import ctypes
import os
import signal
import sys


PR_SET_PDEATHSIG = 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "--" or len(arguments) == 1:
        return 64
    parent = os.getppid()
    if parent <= 1:
        return 70
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        return 70
    if os.getppid() != parent:
        return 70
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        os.execvpe(arguments[1], arguments[1:], os.environ)
    except OSError as exc:
        print(f"cannot start trusted trace boundary: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
