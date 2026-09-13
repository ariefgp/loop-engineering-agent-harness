from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from loop_harness.app import TickResult
from loop_harness.cli import main
from loop_harness.worker import WorkerResult


class CliTests(unittest.TestCase):
    def test_tick_returns_nonzero_when_an_assignment_result_is_dropped(self) -> None:
        assignment = MagicMock()
        assignment.worker.profile = "mcgee"
        assignment.worker.role.value = "dev"
        assignment.candidate.repo = "Example/project"
        assignment.candidate.number = 12
        assignment.candidate.state = "todo"
        assignment.candidate.priority = "P1"
        with patch("loop_harness.cli.load_registry") as registry, patch(
            "loop_harness.cli.GitHubClient"
        ), patch("loop_harness.cli.WorkerRunner"), patch(
            "loop_harness.cli.Harness"
        ) as harness, redirect_stdout(StringIO()):
            registry.return_value.enabled = ()
            harness.return_value.tick.return_value = TickResult("live", [assignment], [], [])
            exit_code = main(["--runtime", "/tmp/runtime", "tick"])
        self.assertEqual(1, exit_code)

    def test_tick_returns_nonzero_when_any_worker_result_failed(self) -> None:
        runner = MagicMock()
        failed = WorkerResult("run-failed", "failed", 1, Path("/tmp/result.json"))
        with patch("loop_harness.cli.load_registry") as registry, patch(
            "loop_harness.cli.GitHubClient"
        ), patch("loop_harness.cli.WorkerRunner", return_value=runner), patch(
            "loop_harness.cli.Harness"
        ) as harness, redirect_stdout(StringIO()):
            registry.return_value.enabled = ()
            harness.return_value.tick.return_value = TickResult("live", [], [failed], [])
            exit_code = main(["--runtime", "/tmp/runtime", "tick"])
        self.assertEqual(1, exit_code)

    def test_tick_sigterm_cleans_worker_and_records_terminal_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = root / "origin.git"
            seed = root / "seed"
            repo = root / "repo"
            subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
            subprocess.run(
                ["git", "-C", str(seed), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "config", "user.name", "Test"], check=True
            )
            source_agents = Path(__file__).resolve().parents[1] / ".agents"
            target_agents = seed / ".agents"
            target_agents.mkdir()
            for source in source_agents.glob("*.md"):
                shutil.copyfile(source, target_agents / source.name)
            (seed / "shared.txt").write_text("shared checkout sentinel\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(seed), "commit", "-q", "-m", "source"], check=True
            )
            source_sha = subprocess.run(
                ["git", "-C", str(seed), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True
            )
            subprocess.run(
                ["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
                check=True,
            )
            subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/Example/project.git",
                ],
                check=True,
            )
            registry = root / "registry.json"
            registry.write_text(
                json.dumps({"schema_version": 1, "repositories": [{"slug": "Example/project", "path": str(repo), "enabled": True}]}),
                encoding="utf-8",
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            git = bin_dir / "git"
            git.write_text(
                """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if 'fetch' in args:
    fetch = args.index('fetch')
    for index in range(fetch + 1, len(args)):
        if args[index] in ('origin', 'https://github.com/Example/project.git'):
            args[index] = __BARE__
            break
os.execv(__GIT__, [__GIT__, *args])
""".replace("__BARE__", repr(str(bare))).replace("__GIT__", repr(real_git)),
                encoding="utf-8",
            )
            git.chmod(0o755)
            gh = bin_dir / "gh"
            gh.write_text(
                """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ['repo', 'clone']:
    os.execv(__GIT__, [__GIT__, 'clone', __BARE__, args[3], *args[5:]])
elif args[:2] == ['issue', 'list']:
    payload = [{'number': 1, 'title': 'work', 'labels': [{'name': 'todo'}, {'name': 'P1'}],
                'updatedAt': '2026-09-13T00:00:00Z'}]
elif args[:4] == ['api', '--hostname', 'github.com', 'repos/Example/project']:
    payload = {'default_branch': 'main'}
elif args[:4] == ['api', '--hostname', 'github.com', 'repos/Example/project/git/ref/heads/main']:
    payload = {'object': {'sha': __SHA__}}
else:
    print(f'unexpected gh arguments: {args!r}', file=sys.stderr)
    sys.exit(2)
print(json.dumps(payload))
""".replace("__SHA__", repr(source_sha)).replace(
                    "__BARE__", repr(str(bare))
                ).replace("__GIT__", repr(real_git)),
                encoding="utf-8",
            )
            gh.chmod(0o755)
            runtime = root / "runtime"
            temp_home = root / "home"
            temp_home.mkdir()
            workspace_root = temp_home / ".local" / "share" / "loop-engineering" / "worktrees"
            profile = bin_dir / "mcgee"
            profile.write_text(
                "#!/usr/bin/env python3\nimport os,time,pathlib\n"
                "runtime=pathlib.Path(os.environ['LOOP_HARNESS_RUNTIME'])/'tmp'\n"
                "host=next(x.split()[1] for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('NSpid:'))\n"
                "(runtime/'worker.pid').write_text(host)\n"
                "(runtime/'worker.cwd').write_text(str(pathlib.Path.cwd()))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            profile.chmod(0o755)
            shared_head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            shared_status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v1"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["HOME"] = str(temp_home)
            process = subprocess.Popen(
                [sys.executable, "-m", "loop_harness", "--registry", str(registry), "--runtime", str(runtime), "tick", "--worker-timeout", "60"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 8
                worker_pid: Path | None = None
                while worker_pid is None and time.monotonic() < deadline:
                    worker_pid = next(
                        workspace_root.glob("*/*/runtime/tmp/worker.pid"), None
                    )
                    time.sleep(0.02)
                if worker_pid is None:
                    returncode = process.poll()
                    output, error = process.communicate(timeout=1) if returncode is not None else ("", "")
                    result_details = []
                    for result_file in (runtime / "results").glob("*.json"):
                        result_details.append(result_file.read_text(encoding="utf-8"))
                    self.fail(
                        f"worker did not start (dispatcher={returncode}): {output} {error} "
                        f"results={result_details}"
                    )
                assert worker_pid is not None
                worker_cwd = worker_pid.with_name("worker.cwd")
                self.assertTrue(worker_cwd.exists())
                self.assertFalse((runtime / "worker.pid").exists())
                self.assertFalse((runtime / "worker.cwd").exists())
                run_root = worker_pid.parent.parent.parent
                workspace = Path(worker_cwd.read_text(encoding="utf-8"))
                self.assertNotEqual(repo, workspace)
                self.assertTrue(workspace.is_dir())
                self.assertEqual(
                    "shared checkout sentinel\n",
                    (workspace / "shared.txt").read_text(encoding="utf-8"),
                )
                pid = int(worker_pid.read_text(encoding="utf-8"))
                for _ in range(8):
                    try:
                        process.send_signal(signal.SIGTERM)
                    except ProcessLookupError:
                        break
                process.wait(timeout=10)
                self.assertEqual(128 + signal.SIGTERM, process.returncode)
                deadline = time.monotonic() + 5
                while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(Path(f"/proc/{pid}").exists())
                with sqlite3.connect(runtime / "runs.sqlite3") as connection:
                    status, outcome = connection.execute(
                        "SELECT state, outcome FROM runs"
                    ).fetchone()
                self.assertEqual("finished", status)
                self.assertIsNotNone(outcome)
                self.assertFalse(workspace.exists())
                self.assertFalse(run_root.exists())
                self.assertEqual(
                    shared_head,
                    subprocess.run(
                        ["git", "-C", str(repo), "rev-parse", "HEAD"],
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip(),
                )
                self.assertEqual(
                    shared_status,
                    subprocess.run(
                        ["git", "-C", str(repo), "status", "--porcelain=v1"],
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout,
                )
                self.assertEqual(
                    1,
                    len(
                        subprocess.run(
                            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                            text=True,
                            capture_output=True,
                            check=True,
                        ).stdout.split("worktree ")
                    )
                    - 1,
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_heavy_wrapper_works_from_outside_harness_with_supplied_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = Path(__file__).resolve().parents[1] / "scripts" / "loop-engineering-heavy"
            completed = subprocess.run(
                [wrapper, sys.executable, "-c", "import os; print(os.getcwd())"],
                cwd=root,
                env={**os.environ, "LOOP_HARNESS_RUNTIME": str(root / "runtime")},
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(f"{root}\n", completed.stdout)
            self.assertTrue((root / "runtime" / "heavy.lock").exists())

    def test_dry_run_cli_outputs_plan_without_runtime_or_profile_launch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            source_agents = Path(__file__).resolve().parents[1] / ".agents"
            target_agents = repo / ".agents"
            target_agents.mkdir()
            for name in (
                "WORKFLOW.md",
                "agent-pm.md",
                "agent-dev.md",
                "agent-dev-torres.md",
                "agent-dev-kate.md",
                "agent-qa.md",
                "agent-qa-ducky.md",
            ):
                shutil.copyfile(source_agents / name, target_agents / name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            contracts = {
                f".agents/{name}": base64.b64encode(
                    (source_agents / name).read_bytes()
                ).decode("ascii")
                for name in (
                    "WORKFLOW.md",
                    "agent-pm.md",
                    "agent-dev.md",
                    "agent-dev-torres.md",
                    "agent-dev-kate.md",
                    "agent-qa.md",
                    "agent-qa-ducky.md",
                )
            }
            gh.write_text(
                """#!/usr/bin/env python3
import json, sys
rows = [
    {'number': 1, 'title': 'plan', 'labels': [{'name': 'to be planned'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 2, 'title': 'dev 1', 'labels': [{'name': 'todo'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 3, 'title': 'dev 2', 'labels': [{'name': 'feedback'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 4, 'title': 'dev 3', 'labels': [{'name': 'in progress'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 5, 'title': 'qa 1', 'labels': [{'name': 'qa ready'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 6, 'title': 'qa 2', 'labels': [{'name': 'qa in progress'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
]
args = sys.argv[1:]
sha = 'a' * 40
if args[:2] == ['issue', 'list']:
    payload = rows
elif args[:4] == ['api', '--hostname', 'github.com', 'repos/Example/project']:
    payload = {'default_branch': 'main'}
elif args[:4] == ['api', '--hostname', 'github.com', 'repos/Example/project/git/ref/heads/main']:
    payload = {'object': {'sha': sha}}
elif args[:4] == ['api', '--hostname', 'github.com', 'graphql']:
    number = int(next(value.split('=', 1)[1] for value in args if value.startswith('number=')))
    payload = {'data': {'repository': {'issue': {'closedByPullRequestsReferences': {
        'nodes': [{'number': 100 + number, 'state': 'OPEN', 'headRefName': f'issue-{number}',
                   'headRefOid': sha, 'headRepository': {'nameWithOwner': 'Example/project'}}],
        'pageInfo': {'hasNextPage': False}
    }}}}}
elif args[:3] == ['api', '--hostname', 'github.com'] and len(args) > 3 and '/contents/' in args[3]:
    path = args[3].split('/contents/', 1)[1]
    payload = {'encoding': 'base64', 'content': __CONTRACTS__[path]}
else:
    print(f'unexpected gh arguments: {args!r}', file=sys.stderr)
    sys.exit(2)
print(json.dumps(payload))
""".replace("__CONTRACTS__", repr(contracts))
            )
            gh.chmod(0o755)
            launch_marker = root / "profile-launched"
            for name in ("gibbs", "mcgee", "torres", "kate", "jimmy", "ducky", "claude"):
                executable = bin_dir / name
                executable.write_text(
                    f"#!/bin/sh\nprintf '%s\\n' {name!r} >> {str(launch_marker)!r}\nexit 99\n"
                )
                executable.chmod(0o755)
            registry = root / "repositories.json"
            registry.write_text(
                json.dumps(
                    {
                        "repositories": [
                            {
                                "slug": "Example/project",
                                "path": str(repo),
                                "enabled": True,
                                "roles": ["pm", "dev", "qa"],
                            }
                        ]
                    }
                )
            )
            runtime = root / "runtime"
            refs_before = subprocess.run(
                ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            worktrees_before = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            home = root / "home"
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "loop_harness",
                    "--registry",
                    str(registry),
                    "--runtime",
                    str(runtime),
                    "tick",
                    "--dry-run",
                ],
                text=True,
                capture_output=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("dry-run", payload["mode"])
            self.assertEqual(6, len(payload["assignments"]))
            self.assertEqual([], payload["failures"])
            self.assertFalse(runtime.exists())
            self.assertFalse(home.exists())
            self.assertFalse(launch_marker.exists())
            refs_after = subprocess.run(
                ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            worktrees_after = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(refs_before, refs_after)
            self.assertEqual(worktrees_before, worktrees_after)

    def test_cli_redacts_github_stderr_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            source_agents = Path(__file__).resolve().parents[1] / ".agents"
            target_agents = repo / ".agents"
            target_agents.mkdir()
            for source in source_agents.glob("*.md"):
                shutil.copyfile(source, target_agents / source.name)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/Example/project.git"],
                check=True,
            )
            registry = root / "registry.json"
            registry.write_text(
                json.dumps({"repositories": [{"slug": "Example/project", "path": str(repo), "enabled": True, "roles": ["dev"]}]}),
                encoding="utf-8",
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            secret = "github_pat_cli-secret-value"
            gh = bin_dir / "gh"
            gh.write_text(
                f"#!/bin/sh\nprintf '%s\\n' 'Authorization: Bearer {secret}' >&2\nexit 1\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            completed = subprocess.run(
                [sys.executable, "-m", "loop_harness", "--registry", str(registry), "--runtime", str(root / "runtime"), "tick", "--dry-run"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(1, completed.returncode)
            self.assertNotIn(secret, completed.stderr)
            self.assertIn("[REDACTED]", completed.stderr)

    def test_heavy_cli_runs_argument_vector_after_separator(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "loop_harness",
                    "--runtime",
                    str(runtime),
                    "heavy",
                    "--timeout",
                    "2",
                    "--",
                    sys.executable,
                    "-c",
                    "print('heavy-ok')",
                ],
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("heavy-ok\n", completed.stdout)

    def test_heavy_cli_timeout_is_clean_and_returns_124(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "loop_harness",
                    "--runtime",
                    str(root / "runtime"),
                    "heavy",
                    "--timeout",
                    "0.05",
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(10)",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(124, completed.returncode)
            self.assertIn("timed out", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
