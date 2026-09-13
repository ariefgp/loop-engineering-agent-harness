from __future__ import annotations

import json
import io
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from loop_harness.containment import CgroupScope, _current_cgroup
from loop_harness.group_supervisor import _cleanup_cgroup
from loop_harness.models import Candidate, Role
from loop_harness.runtime import FileLock, HeavyRunner, ResultStore, RuntimePaths
from loop_harness.scheduler import Assignment, DEFAULT_WORKERS, WorkerSpec, run_concurrently
from loop_harness.worker import WorkerResult, WorkerRunner, build_task_prompt, linux_process_start


NOW = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


def assignments() -> list[Assignment]:
    result = []
    for index, worker in enumerate(DEFAULT_WORKERS, start=1):
        state = {Role.PM: "to be planned", Role.DEV: "todo", Role.QA: "qa ready"}[worker.role]
        result.append(
            Assignment(
                worker,
                Candidate(
                    repo="Example/project",
                    repo_path=Path("/srv/project"),
                    number=index,
                    title=f"Issue {index}",
                    role=worker.role,
                    state=state,
                    priority="P1",
                    state_entered_at=NOW,
                ),
            )
        )
    return result


def candidate_at(path: Path) -> Candidate:
    return replace(assignments()[1].candidate, repo_path=path)


def process_is_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    close = stat.rfind(")")
    fields = stat[close + 1 :].split() if close >= 0 else []
    return bool(fields and fields[0] != "Z")


class RuntimeTests(unittest.TestCase):
    def test_guardian_retries_cleanup_before_returning(self) -> None:
        with patch("loop_harness.group_supervisor._move"), patch(
            "loop_harness.group_supervisor.CgroupScope.cleanup",
            side_effect=[RuntimeError("retry"), None],
        ) as cleanup, patch("loop_harness.group_supervisor.time.sleep"):
            _cleanup_cgroup(Path("/scope"), Path("/parent"))
        self.assertEqual(2, cleanup.call_count)

    def test_cgroup_cleanup_removes_nested_owned_hierarchy(self) -> None:
        scope = CgroupScope.create("nested-test")
        nested = scope.path / "child" / "grandchild"
        nested.mkdir(parents=True)
        scope.cleanup()
        self.assertFalse(scope.path.exists())

    def test_heavy_target_cannot_escape_through_cgroupfs_or_proc_roots(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            outcome = root / "escape.txt"
            parent = _current_cgroup()
            command = """
import os
import subprocess
from pathlib import Path
subprocess.run(['umount', '/sys/fs/cgroup'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(
    ['unshare', '--user', '--map-root-user', '--mount', 'sh', '-c', 'umount /sys/fs/cgroup'],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
attempts = [Path({parent!r}) / 'cgroup.procs']
relative = Path({parent!r}).relative_to('/sys/fs/cgroup')
attempts.extend(path / relative / 'cgroup.procs' for path in Path('/proc').glob('[0-9]*/root/sys/fs/cgroup'))
escaped = False
try:
    os.kill({outer_pid}, 0)
    escaped = True
except OSError:
    pass
for path in attempts:
    try:
        descriptor = os.open(path, os.O_WRONLY)
        os.close(descriptor)
        escaped = True
        break
    except OSError:
        pass
Path({outcome!r}).write_text('escaped' if escaped else 'blocked', encoding='ascii')
""".format(parent=str(parent), outcome=str(outcome), outer_pid=os.getpid())
            result = HeavyRunner(root / "heavy.lock").run(
                [sys.executable, "-c", command], cwd=parent, timeout=10
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("blocked", outcome.read_text(encoding="ascii"))

    def test_heavy_runner_cleans_background_descendant_after_leader_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "descendant.pid"
            child_code = (
                "import pathlib,time; "
                "host=next(x.split()[1] for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('NSpid:')); "
                f"pathlib.Path({str(pid_file)!r}).write_text(host); time.sleep(60)"
            )
            command = (
                "import subprocess,sys,pathlib,time; "
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); "
                f"f=pathlib.Path({str(pid_file)!r}); d=time.monotonic()+3; "
                "\nwhile not f.exists() and time.monotonic() < d:\n    time.sleep(0.01)\n"
            )
            result = HeavyRunner(root / "heavy.lock").run(
                [sys.executable, "-c", command], timeout=10
            )
            self.assertEqual(0, result.returncode)
            pid = int(pid_file.read_text(encoding="utf-8"))
            try:
                deadline = time.monotonic() + 5
                while process_is_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_running(pid))
            finally:
                if process_is_running(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_heavy_wrapper_sigkill_does_not_orphan_command_group(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_pid_file = root / "heavy.pid"
            wrapper_pid_file = root / "wrapper.pid"
            cgroup_parent = _current_cgroup()
            existing_scopes = {path.name for path in cgroup_parent.glob("loop-heavy-*")}
            wrapper = Path(__file__).resolve().parents[1] / "scripts" / "loop-engineering-heavy"
            child_code = (
                "import pathlib,time; "
                "host=next(x.split()[1] for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('NSpid:')); "
                f"pathlib.Path({str(child_pid_file)!r}).write_text(host); "
                "time.sleep(60)"
            )
            target = (
                "import subprocess,sys,time,pathlib; "
                "relative=next(x[3:] for x in pathlib.Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::')); "
                "nested=pathlib.Path('/sys/fs/cgroup')/relative.lstrip('/')/'nested'; nested.mkdir(); "
                f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                "(nested/'cgroup.procs').write_text(str(p.pid)); "
                "time.sleep(60)"
            )
            parent = (
                "import os,subprocess,sys,time,pathlib,signal; "
                "e=os.environ.copy(); "
                f"e['LOOP_HARNESS_RUNTIME']={str(root / 'runtime')!r}; "
                f"p=subprocess.Popen([{str(wrapper)!r},sys.executable,'-c',{target!r}],"
                "env=e,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); "
                f"pathlib.Path({str(wrapper_pid_file)!r}).write_text(str(p.pid)); "
                f"f=pathlib.Path({str(child_pid_file)!r}); d=time.monotonic()+5; "
                "\nwhile not f.exists() and time.monotonic() < d:\n    time.sleep(0.02)\n"
                "os.kill(p.pid,signal.SIGKILL); "
                "os._exit(0)"
            )
            subprocess.run([sys.executable, "-c", parent], check=True)
            self.assertTrue(child_pid_file.exists())
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            wrapper_pid = int(wrapper_pid_file.read_text(encoding="utf-8"))
            try:
                deadline = time.monotonic() + 8
                while process_is_running(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_running(child_pid))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    current = {path.name for path in cgroup_parent.glob("loop-heavy-*")}
                    if current == existing_scopes:
                        break
                    time.sleep(0.02)
                self.assertEqual(
                    existing_scopes,
                    {path.name for path in cgroup_parent.glob("loop-heavy-*")},
                )
            finally:
                if process_is_running(wrapper_pid):
                    os.killpg(wrapper_pid, signal.SIGKILL)
                elif process_is_running(child_pid):
                    os.kill(child_pid, signal.SIGKILL)

    def test_heavy_wrapper_sigterm_terminates_its_command_group(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "heavy.pid"
            wrapper = Path(__file__).resolve().parents[1] / "scripts" / "loop-engineering-heavy"
            environment = os.environ.copy()
            environment["LOOP_HARNESS_RUNTIME"] = str(root / "runtime")
            process = subprocess.Popen(
                [
                    str(wrapper),
                    sys.executable,
                    "-c",
                    "import time,pathlib; "
                    "host=next(x.split()[1] for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('NSpid:')); "
                    f"pathlib.Path({str(pid_file)!r}).write_text(host); "
                    "time.sleep(60)",
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
                deadline = time.monotonic() + 5
                while process_is_running(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_running(child_pid))
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
    def test_runtime_directory_and_results_are_private_and_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = RuntimePaths(Path(tmp) / "runtime")
            paths.ensure()
            self.assertEqual(0o700, paths.root.stat().st_mode & 0o777)

            store = ResultStore(paths.results)
            path = store.write(
                "run-1",
                {
                    "status": "failed",
                    "stdout": "Authorization: Bearer secret-value",
                    "stderr": "GH_TOKEN=secret-value",
                    "api_key": "structured-secret",
                },
            )
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            content = path.read_text()
            self.assertNotIn("secret-value", content)
            self.assertNotIn("structured-secret", content)
            self.assertIn("[REDACTED]", content)
            self.assertEqual("failed", json.loads(content)["status"])

    def test_result_redaction_handles_quoted_assignments_and_json_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp))
            path = store.write(
                "run-quoted",
                {
                    "stdout": "GH_TOKEN='shell secret' API_KEY=\"double secret\"",
                    "stderr": '\"password\": \"json secret\", \"token\":\"compact secret\"',
                },
            )

            content = path.read_text(encoding="utf-8")
            for secret in ("shell secret", "double secret", "json secret", "compact secret"):
                self.assertNotIn(secret, content)
            self.assertGreaterEqual(content.count("[REDACTED]"), 4)

    def test_result_redaction_handles_common_authorization_url_and_token_forms(self) -> None:
        with TemporaryDirectory() as tmp:
            path = ResultStore(Path(tmp)).write(
                "run-common",
                {
                    "stdout": (
                        "Authorization: Basic dXNlcjpwYXNz Bearer abc.def.ghi "
                        "https://user:pass@example.com/path "
                        "ghp_123456789012345678901234567890123456"
                    )
                },
            )
            content = path.read_text(encoding="utf-8")
            for secret in (
                "dXNlcjpwYXNz",
                "abc.def.ghi",
                "user:pass",
                "ghp_123456789012345678901234567890123456",
            ):
                self.assertNotIn(secret, content)

    def test_redaction_covers_reported_keys_headers_pem_and_database_uris(self) -> None:
        secrets = (
            "AKIAIOSFODNN7EXAMPLE",
            "legacy-passwd",
            "session-secret",
            "cookie-secret",
            "database-password",
            "PRIVATE-BODY-SECRET",
        )
        value = (
            "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE PASSWD='legacy-passwd' "
            "session=session-secret\n"
            "Cookie: sid=cookie-secret; theme=dark\n"
            "DATABASE_URL=postgresql://dbuser:database-password@db.example/app\n"
            "-----BEGIN PRIVATE KEY-----\nPRIVATE-BODY-SECRET\n-----END PRIVATE KEY-----"
        )
        with TemporaryDirectory() as tmp:
            path = ResultStore(Path(tmp)).write("run-reported", {"stdout": value})
            content = path.read_text(encoding="utf-8")
        for secret in secrets:
            self.assertNotIn(secret, content)
        self.assertGreaterEqual(content.count("[REDACTED]"), len(secrets))

    def test_redaction_covers_whitespace_separated_credential_arguments(self) -> None:
        value = "tool --token cli-secret password second-secret --cookie session-secret"
        with TemporaryDirectory() as tmp:
            path = ResultStore(Path(tmp)).write("run-argv", {"stdout": value})
            content = path.read_text(encoding="utf-8")
        for secret in ("cli-secret", "second-secret", "session-secret"):
            self.assertNotIn(secret, content)
        self.assertGreaterEqual(content.count("[REDACTED]"), 3)

    def test_short_file_lock_does_not_remain_held_after_context(self) -> None:
        with TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "claim.lock"
            first = FileLock(lock_path)
            second = FileLock(lock_path)
            with first:
                self.assertFalse(second.try_acquire())
            self.assertTrue(second.try_acquire())
            second.release()

    def test_six_worker_functions_overlap(self) -> None:
        active = 0
        peak = 0
        guard = threading.Lock()
        barrier = threading.Barrier(6)

        def execute(item: Assignment) -> str:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=3)
            with guard:
                active -= 1
            return item.worker.profile

        results = run_concurrently(assignments(), execute)
        self.assertEqual(6, peak)
        self.assertEqual(
            {worker.profile for worker in DEFAULT_WORKERS},
            set(results),
        )

    def test_heavy_runner_serializes_only_child_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = RuntimePaths(Path(tmp) / "runtime")
            paths.ensure()
            heavy = HeavyRunner(paths.heavy_lock)
            intervals: list[tuple[float, float]] = []
            guard = threading.Lock()

            def invoke() -> None:
                start = time.monotonic()
                completed = heavy.run(
                    [sys.executable, "-c", "import time; time.sleep(0.15)"],
                    timeout=2,
                )
                end = time.monotonic()
                self.assertEqual(0, completed.returncode)
                with guard:
                    intervals.append((start, end))

            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(2, len(intervals))
            elapsed = max(end for _, end in intervals) - min(start for start, _ in intervals)
            self.assertGreaterEqual(elapsed, 0.25)

    def test_heavy_runner_returns_bounded_redacted_stream_tails(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = RuntimePaths(Path(tmp) / "runtime")
            paths.ensure()
            result = HeavyRunner(paths.heavy_lock).run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "sys.stdout.write('A' * 300000 + '\\nPASSWD=stdout-secret\\n'); "
                        "sys.stderr.write('B' * 300000 + "
                        "'\\npostgresql://user:stderr-secret@db/app\\n')"
                    ),
                ],
                timeout=10,
            )
        self.assertEqual(0, result.returncode)
        self.assertLessEqual(len(result.stdout.encode()), 128_000)
        self.assertLessEqual(len(result.stderr.encode()), 128_000)
        self.assertNotIn("stdout-secret", result.stdout)
        self.assertNotIn("stderr-secret", result.stderr)
        self.assertIn("[REDACTED]", result.stdout)
        self.assertIn("[REDACTED]", result.stderr)


class WorkerTests(unittest.TestCase):
    def test_shutdown_during_launch_cannot_register_a_late_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "profile"
            executable.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
            executable.chmod(0o755)
            launched = threading.Event()
            release = threading.Event()
            spawned: list[subprocess.Popen[bytes]] = []

            def delayed_popen(*args, **kwargs):
                process = subprocess.Popen(*args, **kwargs)
                spawned.append(process)
                launched.set()
                release.wait(5)
                return process

            worker = WorkerSpec("test", Role.DEV, ".agents/agent-dev.md", str(executable))
            runner = WorkerRunner(
                RuntimePaths(root / "runtime"),
                popen=delayed_popen,
                process_start=lambda pid: f"boot:{pid}",
            )
            results: list[WorkerResult] = []
            thread = threading.Thread(
                target=lambda: results.append(
                    runner.run(
                        "run-launch-race",
                        Assignment(worker, candidate_at(root)),
                        timeout=10,
                    )
                )
            )
            thread.start()
            self.assertTrue(launched.wait(5))
            runner.terminate_all()
            release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual("launch-failed", results[0].status)
            self.assertFalse(runner._active)
            self.assertIsNotNone(spawned[0].returncode)

    def test_terminate_all_attempts_every_worker_after_one_cleanup_failure(self) -> None:
        runner = WorkerRunner(RuntimePaths(Path("/tmp/runtime")))
        first = object()
        second = object()
        runner._active = {1: (first, None), 2: (second, None)}  # type: ignore[assignment]
        with patch(
            "loop_harness.worker._terminate_and_reap",
            side_effect=[RuntimeError("first failed"), None],
        ) as terminate:
            with self.assertRaisesRegex(RuntimeError, "1 worker cleanup"):
                runner.terminate_all()
        self.assertEqual(2, terminate.call_count)

    def test_worker_can_invoke_nested_heavy_wrapper(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "heavy-ok"
            executable = root / "profile"
            heavy_code = (
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')"
            )
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os,subprocess,sys\n"
                f"subprocess.run([os.environ['LOOP_HARNESS_HEAVY'], sys.executable, '-c', {heavy_code!r}], check=True)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            worker = WorkerSpec("test", Role.DEV, ".agents/agent-dev.md", str(executable))
            assignment = Assignment(
                worker,
                Candidate(
                    repo="Example/project",
                    repo_path=root,
                    number=11,
                    title="heavy work",
                    state="todo",
                    priority="P1",
                    role=Role.DEV,
                    state_entered_at=NOW,
                ),
            )
            result = WorkerRunner(
                RuntimePaths(root / "runtime"), process_start=lambda pid: f"boot:{pid}"
            ).run("run-heavy", assignment, timeout=15)
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", result.status, payload["stderr"])
            self.assertEqual("ok", marker.read_text(encoding="utf-8"))

    def test_worker_cleans_background_descendant_after_profile_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "descendant.pid"
            executable = root / "profile"
            executable.write_text(
                "#!/usr/bin/env python3\nimport subprocess,sys,pathlib,time\n"
                "code=\"import pathlib,time; host=next(x.split()[1] for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('NSpid:')); "
                f"pathlib.Path({str(pid_file)!r}).write_text(host); time.sleep(60)\"\n"
                "subprocess.Popen([sys.executable,'-c',code],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)\n"
                f"f=pathlib.Path({str(pid_file)!r}); d=time.monotonic()+3\n"
                "while not f.exists() and time.monotonic() < d:\n    time.sleep(0.01)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            assignment = Assignment(
                WorkerSpec("test", Role.DEV, ".agents/agent-dev.md", str(executable)),
                Candidate(
                    repo="Example/project",
                    repo_path=root,
                    number=10,
                    title="work",
                    state="todo",
                    priority="P1",
                    role=Role.DEV,
                    state_entered_at=NOW,
                ),
            )
            result = WorkerRunner(
                RuntimePaths(root / "runtime"), process_start=lambda pid: f"boot:{pid}"
            ).run("run-background", assignment, timeout=10)
            self.assertEqual("completed", result.status)
            pid = int(pid_file.read_text(encoding="utf-8"))
            try:
                deadline = time.monotonic() + 5
                while process_is_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_running(pid))
            finally:
                if process_is_running(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_supervisor_parent_death_terminates_execed_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "profile.pid"
            project = Path(__file__).resolve().parents[1]
            target = (
                "import os,time,pathlib,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            parent = (
                "import os,subprocess,sys,time,pathlib; "
                "r,w=os.pipe(); "
                "p=subprocess.Popen([sys.executable,'-m','loop_harness.supervisor',str(r),'--',"
                f"sys.executable,'-c',{target!r}],pass_fds=(r,),start_new_session=True); "
                "os.close(r); os.write(w,b'GO'); os.close(w); "
                f"f=pathlib.Path({str(pid_file)!r}); d=time.monotonic()+5; "
                "\nwhile not f.exists() and time.monotonic() < d:\n    time.sleep(0.02)\n"
                "os._exit(0)"
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project)
            subprocess.run([sys.executable, "-c", parent], env=environment, check=True)
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text(encoding="utf-8"))
            try:
                deadline = time.monotonic() + 5
                while process_is_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_running(pid))
            finally:
                if process_is_running(pid):
                    os.killpg(pid, signal.SIGKILL)

    def test_terminate_all_stops_active_worker_process_group(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "profile"
            executable.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            worker = WorkerSpec("test", Role.DEV, ".agents/agent-dev.md", str(executable))
            assignment = Assignment(
                worker,
                Candidate(
                    repo="Example/project",
                    repo_path=root,
                    number=9,
                    title="work",
                    state="todo",
                    priority="P1",
                    role=Role.DEV,
                    state_entered_at=NOW,
                ),
            )
            runner = WorkerRunner(RuntimePaths(root / "runtime"), process_start=lambda pid: f"boot:{pid}")
            started = threading.Event()
            holder: list[WorkerResult] = []
            thread = threading.Thread(
                target=lambda: holder.append(
                    runner.run(
                        "run-active",
                        assignment,
                        timeout=60,
                        on_started=lambda *_: started.set(),
                    )
                )
            )
            thread.start()
            self.assertTrue(started.wait(5))
            runner.terminate_all()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual("failed", holder[0].status)
    def test_linux_process_identity_parses_comm_containing_spaces(self) -> None:
        stat = "123 (profile worker name) S " + " ".join(str(i) for i in range(4, 53))

        def read_text(path: Path, **kwargs) -> str:
            if str(path) == "/proc/123/stat":
                return stat
            if str(path) == "/proc/sys/kernel/random/boot_id":
                return "boot-id\n"
            raise AssertionError(path)

        with patch("loop_harness.worker.Path.read_text", autospec=True, side_effect=read_text):
            self.assertEqual("boot-id:22", linux_process_start(123))

    def test_task_prompt_is_bounded_structured_and_names_contract(self) -> None:
        item = assignments()[1]
        prompt = build_task_prompt("run-123", item, RuntimePaths(Path("/tmp/runtime")))
        self.assertLess(len(prompt), 4000)
        self.assertIn("run-123", prompt)
        self.assertIn("Example/project#2", prompt)
        self.assertIn(item.worker.contract, prompt)
        self.assertIn("only the supplied issue", prompt)

    def test_worker_persists_bounded_stdout_and_stderr_log_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "noisy-profile"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "sys.stdout.write('A' * 150000)\n"
                "sys.stderr.write('B' * 150000)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable)
            )
            assignment = Assignment(worker, candidate_at(root))

            result = WorkerRunner(paths).run("run-noisy", assignment, timeout=10)

            stdout_log = paths.logs / "run-noisy.stdout.log"
            stderr_log = paths.logs / "run-noisy.stderr.log"
            self.assertTrue(stdout_log.exists())
            self.assertTrue(stderr_log.exists())
            self.assertLessEqual(stdout_log.stat().st_size, 128_000)
            self.assertLessEqual(stderr_log.stat().st_size, 128_000)
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["stdout_path"], str(stdout_log))
            self.assertEqual(payload["stderr_path"], str(stderr_log))
            self.assertTrue(payload["truncated"])

    def test_worker_drops_partial_truncated_line_before_redaction(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "boundary-profile"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "sys.stdout.write('PASSWD=' + 'Z' * 140000 + '\\nSAFE-tail\\n')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable)
            )
            assignment = Assignment(worker, candidate_at(root))

            result = WorkerRunner(paths).run("run-boundary", assignment, timeout=10)

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            persisted = (paths.logs / "run-boundary.stdout.log").read_text(encoding="utf-8")
            self.assertNotIn("Z" * 100, persisted)
            self.assertNotIn("Z" * 100, payload["stdout"])
            self.assertIn("SAFE-tail", persisted)

    def test_worker_redacts_persisted_stdout_and_stderr_logs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "secret-profile"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "print(\"GH_TOKEN='stdout secret'\")\n"
                "print('\\\"password\\\": \\\"stderr secret\\\"', file=sys.stderr)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable)
            )

            WorkerRunner(paths).run(
                "run-secret", Assignment(worker, candidate_at(root)), timeout=10
            )

            for log_path in (
                paths.logs / "run-secret.stdout.log",
                paths.logs / "run-secret.stderr.log",
            ):
                content = log_path.read_text(encoding="utf-8")
                self.assertNotIn("stdout secret", content)
                self.assertNotIn("stderr secret", content)
                self.assertIn("[REDACTED]", content)
                self.assertEqual(0o600, log_path.stat().st_mode & 0o777)
                self.assertLessEqual(log_path.stat().st_size, 128_000)

    def test_worker_does_not_persist_raw_or_unbounded_logs_while_running(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "started"
            executable = root / "noisy-running-profile"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys, time\n"
                "sys.stdout.write(\"TOKEN='live secret' \" + 'X' * 200000)\n"
                "sys.stdout.flush()\n"
                f"pathlib.Path({str(marker)!r}).write_text('started')\n"
                "time.sleep(0.5)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable)
            )
            results: list[object] = []
            thread = threading.Thread(
                target=lambda: results.append(
                    WorkerRunner(paths).run(
                        "run-live-log", Assignment(worker, candidate_at(root)), timeout=5
                    )
                )
            )
            thread.start()
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            self.assertFalse((paths.logs / "run-live-log.stdout.log").exists())
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            content = (paths.logs / "run-live-log.stdout.log").read_text(encoding="utf-8")
            self.assertNotIn("live secret", content)
            self.assertLessEqual(len(content.encode()), 128_000)

    def test_spawn_exception_does_not_leak_gate_descriptors(self) -> None:
        with TemporaryDirectory() as directory:
            paths = RuntimePaths(Path(directory) / "runtime")

            def fail_spawn(*_args, **_kwargs):
                raise OSError("injected spawn failure")

            runner = WorkerRunner(paths, popen=fail_spawn)
            before = len(list(Path("/proc/self/fd").iterdir()))
            for index in range(10):
                result = runner.run(f"run-spawn-{index}", assignments()[1], timeout=1)
                self.assertEqual("launch-failed", result.status)
            after = len(list(Path("/proc/self/fd").iterdir()))
            self.assertLessEqual(after, before + 1)

    def test_worker_does_not_exec_profile_until_started_callback_succeeds(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "executed"
            executable = root / "gated-profile"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                f"pathlib.Path({str(marker)!r}).write_text('executed')\n"
                "sys.stdin.read()\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable)
            )
            observed: dict[str, object] = {}

            def on_started(pid: int, identity: str) -> None:
                time.sleep(0.1)
                observed.update(pid=pid, identity=identity)
                self.assertFalse(marker.exists())

            result = WorkerRunner(paths).run(
                "run-gated",
                Assignment(worker, candidate_at(root)),
                timeout=10,
                on_started=on_started,
            )

            self.assertEqual("completed", result.status)
            self.assertTrue(marker.exists())
            self.assertNotEqual("unknown", observed["identity"])

    def test_unknown_process_identity_fails_launch_without_execing_profile(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "executed"
            executable = root / "profile"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable)
            )

            result = WorkerRunner(paths, process_start=lambda _pid: "unknown").run(
                "run-no-identity",
                Assignment(worker, candidate_at(root)),
                timeout=10,
                on_started=lambda *_: self.fail("unknown identity must not be persisted as running"),
            )

            self.assertEqual("launch-failed", result.status)
            self.assertFalse(marker.exists())
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("launch-failed", payload["status"])

    def test_started_callback_failure_reaps_supervisor_and_persists_launch_failed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "executed"
            executable = root / "profile"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable)
            )
            observed: dict[str, int] = {}

            def fail_callback(pid: int, _identity: str) -> None:
                observed["pid"] = pid
                raise RuntimeError("injected persistence failure")

            result = WorkerRunner(paths).run(
                "run-callback-failure",
                Assignment(worker, candidate_at(root)),
                timeout=10,
                on_started=fail_callback,
            )

            self.assertEqual("launch-failed", result.status)
            self.assertFalse(marker.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(observed["pid"], 0)
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("launch-failed", payload["status"])
            self.assertIn("injected persistence failure", payload["stderr"])

    def test_worker_uses_profile_argv_stdin_and_new_process_session(self) -> None:
        recorded: dict[str, object] = {}

        class FakeProcess:
            pid = os.getpid()

            def __init__(self, gate_fd: int):
                self.gate_fd = os.dup(gate_fd)
                self.returncode = None
                self.stdout = io.BytesIO(b"done")
                self.stderr = io.BytesIO(b"")
                self.stdin = self

            def write(self, data: bytes):
                recorded["input"] = data.decode()
                return len(data)

            def close(self):
                return None

            def poll(self):
                return self.returncode

            def wait(self, timeout: float | None = None):
                recorded["gate"] = os.read(self.gate_fd, 2)
                os.close(self.gate_fd)
                recorded["timeout"] = timeout
                self.returncode = 0
                return 0

        def popen(argv, **kwargs):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs
            return FakeProcess(int(argv[3]))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths(root / "runtime")
            paths.ensure()
            runner = WorkerRunner(paths, popen=popen, process_start=lambda pid: "start")
            injected_worker = WorkerSpec(
                "mcgee", Role.DEV, ".agents/agent-dev.md", executable="not-on-path"
            )
            injected_assignment = Assignment(injected_worker, candidate_at(root))
            with patch.dict(
                os.environ,
                {"GH_TOKEN": "do-not-inherit", "TELEGRAM_BOT_TOKEN": "also-secret"},
            ):
                result = runner.run("run-123", injected_assignment, timeout=5)

        self.assertEqual("completed", result.status)
        self.assertEqual(["not-on-path", "chat"], recorded["argv"][5:7])
        self.assertEqual(b"GO", recorded["gate"])
        self.assertEqual(True, recorded["kwargs"]["start_new_session"])
        self.assertEqual(False, recorded["kwargs"].get("shell", False))
        self.assertEqual(root, recorded["kwargs"]["cwd"])
        self.assertNotIn("GH_TOKEN", recorded["kwargs"]["env"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", recorded["kwargs"]["env"])
        self.assertIn("run-123", recorded["input"])

    def test_spawn_failure_is_persisted_as_a_terminal_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root / "runtime")
            worker = WorkerSpec(
                "mcgee",
                Role.DEV,
                ".agents/agent-dev.md",
                executable=str(root / "missing-profile"),
            )
            assignment = Assignment(worker, candidate_at(root))

            result = WorkerRunner(paths).run("run-missing", assignment, timeout=5)

            self.assertEqual("launch-failed", result.status)
            self.assertIsNone(result.exit_code)
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("launch-failed", payload["status"])
            self.assertIn("No such file", payload["stderr"])


if __name__ == "__main__":
    unittest.main()
