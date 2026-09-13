from __future__ import annotations

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
            repo = root / "repo"
            repo.mkdir()
            source_agents = Path(__file__).resolve().parents[1] / ".agents"
            target_agents = repo / ".agents"
            target_agents.mkdir()
            for name in (
                "WORKFLOW.md",
                "agent-dev.md",
                "agent-dev-torres.md",
                "agent-dev-kate.md",
            ):
                shutil.copyfile(source_agents / name, target_agents / name)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/Example/project.git"],
                check=True,
            )
            registry = root / "registry.json"
            registry.write_text(
                json.dumps({"schema_version": 1, "repositories": [{"slug": "Example/project", "path": str(repo), "enabled": True}]}),
                encoding="utf-8",
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/usr/bin/env python3\nimport json\nprint(json.dumps([{'number':1,'title':'work','labels':[{'name':'todo'},{'name':'P1'}],'updatedAt':'2026-09-13T00:00:00Z'}]))\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            worker_pid = root / "worker.pid"
            profile = bin_dir / "mcgee"
            profile.write_text(
                "#!/usr/bin/env python3\nimport time,pathlib\n"
                "host=next(x.split()[1] for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('NSpid:'))\n"
                f"pathlib.Path({str(worker_pid)!r}).write_text(host)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            profile.chmod(0o755)
            runtime = root / "runtime"
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            process = subprocess.Popen(
                [sys.executable, "-m", "loop_harness", "--registry", str(registry), "--runtime", str(runtime), "tick", "--worker-timeout", "60"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 8
                while not worker_pid.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(worker_pid.exists())
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
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

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
            repo.mkdir()
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
            gh.write_text(
                """#!/usr/bin/env python3
import json
rows = [
    {'number': 1, 'title': 'plan', 'labels': [{'name': 'to be planned'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 2, 'title': 'dev 1', 'labels': [{'name': 'todo'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 3, 'title': 'dev 2', 'labels': [{'name': 'feedback'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 4, 'title': 'dev 3', 'labels': [{'name': 'in progress'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 5, 'title': 'qa 1', 'labels': [{'name': 'qa ready'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
    {'number': 6, 'title': 'qa 2', 'labels': [{'name': 'qa in progress'}, {'name': 'P1'}], 'updatedAt': '2026-09-11T00:00:00Z'},
]
print(json.dumps(rows))
"""
            )
            gh.chmod(0o755)
            for name in ("gibbs", "mcgee", "torres", "kate", "jimmy", "ducky", "claude"):
                executable = bin_dir / name
                executable.write_text("#!/bin/sh\nexit 99\n")
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
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
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
            self.assertFalse(runtime.exists())

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
