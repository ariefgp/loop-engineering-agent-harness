from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unicodedata
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .git_refs import validate_branch_name
from .models import ResolvedSource, Role, WorkspaceContext
from .runtime import redact_text


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REPO_SLUG = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+"
)
_LOCKS_GUARD = threading.Lock()
_REPOSITORY_LOCKS: dict[Path, threading.Lock] = {}
_AUDIT_INDEX_LIMIT = 1_000_000
_TRACE_SYSCALLS = "%file,%desc,%memory,io_uring_setup,io_uring_enter,io_uring_register"


def _validate_copied_commit(data: bytes | bytearray, oid: str) -> None:
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(data, 20_000_001)
        if (
            len(raw) > 20_000_000
            or decompressor.unconsumed_tail
            or decompressor.unused_data
            or not decompressor.eof
        ):
            raise RuntimeError("observed Git object is oversized or malformed")
        raw += decompressor.flush()
    except zlib.error as exc:
        raise RuntimeError("observed Git object is malformed") from exc
    digest = (hashlib.sha1(raw) if len(oid) == 40 else hashlib.sha256(raw)).hexdigest()
    if digest != oid:
        raise RuntimeError("observed Git object hash is invalid")
    try:
        header, body = raw.split(b"\0", 1)
        kind, size = header.split(b" ", 1)
    except ValueError as exc:
        raise RuntimeError("observed Git object header is invalid") from exc
    if kind != b"commit" or not size.isdigit() or int(size) != len(body):
        raise RuntimeError("observed branch tip is not a valid commit")


@dataclass(frozen=True)
class WorkspaceOutcome:
    final_sha: str | None
    clean: bool
    push_verified: bool
    handoff_verified: bool
    cleanup: str
    failure: str | None


class GitObjectObservation:
    """Durably preserve the assigned branch's exact ref-transition journal."""

    def __init__(self, context: WorkspaceContext, audit_path: Path) -> None:
        if context.local_branch is None:
            raise ValueError("Git commit observation requires an assigned branch")
        self.context = context
        self.objects = context.git_dir / "objects"
        self.audit_path = audit_path
        self.audit_objects = audit_path / "objects"
        self.audit_objects.mkdir(mode=0o700, parents=True, exist_ok=False)
        source_reflog = context.git_dir.joinpath(
            "logs", "refs", "heads", *context.local_branch.split("/")
        )
        self.audit_reflog = audit_path / "assigned-branch.reflog"
        self.trace_path = audit_path / "assigned-ref.trace"
        try:
            source_metadata = os.stat(source_reflog, follow_symlinks=False)
            if not stat.S_ISREG(source_metadata.st_mode):
                raise RuntimeError("assigned branch reflog is not a regular file")
            tracer = shutil.which("strace")
            if tracer is None:
                raise RuntimeError("trusted Git ref tracing is unavailable")
            probe = subprocess.run(
                [tracer, "-o", "/dev/null", "--", "/bin/true"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
            if probe.returncode != 0:
                raise RuntimeError("trusted Git ref tracing is unavailable")
            # This is a separate dispatcher-owned inode, never a writable alias
            # of worker Git metadata.  WorkerRunner's trusted strace parent logs
            # successful writes to the exact assigned ref/reflog before resuming
            # the tracee; Landlock denies the worker access to this audit tree.
            initial = _read_regular_file(source_reflog, _AUDIT_INDEX_LIMIT)
            descriptor = os.open(
                self.audit_reflog,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(descriptor, initial)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except Exception:
            shutil.rmtree(audit_path, ignore_errors=True)
            raise
        self.audit_index = audit_path / "objects.index"
        self._index_fd = os.open(
            self.audit_index,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        os.fsync(self._index_fd)
        audit_fd = os.open(audit_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(audit_fd)
        finally:
            os.close(audit_fd)
        audit_parent_fd = os.open(
            audit_path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            os.fsync(audit_parent_fd)
        finally:
            os.close(audit_parent_fd)
        self._observed: set[str] = set()
        self._seen_tips: set[str] = {context.source_sha}
        self._errors: list[str] = []
        for index in range(256):
            (self.audit_objects / f"{index:02x}").mkdir(mode=0o700)

    def _capture(self, oid: str) -> bool:
        if _OBJECT_ID.fullmatch(oid) is None or oid in self._observed:
            return oid in self._observed
        source = self.objects / oid[:2] / oid[2:]
        destination = self.audit_objects / oid[:2] / oid[2:]
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 20_000_000:
                    raise RuntimeError("unsafe observed Git object")
                data = bytearray()
                while len(data) <= 20_000_000:
                    chunk = os.read(descriptor, min(65_536, 20_000_001 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > 20_000_000:
                    raise RuntimeError("oversized observed Git object")
            finally:
                os.close(descriptor)
            _validate_copied_commit(data, oid)
            output = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o400,
            )
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(output, view):]
                os.fsync(output)
            finally:
                os.close(output)
            directory_fd = os.open(
                destination.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._record(oid)
            self._observed.add(oid)
            return True
        except FileExistsError:
            self._record(oid)
            self._observed.add(oid)
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            self._errors.append(redact_text(str(exc)))
            return False

    def _record(self, oid: str) -> None:
        row = (oid + "\n").encode("ascii")
        if os.fstat(self._index_fd).st_size + len(row) > _AUDIT_INDEX_LIMIT:
            raise RuntimeError("observed Git object index is oversized")
        view = memoryview(row)
        while view:
            view = view[os.write(self._index_fd, view):]
        os.fsync(self._index_fd)

    def _capture_audited_tips(self) -> None:
        self._append_traced_tips()
        for oid in _audit_branch_tip_oids(self.audit_reflog):
            if oid not in self._seen_tips:
                if self._capture(oid):
                    self._seen_tips.add(oid)

    def _append_traced_tips(self) -> None:
        try:
            trace = _read_regular_file(
                self.trace_path, _AUDIT_INDEX_LIMIT
            ).decode("utf-8")
        except FileNotFoundError:
            return
        except UnicodeDecodeError as exc:
            raise RuntimeError("assigned ref syscall journal is invalid") from exc
        tips, errors = _parse_ref_trace(self.context, trace)
        self._errors.extend(errors)
        if not tips:
            return
        descriptor = os.open(self.audit_reflog, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            for oid in sorted(tips):
                row = f"{'0' * len(oid)} {oid} traced ref syscall\n".encode("ascii")
                os.write(descriptor, row)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def stop(self) -> dict[str, object]:
        try:
            self._capture_audited_tips()
        except Exception as exc:
            self._errors.append(redact_text(str(exc)))
        os.close(self._index_fd)
        directory_fd = os.open(self.audit_objects, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "observed_objects": tuple(sorted(self._observed)),
            "audit_path": self.audit_path,
            "observation_error": "; ".join(self._errors) or None,
        }


def _run(
    argv: list[str], *, cwd: Path | None = None, environment: dict[str, str] | None = None
) -> str:
    result = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
        shell=False,
        cwd=cwd,
        env=environment,
    )
    if result.returncode != 0:
        detail = redact_text(result.stderr.strip()) or f"exit {result.returncode}"
        raise RuntimeError(f"git workspace operation failed: {detail}")
    return result.stdout.strip()


def _run_git(repo: Path, *arguments: str) -> str:
    return _run(["git", "-C", str(repo), *arguments])


def _run_git_dir(git_dir: Path, *arguments: str) -> str:
    return _run(
        ["git", "--git-dir", str(git_dir), *arguments],
        environment=_trusted_git_environment(git_dir.parent),
    )


def _trusted_git_environment(home: Path, objects: Path | None = None) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if objects is not None:
        environment["GIT_OBJECT_DIRECTORY"] = str(objects)
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = ""
    return environment


def _read_regular_file(path: Path, limit: int = 1_000_000) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise RuntimeError("unsafe Git metadata file")
        data = os.read(descriptor, limit + 1)
        if len(data) > limit:
            raise RuntimeError("oversized Git metadata file")
        return data
    finally:
        os.close(descriptor)


def _audit_branch_tip_oids(reflog: Path) -> set[str]:
    """Read exact committed transitions from a stable assigned-reflog inode."""
    try:
        text = _read_regular_file(reflog, _AUDIT_INDEX_LIMIT).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("assigned branch audit reflog is invalid") from exc
    if text and not text.endswith("\n"):
        raise RuntimeError("assigned branch audit reflog is truncated")
    tips: set[str] = set()
    for line in text.splitlines():
        fields = line.split(" ", 2)
        if len(fields) != 3 or any(_OBJECT_ID.fullmatch(oid) is None for oid in fields[:2]):
            raise RuntimeError("assigned branch audit reflog is invalid")
        tips.add(fields[1])
    return tips


def _traced_branch_tip_oids(
    context: WorkspaceContext, audit_path: Path | None = None
) -> set[str]:
    """Read branch-tip IDs from the trusted syscall journal, including after a crash."""
    audit_path = audit_path or context.audit_path
    if audit_path is None or context.local_branch is None:
        return set()
    try:
        trace = _read_regular_file(
            audit_path / "assigned-ref.trace", _AUDIT_INDEX_LIMIT
        ).decode("utf-8")
    except FileNotFoundError:
        return set()
    except UnicodeDecodeError as exc:
        raise RuntimeError("assigned ref syscall journal is invalid") from exc
    tips, errors = _parse_ref_trace(context, trace)
    if errors:
        raise RuntimeError("Git ref observation failed closed: " + "; ".join(errors))
    return tips


def ref_trace_argv(context: WorkspaceContext, trace_path: Path) -> list[str]:
    """Build the complete fail-closed strace boundary for worker Git refs."""
    if context.local_branch is None:
        raise ValueError("Git ref tracing requires an assigned branch")
    assigned_ref = context.git_dir.joinpath(
        "refs", "heads", *context.local_branch.split("/")
    )
    reflog = context.git_dir.joinpath(
        "logs", "refs", "heads", *context.local_branch.split("/")
    )
    packed = context.git_dir / "packed-refs"
    protected = tuple(
        path
        for base in (assigned_ref, reflog, packed)
        for path in (base, Path(str(base) + ".lock"))
    )
    return [
        "--follow-forks", "--decode-fds=path", "--string-limit=65535",
        f"--trace={_TRACE_SYSCALLS}", "--output", str(trace_path),
        *[value for path in protected for value in ("--trace-path", str(path))],
    ]


def _parse_ref_trace(
    context: WorkspaceContext, trace: str
) -> tuple[set[str], list[str]]:
    """Accept only read-only access and ordinary atomic assigned-ref updates."""
    assert context.local_branch is not None
    assigned_ref = context.git_dir.joinpath(
        "refs", "heads", *context.local_branch.split("/")
    )
    reflog = context.git_dir.joinpath(
        "logs", "refs", "heads", *context.local_branch.split("/")
    )
    packed = context.git_dir / "packed-refs"
    names = {
        str(Path(str(assigned_ref) + ".lock")): "ref-lock",
        str(assigned_ref): "ref",
        str(Path(str(reflog) + ".lock")): "reflog-lock",
        str(reflog): "reflog",
        str(Path(str(packed) + ".lock")): "packed-lock",
        str(packed): "packed",
    }
    ordered_names = sorted(names, key=len, reverse=True)
    read_only = {
        "access", "faccessat", "faccessat2", "stat", "lstat", "fstat",
        "newfstatat", "statx", "read", "readv", "pread64", "preadv",
        "preadv2", "readlink", "readlinkat", "lseek", "close", "getxattr",
        "lgetxattr", "fgetxattr", "listxattr", "llistxattr", "flistxattr",
    }
    always_forbidden = {
        "link", "linkat", "symlink", "symlinkat",
        "truncate", "ftruncate", "fallocate", "mknod", "mknodat", "mkdir",
        "mkdirat", "rmdir", "chmod", "fchmod", "fchmodat", "chown",
        "fchown", "fchownat", "lchown", "setxattr", "lsetxattr", "fsetxattr",
        "removexattr", "lremovexattr", "fremovexattr", "copy_file_range",
        "sendfile", "splice", "vmsplice", "tee", "mmap", "mmap2", "msync",
        "mremap", "io_uring_setup", "io_uring_enter", "io_uring_register",
    }
    tips: set[str] = set()
    errors: list[str] = []
    ref_transaction = False
    ref_written: str | None = None
    reflog_lock_transaction = False
    reflog_lock_wrote_oid = False
    packed_lock_transaction = False

    for raw in trace.splitlines():
        # Match complete quoted path arguments or decoded descriptor paths.  A
        # lock pathname contains its target pathname as a string prefix, so a
        # substring match misclassifies read-only ``packed-refs.lock`` access
        # as a mutation of ``packed-refs`` itself.
        matching = [
            path for path in ordered_names
            if f'"{path}"' in raw or f"<{path}>" in raw
        ]
        if not matching:
            continue
        categories = {names[path] for path in matching}
        if " = -1 " in raw or raw.rstrip().endswith("= ?"):
            continue
        call_match = re.match(r"(?:\d+\s+)?(?:<\.\.\.\s+)?([A-Za-z0-9_]+)", raw)
        if call_match is None or "<unfinished ...>" in raw or "resumed>" in raw:
            errors.append("unparseable protected-path syscall")
            continue
        call = call_match.group(1)
        packed_access = "packed" in categories
        write_open = call in {"open", "openat", "openat2", "creat"} and bool(
            re.search(r"O_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)", raw)
        )
        if packed_access and (write_open or call not in read_only | {"open", "openat", "openat2"}):
            errors.append("packed-refs mutation observed")
            continue
        if packed_access:
            continue
        if call in always_forbidden:
            errors.append(f"forbidden {call} mutation observed")
            continue
        if call in {"open", "openat", "openat2", "creat"}:
            if not write_open:
                continue
            if "ref-lock" in categories:
                if not all(flag in raw for flag in ("O_CREAT", "O_EXCL")) or ref_transaction:
                    errors.append("non-atomic assigned-ref lock open observed")
                ref_transaction = True
                ref_written = None
            elif "reflog-lock" in categories:
                if not all(flag in raw for flag in ("O_CREAT", "O_EXCL")) or reflog_lock_transaction:
                    errors.append("non-atomic assigned-reflog lock open observed")
                reflog_lock_transaction = True
                reflog_lock_wrote_oid = False
            elif "packed-lock" in categories:
                if not all(flag in raw for flag in ("O_CREAT", "O_EXCL")) or packed_lock_transaction:
                    errors.append("non-atomic packed-refs lock open observed")
                packed_lock_transaction = True
            elif "reflog" in categories:
                if "O_APPEND" not in raw or "O_TRUNC" in raw:
                    errors.append("non-append assigned-reflog open observed")
            else:
                errors.append("direct assigned-ref overwrite observed")
            continue
        if call in {"write", "writev", "pwrite64", "pwritev", "pwritev2"}:
            oids = _OBJECT_ID.findall(raw)
            if "ref-lock" in categories:
                if not ref_transaction:
                    errors.append("assigned-ref lock write occurred outside a transaction")
                elif len(oids) == 1 and re.search(
                    rf'"{oids[0]}", (?:40|64)\) = (?:40|64)$', raw
                ):
                    ref_written = oids[0]
                elif ref_written is not None and re.search(r'"\\n", 1\) = 1$', raw):
                    pass
                else:
                    errors.append("assigned-ref lock write lacked one full object id")
            elif "reflog-lock" in categories:
                if not reflog_lock_transaction or not oids:
                    errors.append("assigned-reflog lock write lacked a full object id")
                else:
                    reflog_lock_wrote_oid = True
                    tips.update(oids[1::2] or oids)
            elif "reflog" in categories:
                if len(oids) < 2:
                    errors.append("assigned-reflog append lacked full object ids")
                else:
                    tips.update(oids[1::2])
            elif "packed-lock" in categories:
                errors.append("packed-refs mutation observed")
            else:
                errors.append("direct assigned-ref write observed")
            continue
        if call in {"rename", "renameat", "renameat2"}:
            if categories == {"ref", "ref-lock"}:
                if not ref_transaction or ref_written is None:
                    errors.append("assigned-ref replacement lacked captured object id")
                else:
                    tips.add(ref_written)
                ref_transaction = False
                ref_written = None
            elif categories == {"reflog", "reflog-lock"}:
                if not reflog_lock_transaction:
                    errors.append("assigned-reflog replacement lacked captured object id")
                reflog_lock_transaction = False
                reflog_lock_wrote_oid = False
            elif categories == {"packed", "packed-lock"}:
                errors.append("packed-refs mutation observed")
                packed_lock_transaction = False
            else:
                errors.append("non-atomic protected ref rename observed")
            continue
        if call in {"unlink", "unlinkat"}:
            if categories == {"ref-lock"} and ref_transaction:
                ref_transaction = False
                ref_written = None
            elif categories == {"reflog-lock"} and reflog_lock_transaction:
                reflog_lock_transaction = False
                reflog_lock_wrote_oid = False
            elif categories == {"packed-lock"} and packed_lock_transaction:
                packed_lock_transaction = False
            else:
                errors.append(f"forbidden {call} mutation observed")
            continue
        if call == "fcntl":
            if re.search(r"\bF_GET(?:FD|FL)\b", raw):
                continue
            errors.append("unknown protected-path syscall fcntl")
            continue
        if call not in read_only:
            errors.append(f"unknown protected-path syscall {call}")

    if ref_transaction:
        errors.append("incomplete assigned-ref transaction")
    if reflog_lock_transaction:
        errors.append("incomplete assigned-reflog transaction")
    if packed_lock_transaction:
        errors.append("incomplete packed-refs transaction")
    return tips, list(dict.fromkeys(errors))


def _audit_object_ids(context: WorkspaceContext) -> set[str]:
    if context.audit_path is None:
        return set()
    try:
        text = _read_regular_file(
            context.audit_path / "objects.index", _AUDIT_INDEX_LIMIT
        ).decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("observed Git object index is invalid") from exc
    if text and not text.endswith("\n"):
        raise RuntimeError("observed Git object index is truncated")
    rows = text.splitlines()
    if any(_OBJECT_ID.fullmatch(oid) is None for oid in rows):
        raise RuntimeError("observed Git object index contains an invalid id")
    if len(rows) != len(set(rows)):
        raise RuntimeError("observed Git object index contains duplicates")
    indexed = set(rows)
    copied: set[str] = set()
    objects = context.audit_path / "objects"
    try:
        with os.scandir(objects) as fanouts:
            for fanout in fanouts:
                if (
                    len(fanout.name) != 2
                    or any(character not in "0123456789abcdef" for character in fanout.name)
                    or not fanout.is_dir(follow_symlinks=False)
                ):
                    raise RuntimeError("observed Git object storage is invalid")
                with os.scandir(fanout.path) as entries:
                    for entry in entries:
                        oid = fanout.name + entry.name
                        if (
                            _OBJECT_ID.fullmatch(oid) is None
                            or not entry.is_file(follow_symlinks=False)
                        ):
                            raise RuntimeError("observed Git object storage is invalid")
                        copied.add(oid)
                        if len(copied) > _AUDIT_INDEX_LIMIT // 41:
                            raise RuntimeError("observed Git object storage is oversized")
    except OSError as exc:
        raise RuntimeError("observed Git object storage is missing or invalid") from exc
    if copied != indexed:
        raise RuntimeError("observed Git object index does not match copied objects")
    observed = set(context.observed_objects)
    if any(_OBJECT_ID.fullmatch(oid) is None for oid in observed) or not observed <= indexed:
        raise RuntimeError("observed Git object snapshot is invalid")
    return indexed


def _observed_commit_objects(context: WorkspaceContext) -> set[str]:
    commits: set[str] = set()
    if context.audit_path is None:
        return commits
    for oid in _audit_object_ids(context):
        compressed = _read_regular_file(
            context.audit_path / "objects" / oid[:2] / oid[2:], 20_000_000
        )
        _validate_copied_commit(compressed, oid)
        commits.add(oid)
    return commits


def _packed_ref_oid(git_dir: Path, expected_ref: str) -> str:
    try:
        text = _read_regular_file(git_dir / "packed-refs").decode("ascii")
    except (UnicodeDecodeError, FileNotFoundError) as exc:
        raise RuntimeError("worker branch ref is missing or invalid") from exc
    refs: dict[str, str] = {}
    previous_ref: str | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            previous_ref = None
            continue
        if line.startswith("^"):
            if previous_ref is None or _OBJECT_ID.fullmatch(line[1:]) is None:
                raise RuntimeError("worker packed refs are malformed")
            previous_ref = None
            continue
        fields = line.split(" ")
        if len(fields) != 2:
            raise RuntimeError("worker packed refs are malformed")
        oid, ref = fields
        if (
            _OBJECT_ID.fullmatch(oid) is None
            or not ref.startswith("refs/")
            or any(character.isspace() or unicodedata.category(character) == "Cc" for character in ref)
            or any(token in ref for token in ("..", "@{", "//", "\\", "~", "^", ":", "?", "*", "["))
            or ref.endswith(("/", ".", ".lock"))
            or any(part in {"", ".", ".."} or part.startswith(".") for part in ref.split("/"))
            or ref in refs
        ):
            raise RuntimeError("worker packed refs are malformed")
        refs[ref] = oid
        previous_ref = ref
    try:
        return refs[expected_ref]
    except KeyError as exc:
        raise RuntimeError("worker branch ref is missing or invalid") from exc


def _worker_head(context: WorkspaceContext) -> tuple[str, Path]:
    admin = context.git_dir / "worktrees" / context.path.name
    if admin.resolve(strict=True) != admin or admin.parent != context.git_dir / "worktrees":
        raise RuntimeError("worker Git administration path is invalid")
    value = _read_regular_file(admin / "HEAD", 1024).decode("ascii").strip()
    if context.local_branch is None:
        if _OBJECT_ID.fullmatch(value) is None:
            raise RuntimeError("detached worker HEAD is invalid")
        return value, admin / "index"
    expected_ref = f"refs/heads/{context.local_branch}"
    if value != f"ref: {expected_ref}":
        raise RuntimeError("worker changed its assigned branch")
    ref_path = context.git_dir.joinpath(*expected_ref.split("/"))
    try:
        oid = _read_regular_file(ref_path, 1024).decode("ascii").strip()
    except FileNotFoundError:
        oid = _packed_ref_oid(context.git_dir, expected_ref)
    except UnicodeDecodeError as exc:
        raise RuntimeError("worker branch head is invalid") from exc
    if _OBJECT_ID.fullmatch(oid) is None:
        raise RuntimeError("worker branch head is invalid")
    return oid, admin / "index"


def _worker_branch_tip_oids(context: WorkspaceContext) -> set[str]:
    """Read only the assigned local branch's current and reflog tip OIDs."""
    if context.local_branch is None:
        return set()
    current, _index = _worker_head(context)
    tips = {current}
    reflog = context.git_dir.joinpath(
        "logs", "refs", "heads", *context.local_branch.split("/")
    )
    try:
        tips.update(_audit_branch_tip_oids(reflog))
    except FileNotFoundError:
        return tips
    return tips


@contextmanager
def _trusted_postflight_repository(root: Path, context: WorkspaceContext):
    final_sha, index_path = _worker_head(context)
    index = _read_regular_file(index_path)
    postflight_root = root / ".postflight"
    postflight_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inspect-", dir=postflight_root) as temporary:
        trusted = Path(temporary)
        git_dir = trusted / "git"
        git_dir.mkdir(mode=0o700)
        (git_dir / "objects").mkdir()
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "HEAD").write_text(final_sha + "\n", encoding="ascii")
        (git_dir / "index").write_bytes(index)
        (git_dir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
            f"\tworktree = {context.path}\n\tfsmonitor = false\n"
            "\thooksPath = /dev/null\n",
            encoding="utf-8",
        )
        environment = _trusted_git_environment(trusted, context.git_dir / "objects")
        yield trusted, git_dir, environment, final_sha


def _trusted_git(
    cwd: Path, git_dir: Path, environment: dict[str, str], *arguments: str
) -> str:
    return _run(
        ["git", "--git-dir", str(git_dir), *arguments], cwd=cwd, environment=environment
    )


def _authenticated_environment() -> dict[str, str]:
    # Build this environment from an allowlist: gh invokes child Git processes, so
    # removing only known-bad Git variables would leave future execution knobs and
    # unrelated ambient secrets available to both programs.
    allowed = (
        "PATH", "HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME",
        "LANG", "LC_ALL", "LC_CTYPE", "GH_TOKEN", "GITHUB_TOKEN",
    )
    environment: dict[str, str] = {
        key: value for key in allowed
        if (value := os.environ.get(key)) is not None
    }
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment.setdefault("HOME", str(Path.home()))
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": "/dev/null",
            "GIT_CONFIG_KEY_2": "core.fsmonitor",
            "GIT_CONFIG_VALUE_2": "false",
            "GIT_CONFIG_KEY_3": "init.templateDir",
            "GIT_CONFIG_VALUE_3": "/dev/null",
        }
    )
    return environment


def _authenticated_clone(slug: str, destination: Path) -> None:
    _run(
        [
            "gh", "repo", "clone", f"github.com/{slug}", str(destination), "--",
            "--bare", "--quiet", "--no-tags",
        ],
        environment=_authenticated_environment(),
    )


def _authenticated_remote_head(slug: str, branch: str) -> str | None:
    output = _run([
        "gh", "api", "--hostname", "github.com", "--method", "GET",
        f"repos/{slug}/git/ref/heads/{quote(branch, safe='')}",
    ], environment=_authenticated_environment())
    try:
        payload = json.loads(output)
        value = payload["object"]["sha"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("GitHub branch response was malformed") from exc
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise RuntimeError("GitHub branch response was malformed")
    return value


def _commit_objects(cwd: Path, git_dir: Path, environment: dict[str, str]) -> set[str]:
    rows = _trusted_git(
        cwd, git_dir, environment,
        "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)",
    )
    return {
        fields[0]
        for line in rows.splitlines()
        if len(fields := line.split()) == 2 and fields[1] == "commit"
    }


def _repository_lock(repo: Path) -> threading.Lock:
    key = repo.resolve()
    with _LOCKS_GUARD:
        return _REPOSITORY_LOCKS.setdefault(key, threading.Lock())


def _validate_origin_url(url: object) -> str:
    if not isinstance(url, str) or not url or url.startswith("-"):
        raise ValueError("invalid trusted origin URL")
    if any(unicodedata.category(character) == "Cc" for character in url):
        raise ValueError("invalid trusted origin URL")
    return url


class WorkspaceManager:
    def __init__(
        self,
        root: Path,
        *,
        clone_repository: Callable[[str, Path], None] = _authenticated_clone,
        remote_head: Callable[[str, str], str | None] = _authenticated_remote_head,
        default_repo_slug: str | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self._clone_repository = clone_repository
        self._remote_head = remote_head
        self._default_repo_slug = default_repo_slug
        if self.root == Path("/run/user") or Path("/run/user") in self.root.parents:
            raise ValueError("workspace root must be outside /run/user")

    def run_root_for(self, run_id: str, repository: Path) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run id")
        canonical = repository.resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError("repository path is not a directory")
        if self.root == canonical or self.root in canonical.parents or canonical in self.root.parents:
            raise ValueError("workspace root and canonical repository must be disjoint")
        digest = hashlib.sha256(str(canonical).encode()).hexdigest()[:16]
        return self.root / digest / run_id

    def start_object_observation(self, context: WorkspaceContext) -> GitObjectObservation:
        self._validate_context_paths(context)
        audit_path = self.root / ".audit" / context.run_root.name
        audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return GitObjectObservation(context, audit_path)

    def intent(
        self,
        run_id: str,
        repository: Path,
        expected_origin_url: str,
        source: ResolvedSource,
        role: Role,
        issue: int,
        *,
        repo_slug: str | None = None,
    ) -> WorkspaceContext:
        """Derive the complete deterministic workspace identity without creating it."""
        if _OBJECT_ID.fullmatch(source.sha) is None:
            raise ValueError("invalid source revision")
        origin_url = _validate_origin_url(expected_origin_url)
        slug = repo_slug or self._default_repo_slug
        if not isinstance(slug, str) or _REPO_SLUG.fullmatch(slug) is None:
            raise ValueError("invalid trusted repository slug")
        remote_branch = source.remote_branch
        if remote_branch is not None:
            remote_branch = validate_branch_name(remote_branch)
        if role is Role.DEV and remote_branch is None:
            remote_branch = validate_branch_name(f"loop/{issue}-{run_id[-12:]}")
        run_root = self.run_root_for(run_id, repository)
        local_branch = f"loop-harness/{run_id}" if role is Role.DEV else None
        if local_branch is not None:
            validate_branch_name(local_branch)
        return WorkspaceContext(
            path=run_root / "worktree",
            source_sha=source.sha,
            source_kind=source.kind,
            local_branch=local_branch,
            remote_branch=remote_branch,
            pr_number=source.pr_number,
            private_ref=f"refs/loop-harness/{run_id}/source",
            run_root=run_root,
            git_dir=run_root / "git",
            expected_origin_url=origin_url,
            repo_slug=slug,
        )

    def prepare(
        self,
        run_id: str,
        repository: Path,
        expected_origin_url: str,
        source: ResolvedSource,
        role: Role,
        issue: int,
        *,
        repo_slug: str | None = None,
    ) -> WorkspaceContext:
        canonical = repository.resolve(strict=True)
        context = self.intent(
            run_id, canonical, expected_origin_url, source, role, issue,
            repo_slug=repo_slug,
        )
        run_root = context.run_root
        git_dir = context.git_dir
        path = context.path

        with _repository_lock(canonical):
            run_root.parent.mkdir(parents=True, exist_ok=True)
            try:
                run_root.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise RuntimeError("run workspace already exists") from exc
            try:
                self._clone_repository(context.repo_slug, git_dir)
                _run_git_dir(git_dir, "remote", "set-url", "origin", context.expected_origin_url)
                # Bare clones disable branch reflogs by default.  The authored
                # transition audit requires Git's synchronous append journal.
                _run_git_dir(git_dir, "config", "core.logAllRefUpdates", "true")
                _run_git_dir(
                    git_dir, "config", "credential.https://github.com.helper",
                    "!gh auth git-credential",
                )
                try:
                    _run_git_dir(git_dir, "rev-parse", f"{source.sha}^{{commit}}")
                except RuntimeError:
                    _run_git_dir(
                        git_dir, "fetch", "--no-tags", "--", context.expected_origin_url,
                        f"+{source.sha}:{context.private_ref}",
                    )
                else:
                    _run_git_dir(git_dir, "update-ref", context.private_ref, source.sha)
                fetched = _run_git_dir(git_dir, "rev-parse", f"{context.private_ref}^{{commit}}")
                if fetched != source.sha:
                    raise RuntimeError("fetched source does not match resolved revision")
                if context.local_branch is None:
                    _run_git_dir(git_dir, "worktree", "add", "--detach", str(path), context.private_ref)
                else:
                    _run_git_dir(
                        git_dir,
                        "worktree",
                        "add",
                        "-b",
                        context.local_branch,
                        str(path),
                        context.private_ref,
                    )
            except Exception:
                shutil.rmtree(run_root, ignore_errors=True)
                raise

        try:
            with _trusted_postflight_repository(self.root, context) as trusted:
                cwd, trusted_git_dir, environment, _head = trusted
                baseline_commits = tuple(sorted(_commit_objects(cwd, trusted_git_dir, environment)))
            return WorkspaceContext(
                path=context.path,
                source_sha=context.source_sha,
                source_kind=context.source_kind,
                local_branch=context.local_branch,
                remote_branch=context.remote_branch,
                pr_number=context.pr_number,
                private_ref=context.private_ref,
                run_root=context.run_root,
                git_dir=context.git_dir,
                expected_origin_url=context.expected_origin_url,
                baseline_commits=baseline_commits,
                repo_slug=context.repo_slug,
                observed_objects=context.observed_objects,
                audit_path=context.audit_path,
                observation_error=context.observation_error,
            )
        except Exception:
            shutil.rmtree(run_root, ignore_errors=True)
            raise

    def _validate_context_layout(self, context: WorkspaceContext) -> None:
        try:
            context.run_root.relative_to(self.root)
        except ValueError as exc:
            raise RuntimeError("run workspace is outside workspace root") from exc
        if context.path != context.run_root / "worktree" or context.git_dir != context.run_root / "git":
            raise RuntimeError("run workspace layout is invalid")
        if not _RUN_ID.fullmatch(context.run_root.name):
            raise RuntimeError("run workspace path is invalid")
        if context.audit_path is not None:
            expected_audit = self.root / ".audit" / context.run_root.name
            if context.audit_path != expected_audit:
                raise RuntimeError("Git object audit path is invalid")

    @staticmethod
    def _validate_owned_directory(path: Path, description: str) -> None:
        try:
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise RuntimeError(f"{description} is missing or invalid")
            if path.resolve(strict=True) != path:
                raise RuntimeError(f"{description} is missing or invalid")
        except OSError as exc:
            raise RuntimeError(f"{description} is missing or invalid") from exc

    def _validate_context_paths(self, context: WorkspaceContext) -> None:
        self._validate_context_layout(context)
        if context.run_root.exists():
            self._validate_owned_directory(context.run_root, "run workspace path")
        if context.audit_path is not None:
            try:
                self._validate_owned_directory(context.audit_path, "Git object audit path")
                if context.audit_path.resolve(strict=True) != context.audit_path:
                    raise RuntimeError("Git object audit path is invalid")
                objects = context.audit_path / "objects"
                if objects.resolve(strict=True) != objects:
                    raise RuntimeError("Git object audit path is invalid")
                audit_reflog = context.audit_path / "assigned-branch.reflog"
                if audit_reflog.resolve(strict=True) != audit_reflog:
                    raise RuntimeError("Git object audit path is invalid")
            except OSError as exc:
                raise RuntimeError("Git object audit path is missing or invalid") from exc

    def inspect(
        self,
        repository: Path,
        context: WorkspaceContext,
        role: Role,
        worker_status: str,
        handoff_verifier: Callable[[str, bool], bool] | None = None,
    ) -> WorkspaceOutcome:
        del repository  # Canonical Git metadata is never part of run cleanup.
        new_commits: set[str] = set()
        unreachable_new_commits: set[str] = set()
        try:
            self._validate_context_paths(context)
            with _trusted_postflight_repository(self.root, context) as trusted:
                cwd, trusted_git_dir, environment, final_sha = trusted
                commit_type = _trusted_git(
                    cwd, trusted_git_dir, environment, "cat-file", "-t", final_sha
                )
                if commit_type != "commit":
                    raise RuntimeError("worker HEAD is not a commit")
                clean = _trusted_git(
                    cwd, trusted_git_dir, environment,
                    "status", "--porcelain=v1", "--untracked-files=all",
                ) == ""
                if context.observation_error is not None:
                    raise RuntimeError(
                        f"Git object observation failed: {context.observation_error}"
                    )
                reachable = set(
                    _trusted_git(
                        cwd, trusted_git_dir, environment, "rev-list", final_sha
                    ).splitlines()
                )
                audited_commits = _observed_commit_objects(context)
                branch_tips = _worker_branch_tip_oids(context)
                if context.audit_path is not None:
                    branch_tips.update(_audit_branch_tip_oids(
                        context.audit_path / "assigned-branch.reflog"
                    ))
                    branch_tips.update(_traced_branch_tip_oids(context))
                branch_tip_commits: set[str] = set()
                for oid in branch_tips.difference(context.baseline_commits):
                    if oid in audited_commits or oid in reachable:
                        branch_tip_commits.add(oid)
                        continue
                    try:
                        object_type = _trusted_git(
                            cwd, trusted_git_dir, environment, "cat-file", "-t", oid
                        )
                    except RuntimeError as exc:
                        raise RuntimeError(
                            "development authored branch-tip commit was abandoned after "
                            "durable syscall journal capture, but its copied object is unavailable"
                        ) from exc
                    if object_type != "commit":
                        raise RuntimeError("worker branch reflog contains a non-commit tip")
                    branch_tip_commits.add(oid)
                new_commits = (
                    reachable.difference(context.baseline_commits)
                    | audited_commits
                    | branch_tip_commits
                )
                unreachable_new_commits = new_commits.difference(reachable)
                descendant = context.source_sha in reachable
        except Exception as exc:
            return WorkspaceOutcome(
                None, False, False, False, "retained", redact_text(str(exc))
            )

        push_verified = False
        handoff_verified = False
        failure: str | None = None
        if not clean:
            failure = "workspace is dirty"
        elif role is Role.DEV and not descendant:
            failure = "development HEAD is not a descendant of the assigned source revision"
        elif role is Role.DEV and worker_status == "completed":
            has_changes = final_sha != context.source_sha or bool(new_commits)
            if not has_changes:
                pass
            elif context.remote_branch is None:
                failure = "development workspace has no publication branch"
            else:
                try:
                    remote_sha = self._remote_head(context.repo_slug, context.remote_branch)
                    push_verified = remote_sha == final_sha
                    if not push_verified:
                        failure = "local HEAD is not published to the expected remote branch"
                except Exception as exc:
                    failure = f"remote publication verification failed: {redact_text(str(exc))}"
        elif final_sha != context.source_sha:
            failure = "non-development worker changed the assigned revision"

        if role is Role.DEV and unreachable_new_commits:
            failure = "development authored commits were abandoned and not published"

        if failure is None and worker_status == "completed":
            if handoff_verifier is None:
                handoff_verified = True
            else:
                try:
                    handoff_verified = handoff_verifier(
                        final_sha,
                        role is Role.DEV
                        and (final_sha != context.source_sha or bool(new_commits)),
                    )
                except Exception:
                    handoff_verified = False
                if not handoff_verified:
                    failure = "GitHub handoff does not match the terminal workspace state"

        unpublished_commit = role is Role.DEV and (
            bool(unreachable_new_commits)
            or (bool(new_commits) and not push_verified)
            or (final_sha != context.source_sha and not push_verified)
        )
        safe_to_delete = clean and not unpublished_commit and failure is None
        if (
            worker_status != "completed"
            and clean
            and final_sha == context.source_sha
            and not unpublished_commit
        ):
            safe_to_delete = True

        cleanup = "pending" if safe_to_delete else "retained"
        return WorkspaceOutcome(
            final_sha, clean, push_verified, handoff_verified, cleanup, failure
        )

    def cleanup(
        self, context: WorkspaceContext, inspected: WorkspaceOutcome
    ) -> WorkspaceOutcome:
        if inspected.cleanup != "pending":
            return inspected
        failure = inspected.failure
        cleanup = "pending"
        try:
            self._validate_context_layout(context)
            if context.run_root.exists():
                self._validate_owned_directory(context.run_root, "run workspace path")
                shutil.rmtree(context.run_root)
            if context.audit_path is not None and context.audit_path.exists():
                self._validate_context_paths(context)
                shutil.rmtree(context.audit_path)
            cleanup = "deleted"
            failure = None
        except Exception as exc:
            failure = f"workspace cleanup failed: {redact_text(str(exc))}"
        return WorkspaceOutcome(
            inspected.final_sha,
            inspected.clean,
            inspected.push_verified,
            inspected.handoff_verified,
            cleanup,
            failure,
        )

    def finalize(
        self,
        repository: Path,
        context: WorkspaceContext,
        role: Role,
        worker_status: str,
        handoff_verifier: Callable[[str, bool], bool] | None = None,
    ) -> WorkspaceOutcome:
        """Compatibility wrapper; dispatcher uses inspect/persist/cleanup explicitly."""
        inspected = self.inspect(
            repository, context, role, worker_status, handoff_verifier
        )
        return self.cleanup(context, inspected)
