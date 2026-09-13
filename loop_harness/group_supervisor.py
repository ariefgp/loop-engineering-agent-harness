from __future__ import annotations

import ctypes
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loop_harness.containment import CgroupScope


_GATE_TIMEOUT_SECONDS = 10


class _StopRequested(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def _arm_parent_death_signal() -> bool:
    parent = os.getppid()
    if parent <= 1:
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        return False
    return os.getppid() == parent


def _move(path: Path, pid: int) -> None:
    (path / "cgroup.procs").write_text(str(pid), encoding="ascii")


def _inside_mapped_root_user_namespace() -> bool:
    if os.geteuid() != 0:
        return False
    try:
        mappings = Path("/proc/self/uid_map").read_text(encoding="ascii").split()
    except OSError:
        return False
    return len(mappings) >= 3 and not (
        mappings[0] == "0" and mappings[1] == "0" and int(mappings[2]) > 1
    )


def _cleanup_cgroup(path: Path, parent: Path) -> None:
    while True:
        try:
            _move(parent, os.getpid())
            CgroupScope(path=path, parent=parent).cleanup()
            return
        except (OSError, RuntimeError):
            # The guardian must retain its lease until ownership is gone.
            time.sleep(0.1)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 6 or arguments[4] != "--":
        return 64
    try:
        gate_fd = int(arguments[0])
        lease_fd = int(arguments[1])
        if lease_fd >= 0:
            os.fstat(lease_fd)
    except (OSError, ValueError):
        return 64
    cgroup = Path(arguments[2])
    parent_cgroup = Path(arguments[3])
    target = arguments[5:]
    child: subprocess.Popen[bytes] | None = None
    cleanup_started = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise _StopRequested(signum)

    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous:
        signal.signal(signum, request_stop)
    try:
        if not _arm_parent_death_signal():
            return 70
        _move(cgroup, os.getpid())
        ready, _, _ = select.select([gate_fd], [], [], _GATE_TIMEOUT_SECONDS)
        if not ready or os.read(gate_fd, 2) != b"GO":
            return 70
        os.close(gate_fd)
        gate_fd = -1
        unshare = shutil.which("unshare")
        if unshare is None:
            print("cannot run contained command: unshare is unavailable", file=sys.stderr)
            return 70
        namespace_arguments = [unshare]
        if not _inside_mapped_root_user_namespace():
            namespace_arguments.extend(["--user", "--map-root-user"])
        namespace_arguments.extend(
            ["--pid", "--fork", "--kill-child=SIGKILL"]
        )
        child = subprocess.Popen(
            [
                *namespace_arguments,
                sys.executable,
                str(Path(__file__).with_name("namespace_exec.py")),
                "--scope",
                str(cgroup),
                "--",
                *target,
            ],
            stdin=None,
            stdout=None,
            stderr=None,
            start_new_session=True,
            shell=False,
        )
        _move(parent_cgroup, os.getpid())
        return child.wait()
    except _StopRequested as exc:
        return 128 + exc.signum
    except OSError as exc:
        print(f"cannot run heavy command: {exc}", file=sys.stderr)
        return 127
    finally:
        cleanup_started = True
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if gate_fd >= 0:
            try:
                os.close(gate_fd)
            except OSError:
                pass
        try:
            _cleanup_cgroup(cgroup, parent_cgroup)
        finally:
            if child is not None:
                try:
                    child.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            for signum, handler in previous.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
