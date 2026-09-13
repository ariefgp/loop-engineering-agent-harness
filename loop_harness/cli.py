from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from .app import Harness, TickResult
from .config import ConfigError, load_registry
from .github import GitHubClient, GitHubError
from .runtime import HeavyRunner, RuntimePaths, arm_parent_death_signal, redact_text
from .scheduler import DEFAULT_WORKERS, Assignment
from .worker import WorkerRunner


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_REGISTRY = _PROJECT_ROOT / "config" / "repositories.json"


class _TerminationRequested(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"termination signal {signum}")
        self.signum = signum


def _default_runtime() -> Path:
    run_user = Path(f"/run/user/{os.getuid()}")
    if run_user.is_dir():
        return run_user / "loop-engineering"
    return Path.home() / ".hermes" / "loop-engineering"


def _assignment_json(item: Assignment) -> dict[str, object]:
    return {
        "profile": item.worker.profile,
        "role": item.worker.role.value,
        "repository": item.candidate.repo,
        "issue": item.candidate.number,
        "state": item.candidate.state,
        "priority": item.candidate.priority,
    }


def _tick_json(result: TickResult) -> dict[str, object]:
    return {
        "mode": result.mode,
        "assignments": [_assignment_json(item) for item in result.assignments],
        "results": [
            {
                "run_id": item.run_id,
                "status": item.status,
                "exit_code": item.exit_code,
                "result_path": str(item.result_path),
            }
            for item in result.results
        ],
        "reclaimed": result.reclaimed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loop-harness")
    parser.add_argument("--registry", type=Path, default=_DEFAULT_REGISTRY)
    parser.add_argument("--runtime", type=Path, default=_default_runtime())
    subcommands = parser.add_subparsers(dest="command", required=True)

    tick = subcommands.add_parser("tick", help="scan, reserve, and run one fleet tick")
    tick.add_argument("--dry-run", action="store_true", help="plan without writes or launches")
    tick.add_argument("--run-budget", type=int, default=2700)
    tick.add_argument("--worker-timeout", type=float, default=3000)

    scan = subcommands.add_parser("scan", help="read and print the deterministic queue")
    scan.set_defaults(dry_run=True, run_budget=2700, worker_timeout=3000.0)

    heavy = subcommands.add_parser("heavy", help="run one resource-heavy command under a lease")
    heavy.add_argument("--timeout", type=float)
    heavy.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = RuntimePaths(args.runtime)

    if args.command == "heavy":
        command = list(args.argv)
        if command and command[0] == "--":
            command.pop(0)
        if not command:
            parser.error("heavy requires a command after --")
        try:
            arm_parent_death_signal()
            paths.ensure()
            command_cwd = os.environ.get("LOOP_HARNESS_COMMAND_CWD")
            result = HeavyRunner(paths.heavy_lock).run(
                command,
                cwd=Path(command_cwd) if command_cwd else None,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            print("heavy command timed out and was terminated", file=sys.stderr)
            return 124
        except (OSError, RuntimeError) as exc:
            print(f"heavy command failed: {redact_text(str(exc))}", file=sys.stderr)
            return 1
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        return int(result.returncode)

    try:
        registry = load_registry(args.registry)
        github = GitHubClient()
        runner = WorkerRunner(paths, run_budget_seconds=args.run_budget)
        harness = Harness(
            github=github,
            repositories=registry.enabled,
            paths=paths,
            workers=DEFAULT_WORKERS,
            worker_runner=runner,
            worker_timeout_seconds=args.worker_timeout,
        )
        previous_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
        }
        termination_requested = threading.Event()
        termination_signum: list[int | None] = [None]
        cleanup_wakeup = threading.Event()
        cleanup_finished = threading.Event()
        cleanup_errors: list[Exception] = []

        def cleanup_workers() -> None:
            try:
                cleanup_wakeup.wait()
                if termination_requested.is_set():
                    runner.terminate_all()
            except Exception as exc:
                cleanup_errors.append(exc)
            finally:
                cleanup_finished.set()

        cleanup_thread = threading.Thread(
            target=cleanup_workers,
            name="loop-harness-signal-cleanup",
            daemon=True,
        )
        cleanup_thread.start()

        def stop_workers(signum: int, _frame: object) -> None:
            if termination_signum[0] is not None:
                return
            termination_signum[0] = signum
            termination_requested.set()
            cleanup_wakeup.set()
            raise _TerminationRequested(signum)

        for signum in previous_handlers:
            signal.signal(signum, stop_workers)
        try:
            result = harness.tick(dry_run=bool(args.dry_run))
        finally:
            cleanup_wakeup.set()
            cleanup_finished.wait()
            cleanup_thread.join()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            if cleanup_errors:
                raise RuntimeError("worker shutdown did not complete cleanly") from cleanup_errors[0]
    except _TerminationRequested as exc:
        print("loop-harness: interrupted; active workers terminated", file=sys.stderr)
        return 128 + exc.signum
    except (ConfigError, GitHubError, OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"loop-harness: {redact_text(str(exc))}", file=sys.stderr)
        return 1
    print(json.dumps(_tick_json(result), indent=2, sort_keys=True))
    return 0
