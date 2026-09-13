from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .containment import CgroupScope
from .github import required_evidence_kinds
from .runtime import (
    MAX_CAPTURE,
    BoundedTailCapture,
    ResultStore,
    RuntimePaths,
    redact_text,
)
from .scheduler import Assignment, build_profile_argv
from .workspace import ref_trace_argv


_SAFE_ENVIRONMENT = {
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "NO_COLOR",
}


def _worker_environment(paths: RuntimePaths) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key in _SAFE_ENVIRONMENT
    }
    harness_root = Path(__file__).resolve().parent.parent
    environment["LOOP_HARNESS_RUNTIME"] = str(paths.root.resolve())
    temporary = paths.root / "tmp"
    temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment["TMPDIR"] = str(temporary.resolve())
    environment["LOOP_HARNESS_HEAVY"] = str(
        (harness_root / "scripts" / "loop-engineering-heavy").resolve()
    )
    return environment


@dataclass(frozen=True)
class WorkerResult:
    run_id: str
    status: str
    exit_code: int | None
    result_path: Path


def build_task_prompt(run_id: str, assignment: Assignment, paths: RuntimePaths) -> str:
    item = assignment.candidate
    heavy = (Path(__file__).resolve().parent.parent / "scripts" / "loop-engineering-heavy").resolve()
    workspace = assignment.workspace
    workspace_instruction = ""
    if workspace is not None:
        destination = (
            f" Push commits only as HEAD:refs/heads/{workspace.remote_branch}."
            if item.role.value == "dev" and workspace.remote_branch
            else " Do not push from this workspace."
        )
        workspace_instruction = (
            f" The dispatcher owns the prepared workspace at {workspace.path}; do not "
            "create, switch, move, or delete worktrees or branches. Assigned source is "
            f"{workspace.source_sha}.{destination}"
        )
    if item.role.value == "pm":
        pr_number = "null"
        head_sha = "null"
        terminal_state = "plan approval"
        evidence = json.dumps([{
            "kind": "plan",
            "summary": "Published actionable implementation plan",
            "url": f"https://github.com/{item.repo}/issues/{item.number}#issuecomment-123456789",
        }], separators=(",", ":"))
        evidence_rule = (
            "PM plan approval requires kind plan; need confirmation requires kind blocker."
        )
    elif item.role.value == "qa":
        pr_number = str(workspace.pr_number if workspace is not None else 0)
        head_sha = f'"{workspace.source_sha if workspace is not None else "REPLACE_WITH_FULL_HEX_SHA"}"'
        terminal_state = "review ready"
        pr = workspace.pr_number if workspace is not None else "PR"
        evidence = json.dumps([
            {
                "kind": "test",
                "summary": "Executed acceptance suite with passing results",
                "url": f"https://github.com/{item.repo}/actions/runs/123456789/job/987654321",
            },
            {
                "kind": "review",
                "summary": "Reviewed exact pull request head without blockers",
                "url": f"https://github.com/{item.repo}/pull/{pr}#pullrequestreview-123456789",
            },
            {
                "kind": "screenshot",
                "summary": "Captured rendered acceptance state inline",
                "url": "https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000",
            },
        ], separators=(",", ":"))
        evidence_rule = (
            "QA evidence must include kinds test, review, and screenshot; every screenshot "
            "URL must also appear as an inline Markdown image in this same comment."
        )
    else:
        if workspace is None:
            pr_number = "null"
            head_sha = "null"
            terminal_state = "need confirmation"
            evidence = json.dumps([{
                "kind": "blocker",
                "summary": "Documented concrete blocker and required decision",
                "url": f"https://github.com/{item.repo}/issues/{item.number}#issuecomment-123456789",
            }], separators=(",", ":"))
        else:
            pr_number = str(workspace.pr_number or 0)
            head_sha = f'"{workspace.source_sha}"'
            terminal_state = "qa ready"
            evidence = json.dumps([{
                "kind": "verification",
                "summary": "Executed verification commands with passing results",
                "url": f"https://github.com/{item.repo}/actions/runs/123456789/job/987654321",
            }], separators=(",", ":"))
        evidence_rule = (
            "Dev qa ready requires kind verification linked to an exact Actions run/job or "
            "a documented PR verification comment; need confirmation requires kind blocker "
            "at an exact issue comment URL on the assigned issue when unchanged or the exact "
            "closing PR when changed. The comment must contain this run ID, blocker content, "
            "and the evidence summary; bare issue/PR pages do not qualify."
        )
    assert required_evidence_kinds(item.role, terminal_state)
    handoff = (
        "```loop-engineering-handoff\n"
        "{\n"
        '  "schema_version": 1,\n'
        f'  "run_id": "{run_id}",\n'
        f'  "role": "{item.role.value}",\n'
        f'  "state": "{terminal_state}",\n'
        f'  "issue": {item.number},\n'
        f'  "pr_number": {pr_number},\n'
        f'  "head_sha": {head_sha},\n'
        f'  "evidence": {evidence}\n'
        "}\n"
        "```"
    )
    return (
        f"Loop Engineering run {run_id}. You are {assignment.worker.profile}, "
        f"the {item.role.value} worker for {item.repo}#{item.number}. "
        f"Work on only the supplied issue. Read .agents/WORKFLOW.md, "
        f"{assignment.worker.contract}, AGENTS.md/CLAUDE.md when present, the full "
        "GitHub issue and its evidence. Follow the repository lifecycle contract, "
        "never merge or push directly to main, and leave GitHub in one truthful "
        "workflow state. Before reporting success, post exactly one final handoff "
        "block for this run across all issue comments, using this exact JSON shape. Evidence URL "
        "numeric IDs are illustrative examples and must be replaced with the true values:\n"
        f"{handoff}\n"
        "The block must contain exactly these keys; evidence entries must contain "
        "exactly kind, summary, and url, all as nonempty strings, with an https:// URL. "
        f"{evidence_rule} State must match the issue's sole state label. PR number and "
        "head SHA must match the current closing PR; PM uses null for both. Substantive "
        "durable evidence is required; stdout and prose outside the block are not proof. "
        f"{workspace_instruction} Run every install, build, server, browser, Docker, "
        f"or Supabase command through {heavy} (runtime {paths.root.resolve()}). Return "
        "a concise result with links, exact verification performed, and any blocker."
    )


def linux_process_start(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        comm_end = stat.rfind(")")
        if comm_end < 0:
            return "unknown"
        fields_after_comm = stat[comm_end + 1 :].split()
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
        return f"{boot_id}:{fields_after_comm[19]}"
    except (OSError, IndexError):
        return "unknown"


def _write_private_log(path: Path, content: str) -> tuple[str, bool]:
    redacted = redact_text(content)
    encoded = redacted.encode("utf-8", errors="replace")
    truncated = len(encoded) > MAX_CAPTURE
    encoded = encoded[-MAX_CAPTURE:]
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return encoded.decode("utf-8", errors="replace"), truncated


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _terminate_and_reap(
    process: subprocess.Popen[bytes],
    grace: float = 5,
    scope: CgroupScope | None = None,
) -> None:
    if scope is not None:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=grace)
        finally:
            scope.cleanup()
        return
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
    except (OSError, ProcessLookupError):
        pass


class WorkerRunner:
    def __init__(
        self,
        paths: RuntimePaths,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        process_start: Callable[[int], str] = linux_process_start,
        run_budget_seconds: int = 2700,
    ) -> None:
        self.paths = paths
        self._popen = popen
        self._preflight_executable = popen is subprocess.Popen
        self._process_start = process_start
        self.run_budget_seconds = run_budget_seconds
        self._active_lock = threading.Lock()
        self._active: dict[int, tuple[subprocess.Popen[bytes], CgroupScope | None]] = {}
        self._stopping = False

    def terminate_all(self) -> None:
        """Terminate every worker owned by this runner, even if one cleanup fails."""
        with self._active_lock:
            self._stopping = True
            active = list(self._active.values())
        errors: list[Exception] = []
        for process, scope in active:
            try:
                _terminate_and_reap(process, scope=scope)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(f"{len(errors)} worker cleanup operation(s) failed") from errors[0]

    def _persist(
        self,
        *,
        run_id: str,
        assignment: Assignment,
        status: str,
        exit_code: int | None,
        started: datetime,
        process_identity: str,
        stdout: str,
        stderr: str,
        streams_truncated: bool,
    ) -> WorkerResult:
        stdout_path = self.paths.logs / f"{run_id}.stdout.log"
        stderr_path = self.paths.logs / f"{run_id}.stderr.log"
        stdout, stdout_truncated = _write_private_log(stdout_path, stdout)
        stderr, stderr_truncated = _write_private_log(stderr_path, stderr)
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "exit_code": exit_code,
            "profile": assignment.worker.profile,
            "role": assignment.worker.role.value,
            "repository": assignment.candidate.repo,
            "issue": assignment.candidate.number,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "process_start": process_identity,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": streams_truncated or stdout_truncated or stderr_truncated,
        }
        if assignment.workspace is not None:
            payload.update(
                workspace_path=str(assignment.workspace.path),
                source_sha=assignment.workspace.source_sha,
                source_kind=assignment.workspace.source_kind,
                local_branch=assignment.workspace.local_branch,
                remote_branch=assignment.workspace.remote_branch,
                pr_number=assignment.workspace.pr_number,
            )
        result_path = ResultStore(self.paths.results).write(run_id, payload)
        return WorkerResult(run_id, status, exit_code, result_path)

    def run(
        self,
        run_id: str,
        assignment: Assignment,
        *,
        timeout: float,
        on_started: Callable[[int, str], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> WorkerResult:
        self.paths.ensure()
        working_path = (
            assignment.workspace.path
            if assignment.workspace is not None
            else assignment.candidate.repo_path
        )
        argv = build_profile_argv(
            assignment.worker,
            working_path,
            run_budget_seconds=self.run_budget_seconds,
        )
        run_root = (
            assignment.workspace.run_root
            if assignment.workspace is not None
            else self.paths.root / "workers" / run_id
        )
        worker_paths = self.paths.private_worker_runtime(run_root)
        prompt = build_task_prompt(run_id, assignment, worker_paths)
        started = datetime.now(UTC)
        environment = _worker_environment(worker_paths)
        if self._preflight_executable and shutil.which(
            argv[0], path=environment.get("PATH")
        ) is None:
            return self._persist(
                run_id=run_id,
                assignment=assignment,
                status="launch-failed",
                exit_code=None,
                started=started,
                process_identity="unknown",
                stdout="",
                stderr=f"No such file or executable: {argv[0]}",
                streams_truncated=False,
            )

        if assignment.workspace is not None and assignment.workspace.audit_path is not None:
            tracer = shutil.which("strace", path=environment.get("PATH"))
            if tracer is None:
                return self._persist(
                    run_id=run_id, assignment=assignment, status="launch-failed",
                    exit_code=None, started=started, process_identity="unknown",
                    stdout="", stderr="trusted Git ref tracing is unavailable",
                    streams_truncated=False,
                )

        scope = CgroupScope.create("worker") if self._preflight_executable else None
        gate_read, gate_write = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        stdout_capture = BoundedTailCapture()
        stderr_capture = BoundedTailCapture()
        capture_threads: list[threading.Thread] = []
        process_identity = "unknown"
        status = "launch-failed"
        failure = ""
        stop_heartbeat = threading.Event()
        heartbeat_errors: list[Exception] = []
        heartbeat_thread: threading.Thread | None = None
        gate_opened = False
        cleanup_error: Exception | None = None

        try:
            try:
                if scope is None:
                    supervisor_argv = [
                        sys.executable,
                        "-m",
                        "loop_harness.supervisor",
                        str(gate_read),
                        "--",
                        *argv,
                    ]
                else:
                    allowed_roots = [
                        working_path.resolve(),
                        worker_paths.logs.resolve(),
                        worker_paths.results.resolve(),
                        worker_paths.terminalization.resolve(),
                        (worker_paths.root / "tmp").resolve(),
                        worker_paths.heavy_lock.resolve(),
                    ]
                    if assignment.workspace is not None:
                        allowed_roots.append(assignment.workspace.git_dir.resolve())
                    supervisor_argv = [
                        sys.executable,
                        str(Path(__file__).with_name("group_supervisor.py")),
                        str(gate_read),
                        "-1",
                        str(scope.path),
                        str(scope.parent),
                        *[
                            value
                            for root in allowed_roots
                            for value in ("--allow-write", str(root))
                        ],
                        "--",
                        *argv,
                    ]
                if assignment.workspace is not None and assignment.workspace.audit_path is not None:
                    trace_path = assignment.workspace.audit_path / "assigned-ref.trace"
                    supervisor_argv = [
                        sys.executable,
                        str(Path(__file__).with_name("trace_guardian.py")),
                        "--",
                        tracer,
                        *ref_trace_argv(assignment.workspace, trace_path),
                        "--", *supervisor_argv,
                    ]
                process = self._popen(
                    supervisor_argv,
                    cwd=working_path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                    start_new_session=True,
                    shell=False,
                    env=environment,
                    pass_fds=(gate_read,),
                )
            finally:
                _close_fd(gate_read)
                gate_read = None

            with self._active_lock:
                stopping = self._stopping
                if not stopping:
                    self._active[process.pid] = (process, scope)
            if stopping:
                _terminate_and_reap(process, scope=scope)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                raise RuntimeError("worker runner shutdown was requested during launch")

            if process.stdout is None or process.stderr is None or process.stdin is None:
                raise RuntimeError("worker pipes were not created")
            for name, capture, stream in (
                ("stdout", stdout_capture, process.stdout),
                ("stderr", stderr_capture, process.stderr),
            ):
                thread = threading.Thread(
                    target=capture.drain,
                    args=(stream,),
                    name=f"{name}-{run_id}",
                    daemon=True,
                )
                thread.start()
                capture_threads.append(thread)

            process_identity = self._process_start(process.pid)
            if process_identity == "unknown":
                raise RuntimeError("could not establish Linux process identity")
            if on_started is not None:
                on_started(process.pid, process_identity)

            os.write(gate_write, b"GO")
            gate_opened = True
            _close_fd(gate_write)
            gate_write = None

            if on_heartbeat is not None:
                def heartbeat_loop() -> None:
                    while not stop_heartbeat.wait(60):
                        try:
                            on_heartbeat()
                        except Exception as exc:
                            heartbeat_errors.append(exc)
                            if process is not None:
                                _terminate_and_reap(process, scope=scope)
                            return

                heartbeat_thread = threading.Thread(
                    target=heartbeat_loop,
                    name=f"heartbeat-{run_id}",
                    daemon=True,
                )
                heartbeat_thread.start()

            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
            try:
                process.wait(timeout=timeout)
                status = "completed" if process.returncode == 0 else "failed"
                if self._preflight_executable:
                    _terminate_and_reap(process, grace=0.2, scope=scope)
            except subprocess.TimeoutExpired:
                status = "timeout"
                _terminate_and_reap(process, scope=scope)
            if heartbeat_errors:
                status = "failed"
                failure = f"heartbeat failed: {heartbeat_errors[0]}"
        except Exception as exc:
            status = "failed" if gate_opened else "launch-failed"
            failure = f"{status}: {exc}"
            if process is not None:
                try:
                    _terminate_and_reap(process, scope=scope)
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
        finally:
            _close_fd(gate_read)
            _close_fd(gate_write)
            stop_heartbeat.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2)
            try:
                if process is not None and process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if process is not None and process.poll() is None:
                    _terminate_and_reap(process, scope=scope)
                elif scope is not None:
                    scope.cleanup()
            except Exception as exc:
                cleanup_error = exc
            finally:
                if process is not None:
                    with self._active_lock:
                        self._active.pop(process.pid, None)
                for thread in capture_threads:
                    thread.join(timeout=2)
            capture_threads_stuck = any(thread.is_alive() for thread in capture_threads)

        if cleanup_error is not None:
            status = "failed"
            failure = f"worker cleanup failed: {cleanup_error}"
        if capture_threads_stuck:
            status = "failed"
            failure = "stream capture did not terminate"

        stdout = stdout_capture.text()
        stderr = stderr_capture.text()
        capture_error = stdout_capture.error or stderr_capture.error
        if capture_error is not None:
            status = "failed"
            failure = f"stream capture failed: {capture_error}"
        if failure:
            stderr = f"{stderr}\n{failure}\n"
        exit_code = process.returncode if process is not None else None
        return self._persist(
            run_id=run_id,
            assignment=assignment,
            status=status,
            exit_code=exit_code,
            started=started,
            process_identity=process_identity,
            stdout=stdout,
            stderr=stderr,
            streams_truncated=stdout_capture.truncated or stderr_capture.truncated,
        )
