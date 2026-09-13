from __future__ import annotations

import ctypes
import errno
import os
from collections.abc import Iterable
from pathlib import Path

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_CGROUP_ROOT = Path("/sys/fs/cgroup").resolve()

_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
_ACCESS_REFER = 1 << 13
_ACCESS_TRUNCATE = 1 << 14
_MUTATION_ACCESS_V1 = (
    _ACCESS_WRITE_FILE
    | _ACCESS_REMOVE_DIR
    | _ACCESS_REMOVE_FILE
    | _ACCESS_MAKE_CHAR
    | _ACCESS_MAKE_DIR
    | _ACCESS_MAKE_REG
    | _ACCESS_MAKE_SOCK
    | _ACCESS_MAKE_FIFO
    | _ACCESS_MAKE_BLOCK
    | _ACCESS_MAKE_SYM
)


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


def _mutation_access_for_abi(abi: int) -> int:
    access = _MUTATION_ACCESS_V1
    if abi >= 2:
        access |= _ACCESS_REFER
    if abi >= 3:
        access |= _ACCESS_TRUNCATE
    return access


def _existing_write_roots(scope: Path, allowed_roots: Iterable[Path]) -> list[Path]:
    resolved_scope = scope.resolve(strict=True)
    try:
        resolved_scope.relative_to(_CGROUP_ROOT)
    except ValueError as exc:
        raise RuntimeError("owned cgroup scope is outside cgroupfs") from exc
    candidates = [
        resolved_scope,
        *allowed_roots,
        Path("/dev/null"),
        Path("/dev/zero"),
        Path("/dev/random"),
        Path("/dev/urandom"),
    ]
    temporary = os.environ.get("TMPDIR")
    if temporary:
        candidates.append(Path(temporary))

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


def restrict_mutating_filesystem_access(
    scope: Path, allowed_roots: Iterable[Path] = ()
) -> None:
    """Restrict mutations to the owned cgroup and explicit per-run roots."""
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

    mutation_access = _mutation_access_for_abi(abi)
    ruleset_attr = _RulesetAttr(mutation_access)
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
        for root in _existing_write_roots(scope, allowed_roots):
            path_fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                allowed_access = mutation_access if root.is_dir() else _ACCESS_WRITE_FILE
                rule = _PathBeneathAttr(allowed_access, path_fd, 0)
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
