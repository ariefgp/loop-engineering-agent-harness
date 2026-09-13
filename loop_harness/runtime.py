from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from .containment import CgroupScope


_CREDENTIAL_KEYS = (
    r"api[_-]?key|token|secret|password|passwd|private[_-]?key|"
    r"aws[_-]?access[_-]?key[_-]?id|cookie|session|database[_-]?url"
)
_REDACTIONS = (
    re.compile(
        r"(?is)()-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
        r"-----END [^-\r\n]*PRIVATE KEY-----"
    ),
    re.compile(r"(?i)(\bcookie\s*:\s*)[^\r\n]*"),
    re.compile(
        r'''(?i)(authorization\s*:\s*(?:bearer|basic)\s+)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,}\r\n]+)'''
    ),
    re.compile(
        r'''(?i)(\bbearer\s+)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,}\r\n]+)'''
    ),
    re.compile(
        rf'''(?i)((?:"|')?(?:{_CREDENTIAL_KEYS})(?:"|')?\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,}}\r\n]+)'''
    ),
    re.compile(
        rf'''(?i)((?:--)?(?:{_CREDENTIAL_KEYS})\s+)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,}}\r\n]+)'''
    ),
    re.compile(r'''(?i)(\b[a-z][a-z0-9+.-]{0,31}://)[^/\s:@]+:[^@\s/]+@'''),
    re.compile(r'''(?i)()\b(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_.-]{8,}\b'''),
)
_SECRET_KEY = re.compile(rf"(?i)(?:{_CREDENTIAL_KEYS}|authorization)")
MAX_CAPTURE = 128_000


def arm_parent_death_signal(signum: int = signal.SIGTERM) -> None:
    """Fail closed unless Linux can bind this process lifetime to its parent."""
    parent = os.getppid()
    if parent <= 1:
        raise RuntimeError("cannot arm parent-death signal without a live parent")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signum, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent:
        os.kill(os.getpid(), signum)


def redact_text(value: str) -> str:
    for pattern in _REDACTIONS:
        value = pattern.sub(r"\1[REDACTED]", value)
    return value


class BoundedTailCapture:
    def __init__(self, limit: int = MAX_CAPTURE) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.error: Exception | None = None
        self._lock = threading.Lock()

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(16_384)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                with self._lock:
                    self.data.extend(chunk)
                    excess = len(self.data) - self.limit
                    if excess > 0:
                        self.truncated = True
                        del self.data[:excess]
        except Exception as exc:
            with self._lock:
                self.error = exc
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def text(self) -> str:
        with self._lock:
            data = bytes(self.data)
            truncated = self.truncated
        if truncated:
            newline = data.find(b"\n")
            data = data[newline + 1 :] if newline >= 0 else b""
        return redact_text(data.decode("utf-8", errors="replace"))


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    @property
    def claim_lock(self) -> Path:
        return self.root / "claim.lock"

    @property
    def heavy_lock(self) -> Path:
        return self.root / "heavy.lock"

    @property
    def database(self) -> Path:
        return self.root / "runs.sqlite3"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def results(self) -> Path:
        return self.root / "results"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        for directory in (self.logs, self.results):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._file = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            return False
        self._file = file
        return True

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def fileno(self) -> int:
        if self._file is None:
            raise RuntimeError("lock is not acquired")
        return self._file.fileno()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ResultStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]"
                if _SECRET_KEY.search(str(key))
                else ResultStore._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [ResultStore._redact(item) for item in value]
        return value

    def write(self, run_id: str, payload: dict[str, Any]) -> Path:
        target = self.directory / f"{run_id}.json"
        fd, temporary = tempfile.mkstemp(prefix=f".{run_id}.", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(self._redact(payload), file, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target


class HeavyRunner:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes], grace: float = 5) -> None:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not argv:
            raise ValueError("heavy command cannot be empty")
        with FileLock(self.lock_path) as lease:
            scope = CgroupScope.create("heavy")
            gate_read, gate_write = os.pipe()
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("group_supervisor.py")),
                        str(gate_read),
                        str(lease.fileno()),
                        str(scope.path),
                        str(scope.parent),
                        "--",
                        *argv,
                    ],
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                    start_new_session=True,
                    shell=False,
                    pass_fds=(gate_read, lease.fileno()),
                )
            except BaseException:
                os.close(gate_read)
                os.close(gate_write)
                scope.cleanup()
                raise
            os.close(gate_read)
            try:
                os.write(gate_write, b"GO")
            except BaseException:
                self._terminate(process)
                scope.cleanup()
                raise
            finally:
                os.close(gate_write)
            if process.stdout is None or process.stderr is None:
                self._terminate(process)
                scope.cleanup()
                raise RuntimeError("heavy command pipes were not created")
            stdout_capture = BoundedTailCapture()
            stderr_capture = BoundedTailCapture()
            threads = [
                threading.Thread(target=stdout_capture.drain, args=(process.stdout,), daemon=True),
                threading.Thread(target=stderr_capture.drain, args=(process.stderr,), daemon=True),
            ]
            for thread in threads:
                thread.start()
            previous_handlers: dict[int, Any] = {}

            def forward_signal(signum: int, _frame: Any) -> None:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                raise SystemExit(128 + signum)

            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, forward_signal)
            timed_out = False
            capture_threads_stuck = False
            try:
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._terminate(process)
            except BaseException:
                self._terminate(process)
                scope.cleanup()
                raise
            finally:
                scope.cleanup()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
                for thread in threads:
                    thread.join(timeout=2)
                capture_threads_stuck = any(thread.is_alive() for thread in threads)
            if capture_threads_stuck:
                raise RuntimeError("heavy stream capture did not terminate")
            stdout = stdout_capture.text()
            stderr = stderr_capture.text()
            capture_error = stdout_capture.error or stderr_capture.error
            if capture_error is not None:
                raise RuntimeError(f"heavy stream capture failed: {capture_error}")
            if timed_out:
                raise subprocess.TimeoutExpired(list(argv), timeout, output=stdout, stderr=stderr)
            return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
