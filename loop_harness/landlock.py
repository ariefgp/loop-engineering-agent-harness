from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_CGROUP_ROOT = Path("/sys/fs/cgroup").resolve()

_ACCESS_WRITE_FILE = 1 << 1
_CGROUP_MEMBERSHIP_ACCESS = _ACCESS_WRITE_FILE


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


def _syscall(number: int, *arguments: object) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = int(libc.syscall(number, *arguments))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def _existing_write_roots(scope: Path) -> list[Path]:
    resolved_scope = scope.resolve(strict=True)
    try:
        resolved_scope.relative_to(_CGROUP_ROOT)
    except ValueError as exc:
        raise RuntimeError("owned cgroup scope is outside cgroupfs") from exc
    candidates = [
        resolved_scope,
        Path.cwd(),
        Path.home(),
        Path(os.environ.get("TMPDIR", "/tmp")),
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/dev"),
        Path(f"/run/user/{os.getuid()}"),
    ]
    runtime = os.environ.get("LOOP_HARNESS_RUNTIME")
    if runtime:
        candidates.append(Path(runtime))
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved != resolved_scope:
            inside_cgroupfs = resolved == _CGROUP_ROOT or _CGROUP_ROOT in resolved.parents
            contains_cgroupfs = resolved in _CGROUP_ROOT.parents
            if inside_cgroupfs or contains_cgroupfs:
                continue
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def restrict_mutating_filesystem_access(scope: Path) -> None:
    """Prevent cgroup escape while retaining expected workspace write roots.

    Landlock ABI v1 mediates the mutations needed to alter cgroup membership.
    This is process-containment policy, not a general file-integrity sandbox.
    """
    try:
        abi = _syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            0,
            0,
            _LANDLOCK_CREATE_RULESET_VERSION,
        )
    except OSError as exc:
        raise RuntimeError("Landlock filesystem confinement is unavailable") from exc
    if abi < 1:
        raise RuntimeError("Landlock filesystem confinement is unavailable")

    ruleset_attr = _RulesetAttr(_CGROUP_MEMBERSHIP_ACCESS)
    try:
        ruleset_fd = _syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(ruleset_attr),
            ctypes.sizeof(ruleset_attr),
            0,
        )
    except OSError as exc:
        raise RuntimeError("cannot create Landlock ruleset") from exc

    try:
        for root in _existing_write_roots(scope):
            path_fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathBeneathAttr(_CGROUP_MEMBERSHIP_ACCESS, path_fd, 0)
                _syscall(
                    _SYS_LANDLOCK_ADD_RULE,
                    ruleset_fd,
                    _LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(rule),
                    0,
                )
            finally:
                os.close(path_fd)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP}:
            raise RuntimeError("Landlock filesystem confinement is unavailable") from exc
        raise RuntimeError("cannot apply Landlock filesystem confinement") from exc
    finally:
        os.close(ruleset_fd)
