from __future__ import annotations

import json
import io
import errno
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from loop_harness.containment import CgroupScope, _current_cgroup
from loop_harness.github import evidence_url_matches, required_evidence_kinds
from loop_harness.group_supervisor import _cleanup_cgroup
from loop_harness.models import Candidate, Role, WorkspaceContext
from loop_harness.runtime import FileLock, HeavyRunner, ResultStore, RuntimePaths
from loop_harness.scheduler import Assignment, DEFAULT_WORKERS, WorkerSpec, run_concurrently
from loop_harness.store import RunStore
from loop_harness.worker import (
    WorkerResult, WorkerRunner, _profile_home_for_worker, build_task_prompt,
    linux_process_start,
)


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
    def test_atomic_result_write_fsyncs_containing_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "results"
            original_fsync = os.fsync
            synced_directory = False

            def observe_fsync(fd: int) -> None:
                nonlocal synced_directory
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    synced_directory = True
                original_fsync(fd)

            with patch("loop_harness.runtime.os.fsync", side_effect=observe_fsync):
                ResultStore(directory).write("run-durable", {"status": "failed"})
            self.assertTrue(synced_directory)

    def test_cgroup_enodev_is_idempotent_offline_cleanup(self) -> None:
        scope = CgroupScope(Path("/offline/scope"), Path("/offline"))
        with patch("loop_harness.containment.Path.exists", return_value=True), patch(
            "loop_harness.containment.Path.write_text",
            side_effect=OSError(errno.ENODEV, "offline"),
        ):
            scope.kill_all()
        with patch("loop_harness.containment.Path.exists", return_value=True), patch(
            "loop_harness.containment.CgroupScope._directories_bottom_up",
            side_effect=OSError(errno.ENODEV, "offline"),
        ):
            scope.remove(timeout=0)

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
            result = HeavyRunner(root / "runtime" / "heavy.lock").run(
                [sys.executable, "-c", command], cwd=root, timeout=10
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("blocked", outcome.read_text(encoding="ascii"))

    def test_contained_worker_can_write_only_its_assigned_run_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assigned = root / "runs" / "one" / "worktree"
            private_git = root / "runs" / "one" / "git"
            canonical = root / "canonical"
            sibling = root / "runs" / "two" / "worktree"
            runtime = root / "runtime"
            sibling_runtime = root / "runs" / "two" / "runtime"
            for path in (assigned, private_git, canonical, sibling):
                path.mkdir(parents=True)
            RuntimePaths(runtime).ensure()
            sibling_runtime.mkdir()
            (runtime / "runs.sqlite3").write_text("global", encoding="utf-8")
            (runtime / "results" / "existing").write_text("global", encoding="utf-8")
            (runtime / "logs" / "existing").write_text("global", encoding="utf-8")
            shm_target = Path("/dev/shm") / f"loop-harness-{os.getpid()}-{time.time_ns()}"
            executable = root / "probe"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os,pathlib\n"
                f"targets={{'assigned': pathlib.Path({str(assigned / 'write')!r}), "
                f"'git': pathlib.Path({str(private_git / 'write')!r}), "
                f"'canonical': pathlib.Path({str(canonical / 'write')!r}), "
                f"'sibling': pathlib.Path({str(sibling / 'write')!r}), "
                f"'global_db': pathlib.Path({str(runtime / 'runs.sqlite3')!r}), "
                f"'global_result': pathlib.Path({str(runtime / 'results' / 'existing')!r}), "
                f"'global_log': pathlib.Path({str(runtime / 'logs' / 'existing')!r}), "
                f"'sibling_runtime': pathlib.Path({str(sibling_runtime / 'write')!r}), "
                f"'dev_shm': pathlib.Path({str(shm_target)!r})}}\n"
                "out=[]\n"
                "for name,path in targets.items():\n"
                "    try:\n"
                "        path.write_text(name)\n"
                "        out.append(name + '=writable')\n"
                "    except OSError:\n"
                "        out.append(name + '=blocked')\n"
                "lock=pathlib.Path(os.environ['LOOP_HARNESS_RUNTIME'])/'heavy.lock'\n"
                "try:\n"
                "    lock.unlink(); lock.write_text('replacement')\n"
                "    out.append('heavy_replace=writable')\n"
                "except OSError:\n"
                "    out.append('heavy_replace=blocked')\n"
                "out.append('runtime=' + os.environ['LOOP_HARNESS_RUNTIME'])\n"
                "print('\\n'.join(out))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            workspace = WorkspaceContext(
                path=assigned,
                source_sha="a" * 40,
                source_kind="default",
                local_branch="loop-harness/run-one",
                remote_branch="loop/12-run-one",
                pr_number=None,
                private_ref="refs/loop-harness/run-one/source",
                run_root=assigned.parent,
                git_dir=private_git,
                expected_origin_url="https://github.com/Example/project.git",
            )
            worker = WorkerSpec("mcgee", Role.DEV, ".agents/agent-dev.md", executable=str(executable))
            result = WorkerRunner(RuntimePaths(runtime)).run(
                "run-landlock", Assignment(worker, candidate_at(canonical), workspace), timeout=10
            )
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))

            self.assertEqual("completed", result.status, payload["stderr"])
            self.assertIn("assigned=writable", payload["stdout"])
            self.assertIn("git=writable", payload["stdout"])
            self.assertIn("heavy_replace=blocked", payload["stdout"])
            self.assertIn("canonical=blocked", payload["stdout"])
            self.assertIn("sibling=blocked", payload["stdout"])
            for name in (
                "global_db", "global_result", "global_log", "sibling_runtime", "dev_shm"
            ):
                self.assertIn(f"{name}=blocked", payload["stdout"])
            private_runtime = assigned.parent / "runtime"
            self.assertIn(f"runtime={private_runtime}", payload["stdout"])
            self.assertEqual(
                RuntimePaths(runtime).heavy_lock.stat().st_ino,
                (private_runtime / "heavy.lock").stat().st_ino,
            )
            self.assertFalse((canonical / "write").exists())
            self.assertFalse((sibling / "write").exists())
            self.assertFalse(shm_target.exists())

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
            result = HeavyRunner(root / "runtime" / "heavy.lock").run(
                [sys.executable, "-c", command], cwd=root, timeout=10
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
            runtime = root / "runtime"
            workspace = root / "workspace"
            workspace.mkdir()
            child_pid_file = workspace / "heavy.pid"
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
                f"e['LOOP_HARNESS_RUNTIME']={str(runtime)!r}; "
                f"p=subprocess.Popen([{str(wrapper)!r},sys.executable,'-c',{target!r}],"
                "env=e,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); "
                f"pathlib.Path({str(wrapper_pid_file)!r}).write_text(str(p.pid)); "
                f"f=pathlib.Path({str(child_pid_file)!r}); d=time.monotonic()+5; "
                "\nwhile not f.exists() and time.monotonic() < d:\n    time.sleep(0.02)\n"
                "os.kill(p.pid,signal.SIGKILL); "
                "os._exit(0)"
            )
            subprocess.run([sys.executable, "-c", parent], cwd=workspace, check=True)
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
            runtime = root / "runtime"
            workspace = root / "workspace"
            workspace.mkdir()
            pid_file = workspace / "heavy.pid"
            wrapper = Path(__file__).resolve().parents[1] / "scripts" / "loop-engineering-heavy"
            environment = os.environ.copy()
            environment["LOOP_HARNESS_RUNTIME"] = str(runtime)
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
                cwd=workspace,
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

    def test_concurrent_execution_fails_loudly_on_an_assignment_exception(self) -> None:
        def execute(item: int) -> int:
            if item == 1:
                raise RuntimeError("one assignment failed")
            return item

        with self.assertRaisesRegex(RuntimeError, "one assignment failed"):
            run_concurrently([1, 2], execute)

    def test_concurrent_execution_does_not_swallow_base_exceptions(self) -> None:
        def interrupted(_item: int) -> int:
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_concurrently([1], interrupted)

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

    def test_heavy_child_cannot_replace_lease_or_mutate_global_runtime(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            runtime.ensure()
            workspace = root / "workspace"
            workspace.mkdir()
            runtime.database.write_text("database", encoding="ascii")
            existing_result = runtime.results / "existing.json"
            existing_result.write_text("result", encoding="ascii")
            probe = workspace / "probe.json"
            code = (
                "import json,os,pathlib; "
                f"lock=pathlib.Path({str(runtime.heavy_lock)!r}); "
                f"db=pathlib.Path({str(runtime.database)!r}); "
                f"result=pathlib.Path({str(existing_result)!r}); "
                "out={}; "
                "\nfor name,action in ((\"unlink_lock\",lambda:lock.unlink()),"
                "(\"replace_lock\",lambda:os.replace(pathlib.Path('replacement'),lock)),"
                "(\"database\",lambda:db.write_text('changed')),"
                "(\"result\",lambda:result.write_text('changed'))):\n"
                " pathlib.Path('replacement').write_text('replacement')\n"
                " try: action(); out[name]='writable'\n"
                " except OSError: out[name]='blocked'\n"
                f"pathlib.Path({str(probe)!r}).write_text(json.dumps(out))"
            )

            completed = HeavyRunner(runtime.heavy_lock).run(
                [sys.executable, "-c", code], cwd=workspace, timeout=10
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                {
                    "unlink_lock": "blocked",
                    "replace_lock": "blocked",
                    "database": "blocked",
                    "result": "blocked",
                },
                json.loads(probe.read_text(encoding="utf-8")),
            )
            self.assertEqual("database", runtime.database.read_text(encoding="ascii"))
            self.assertEqual("result", existing_result.read_text(encoding="ascii"))

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
    def test_named_profile_write_scope_is_exact_and_rejects_traversal(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = home / ".hermes" / "profiles" / "jimmy"
            sibling = home / ".hermes" / "profiles" / "ducky"
            profile.mkdir(parents=True)
            sibling.mkdir()
            with patch.object(Path, "home", return_value=home):
                self.assertEqual(profile.resolve(), _profile_home_for_worker("jimmy"))
                self.assertIsNone(_profile_home_for_worker("mcgee"))
                with self.assertRaisesRegex(RuntimeError, "profile name is invalid"):
                    _profile_home_for_worker("../ducky")

    def test_prompt_examples_are_accepted_by_the_handoff_policy(self) -> None:
        for item in assignments():
            workspace = WorkspaceContext(
                path=Path("/tmp/worktree"), source_sha="a" * 40,
                source_kind="pr", local_branch="loop-harness/run-123",
                remote_branch="feature/2", pr_number=22,
                private_ref="refs/loop-harness/run-123/source",
                run_root=Path("/tmp/run-123"), git_dir=Path("/tmp/run-123/git"),
                expected_origin_url="https://github.com/Example/project.git",
                repo_slug="Example/project",
            )
            assignment = replace(item, workspace=workspace)
            prompt = build_task_prompt("run-123", assignment, RuntimePaths(Path("/tmp/runtime")))
            block = prompt.split("```loop-engineering-handoff\n", 1)[1].split("\n```", 1)[0]
            payload = json.loads(block)
            state = payload["state"]
            required = required_evidence_kinds(item.worker.role, state)
            self.assertTrue(required.issubset({entry["kind"] for entry in payload["evidence"]}))
            for entry in payload["evidence"]:
                matches = evidence_url_matches(
                    item.worker.role, entry["kind"], entry["url"],
                    repo=item.candidate.repo, issue=item.candidate.number,
                    pr_number=payload["pr_number"], head_sha=payload["head_sha"],
                )
                if entry["kind"] == "screenshot":
                    self.assertFalse(matches, entry)
                    self.assertIn("00000000-0000-0000-0000-000000000000", entry["url"])
                else:
                    self.assertTrue(matches, entry)

    def test_dev_prompt_need_confirmation_policy_requires_blocker_not_verification(self) -> None:
        self.assertEqual({"blocker"}, required_evidence_kinds(Role.DEV, "need confirmation"))
        prompt = build_task_prompt(
            "run-123", assignments()[1], RuntimePaths(Path("/tmp/runtime"))
        )
        self.assertIn("need confirmation requires kind blocker", prompt)

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
            paths = RuntimePaths(root / "runtime")
            paths.ensure()
            lease = FileLock(paths.heavy_lock)
            lease.acquire()
            results: list[WorkerResult] = []
            thread = threading.Thread(
                target=lambda: results.append(
                    WorkerRunner(paths, process_start=lambda pid: f"boot:{pid}").run(
                        "run-heavy", assignment, timeout=15
                    )
                )
            )
            thread.start()
            time.sleep(0.25)
            self.assertTrue(thread.is_alive())
            self.assertFalse(marker.exists())
            lease.release()
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
            result = results[0]
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

    def test_sigkill_dispatcher_reaps_tracer_supervisor_worker_and_reclaims_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(__file__).resolve().parents[1]
            pids_file = root / "pids"
            guardian_file = root / "guardian"
            target = (
                "import os,time,pathlib; "
                f"pathlib.Path({str(pids_file)!r}).write_text(f'{{os.getppid()}} {{os.getpid()}}'); "
                "time.sleep(60)"
            )
            parent = (
                "import os,subprocess,sys,time,pathlib; r,w=os.pipe(); "
                "p=subprocess.Popen([sys.executable,'-m','loop_harness.trace_guardian','--',"
                "'strace','--follow-forks','--output',"
                f"{str(root / 'trace')!r},'--',sys.executable,'-m','loop_harness.supervisor',"
                f"str(r),'--',sys.executable,'-c',{target!r}],pass_fds=(r,),start_new_session=True); "
                "os.close(r); os.write(w,b'GO'); os.close(w); "
                f"pathlib.Path({str(guardian_file)!r}).write_text(str(p.pid)); "
                f"f=pathlib.Path({str(pids_file)!r}); d=time.monotonic()+5; "
                "\nwhile not f.exists() and time.monotonic()<d:\n time.sleep(.02)\n"
                "time.sleep(60)"
            )
            environment = {**os.environ, "PYTHONPATH": str(project)}
            dispatcher = subprocess.Popen([sys.executable, "-c", parent], env=environment)
            tracked: list[int] = []
            try:
                deadline = time.monotonic() + 8
                while (
                    (not guardian_file.exists() or not pids_file.exists())
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertTrue(guardian_file.exists())
                self.assertTrue(pids_file.exists())
                guardian = int(guardian_file.read_text())
                supervisor, worker = map(int, pids_file.read_text().split())
                tracked = [guardian, supervisor, worker]
                store = RunStore(root / "runs.sqlite3")
                item = Assignment(DEFAULT_WORKERS[0], candidate_at(root))
                self.assertTrue(store.reserve(item, "run-parent-death", NOW))
                store.mark_running(
                    "run-parent-death", pid=guardian,
                    process_start=linux_process_start(guardian), at=NOW,
                )

                os.kill(dispatcher.pid, signal.SIGKILL)
                dispatcher.wait(timeout=5)
                deadline = time.monotonic() + 8
                while any(process_is_running(pid) for pid in tracked) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse([pid for pid in tracked if process_is_running(pid)])
                self.assertEqual(
                    ["run-parent-death"],
                    store.reclaim_stale(
                        NOW + timedelta(minutes=45),
                        lambda pid, identity: (
                            process_is_running(pid) and linux_process_start(pid) == identity
                        ),
                    ),
                )
            finally:
                if dispatcher.poll() is None:
                    dispatcher.kill()
                    dispatcher.wait(timeout=5)
                for pid in tracked:
                    if process_is_running(pid):
                        os.kill(pid, signal.SIGKILL)

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
        self.assertIn("```loop-engineering-handoff", prompt)
        self.assertIn('"schema_version": 1', prompt)
        self.assertIn('"run_id": "run-123"', prompt)
        self.assertIn('"role": "dev"', prompt)
        self.assertIn('"state": "need confirmation"', prompt)
        self.assertIn('"issue": 2', prompt)
        self.assertIn('"pr_number": null', prompt)
        self.assertIn('"head_sha": null', prompt)
        self.assertIn('"evidence": [', prompt)
        self.assertNotIn("Loop Engineering run run-123: <terminal-state>", prompt)

    def test_handoff_contracts_publish_exact_schema_not_legacy_marker(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contracts = {
            "README.md": None,
            ".agents/WORKFLOW.md": None,
            ".agents/agent-pm.md": "pm",
            ".agents/agent-dev.md": "dev",
            ".agents/agent-dev-torres.md": "dev",
            ".agents/agent-dev-kate.md": "dev",
            ".agents/agent-qa.md": "qa",
            ".agents/agent-qa-ducky.md": "qa",
        }
        required = (
            "```loop-engineering-handoff",
            '"schema_version": 1',
            '"run_id":',
            '"role":',
            '"state":',
            '"issue":',
            '"pr_number":',
            '"head_sha":',
            '"evidence":',
            '"kind":',
            '"summary":',
            '"url":',
        )
        for relative, role in contracts.items():
            with self.subTest(contract=relative):
                content = (root / relative).read_text(encoding="utf-8")
                for fragment in required:
                    self.assertIn(fragment, content)
                if role is not None:
                    self.assertIn(f'"role": "{role}"', content)
                self.assertNotIn("Loop Engineering run <run_id>:", content)

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
            workspace = WorkspaceContext(
                path=root,
                source_sha="a" * 40,
                source_kind="default",
                local_branch="loop-harness/run-123",
                remote_branch="loop/2-run-123",
                pr_number=None,
                private_ref="refs/loop-harness/run-123/source",
                run_root=root,
                git_dir=root / ".git",
                expected_origin_url="https://github.com/Example/project.git",
            )
            injected_assignment = Assignment(
                injected_worker,
                candidate_at(root / "canonical-must-not-be-used"),
                workspace,
            )
            with patch.dict(
                os.environ,
                {"GH_TOKEN": "do-not-inherit", "TELEGRAM_BOT_TOKEN": "also-secret"},
            ):
                result = runner.run("run-123", injected_assignment, timeout=5)

        self.assertEqual("completed", result.status)
        supervisor_argv = cast(list[str], recorded["argv"])
        separator = supervisor_argv.index("--")
        self.assertEqual(
            ["not-on-path", "chat"], supervisor_argv[separator + 1 : separator + 3]
        )
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
