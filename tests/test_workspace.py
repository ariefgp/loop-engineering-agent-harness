from __future__ import annotations

import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from loop_harness.models import Candidate, ResolvedSource, Role
from loop_harness.runtime import RuntimePaths
from loop_harness.scheduler import Assignment, WorkerSpec
from loop_harness.worker import WorkerRunner
from loop_harness.workspace import (
    WorkspaceManager,
    WorkspaceOutcome,
    _authenticated_clone,
    _authenticated_remote_head,
    ref_trace_argv,
)


def git(*argv: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def repository(root: Path) -> tuple[Path, Path, str]:
    remote = root / "remote.git"
    seed = root / "seed"
    canonical = root / "canonical"
    git("init", "--bare", "-q", str(remote))
    git("init", "-q", "-b", "main", str(seed))
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "Test", cwd=seed)
    (seed / "README.md").write_text("source\n", encoding="utf-8")
    git("add", "README.md", cwd=seed)
    git("commit", "-q", "-m", "source", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-q", "-u", "origin", "main", cwd=seed)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    git("clone", "-q", str(remote), str(canonical))
    return remote, canonical, git("rev-parse", "HEAD", cwd=canonical)


def manager_for(root: Path, remote: Path) -> WorkspaceManager:
    def clone_repository(slug: str, destination: Path) -> None:
        assert slug == "Example/project"
        git("clone", "--bare", "-q", str(remote), str(destination))

    def remote_head(slug: str, branch: str) -> str | None:
        assert slug == "Example/project"
        result = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "--verify", f"refs/heads/{branch}"],
            text=True, capture_output=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return WorkspaceManager(
        root, clone_repository=clone_repository, remote_head=remote_head,
        default_repo_slug="Example/project",
    )


def traced_git_commands(context, observation, commands: str) -> None:
    argv = [
        "strace", *ref_trace_argv(context, observation.trace_path),
        "--", "/bin/sh", "-c", commands,
    ]
    subprocess.run(argv, cwd=context.path, check=True)


class WorkspaceManagerTests(unittest.TestCase):
    def test_cleanup_pending_is_deleted_when_run_and_audit_roots_are_already_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _remote, canonical, source_sha = repository(root)
            manager = WorkspaceManager(root / "workspaces")
            context = manager.intent(
                "run-cleanup-recovery", canonical,
                "https://github.com/Example/project.git",
                ResolvedSource("default", source_sha, "main", None, None),
                Role.DEV, 12, repo_slug="Example/project",
            )
            context = replace(
                context,
                audit_path=manager.root / ".audit" / context.run_root.name,
            )

            outcome = manager.cleanup(
                context,
                WorkspaceOutcome(source_sha, True, True, True, "pending", None),
            )

            self.assertEqual("deleted", outcome.cleanup)
            self.assertIsNone(outcome.failure)

    def test_workspace_intent_derives_prepare_path_without_creating_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            workspace_root = root / "workspaces"
            manager = manager_for(workspace_root, remote)
            source = ResolvedSource("default", source_sha, "main", None, None)

            intent = manager.intent(
                "run-intent", canonical, str(remote), source, Role.DEV, 12
            )

            self.assertEqual(manager.run_root_for("run-intent", canonical), intent.run_root)
            self.assertFalse(intent.run_root.exists())
            prepared = manager.prepare(
                "run-intent", canonical, str(remote), source, Role.DEV, 12
            )
            self.assertEqual(intent.run_root, prepared.run_root)
            self.assertEqual(intent.path, prepared.path)

    def test_authenticated_clone_sanitizes_git_config_without_dropping_gh_auth(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "clone.git"
            captured: dict[str, object] = {}

            def observe(argv, *, cwd=None, environment=None):
                captured.update(argv=argv, cwd=cwd, environment=environment)
                return ""

            hostile = {
                "GH_TOKEN": "retained-auth-token",
                "GITHUB_TOKEN": "retained-secondary-token",
                "GH_HOST": "github.example.test",
                "PATH": "/trusted/bin",
                "LANG": "en_US.UTF-8",
                "HOME": str(root / "hostile-home"),
                "XDG_CONFIG_HOME": str(root / "xdg"),
                "SSH_AUTH_SOCK": str(root / "agent.sock"),
                "UNRELATED_SECRET": "must-not-leak",
                "GIT_SSH_COMMAND": "attacker-command",
                "GIT_PROXY_COMMAND": "attacker-proxy",
                "GIT_TEMPLATE_DIR": str(root / "template"),
                "GIT_OBJECT_DIRECTORY": str(root / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(root / "alternate"),
                "GIT_REPLACE_REF_BASE": "refs/evil/",
                "GIT_TRACE": "1",
                "GIT_CONFIG_SYSTEM": str(root / "system-config"),
                "GIT_CONFIG_GLOBAL": str(root / "global-config"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.file:///attacker/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/",
            }
            with patch.dict("loop_harness.workspace.os.environ", hostile, clear=True), patch(
                "loop_harness.workspace._run", side_effect=observe
            ):
                _authenticated_clone("Example/project", destination)

            environment = captured["environment"]
            self.assertIsInstance(environment, dict)
            assert isinstance(environment, dict)
            self.assertEqual("retained-auth-token", environment["GH_TOKEN"])
            self.assertEqual("retained-secondary-token", environment["GITHUB_TOKEN"])
            self.assertNotIn("GH_HOST", environment)
            self.assertEqual(
                ["gh", "repo", "clone", "github.com/Example/project", str(destination), "--",
                 "--bare", "--quiet", "--no-tags"],
                captured["argv"],
            )
            self.assertEqual("/trusted/bin", environment["PATH"])
            self.assertEqual(str(root / "hostile-home"), environment["HOME"])
            self.assertEqual(str(root / "xdg"), environment["XDG_CONFIG_HOME"])
            self.assertEqual("en_US.UTF-8", environment["LANG"])
            self.assertEqual("1", environment["GIT_CONFIG_NOSYSTEM"])
            self.assertEqual("/dev/null", environment["GIT_CONFIG_GLOBAL"])
            self.assertEqual("/dev/null", environment["GIT_CONFIG_SYSTEM"])
            self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])
            self.assertEqual("never", environment["GCM_INTERACTIVE"])
            configured = {
                environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(environment["GIT_CONFIG_COUNT"]))
            }
            self.assertEqual("", configured["credential.helper"])
            self.assertEqual("/dev/null", configured["core.hooksPath"])
            self.assertEqual("false", configured["core.fsmonitor"])
            self.assertEqual("/dev/null", configured["init.templateDir"])
            self.assertNotIn("url.file:///attacker/.insteadOf", configured)
            self.assertNotIn("GIT_CONFIG_KEY_4", environment)
            for key in (
                "SSH_AUTH_SOCK", "UNRELATED_SECRET", "GIT_SSH_COMMAND",
                "GIT_PROXY_COMMAND", "GIT_TEMPLATE_DIR", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_REPLACE_REF_BASE", "GIT_TRACE",
            ):
                self.assertNotIn(key, environment)

    def test_authenticated_remote_head_pins_github_and_uses_sanitized_environment(self) -> None:
        captured: dict[str, object] = {}

        def observe(argv, *, cwd=None, environment=None):
            captured.update(argv=argv, cwd=cwd, environment=environment)
            return json.dumps({"object": {"sha": "a" * 40}})

        hostile = {
            "GH_TOKEN": "retained-auth-token",
            "GH_HOST": "attacker.invalid",
            "HTTPS_PROXY": "http://attacker.invalid:8080",
            "GIT_SSH_COMMAND": "attacker-command",
            "PATH": "/trusted/bin",
            "HOME": "/trusted/home",
        }
        with patch.dict("loop_harness.workspace.os.environ", hostile, clear=True), patch(
            "loop_harness.workspace._run", side_effect=observe
        ):
            self.assertEqual(
                "a" * 40,
                _authenticated_remote_head("Example/project", "feature/topic"),
            )

        self.assertEqual(
            [
                "gh", "api", "--hostname", "github.com", "--method", "GET",
                "repos/Example/project/git/ref/heads/feature%2Ftopic",
            ],
            captured["argv"],
        )
        environment = captured["environment"]
        self.assertIsInstance(environment, dict)
        assert isinstance(environment, dict)
        self.assertEqual("retained-auth-token", environment["GH_TOKEN"])
        self.assertEqual("/trusted/bin", environment["PATH"])
        self.assertEqual("/trusted/home", environment["HOME"])
        for key in ("GH_HOST", "HTTPS_PROXY", "GIT_SSH_COMMAND"):
            self.assertNotIn(key, environment)

    def test_prepare_uses_validated_slug_for_authenticated_clone(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            calls: list[tuple[str, Path]] = []

            def clone_repository(slug: str, destination: Path) -> None:
                calls.append((slug, destination))
                git("clone", "--bare", "-q", str(remote), str(destination))

            manager = WorkspaceManager(
                root / "workspaces", clone_repository=clone_repository,
                remote_head=lambda _slug, _branch: None,
            )
            context = manager.prepare(
                "run-authenticated", canonical, "https://github.com/Example/project.git",
                ResolvedSource("default", source_sha, "main", None, None), Role.PM, 12,
                repo_slug="Example/project",
            )

            self.assertEqual("Example/project", context.repo_slug)
            self.assertEqual([("Example/project", context.git_dir)], calls)

    def test_prepare_cleans_run_root_when_baseline_enumeration_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            with patch("loop_harness.workspace._commit_objects", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    manager.prepare(
                        "run-baseline-fail", canonical, str(remote),
                        ResolvedSource("default", source_sha, "main", None, None), Role.PM, 12
                    )
            self.assertFalse(any((root / "workspaces").glob("*/run-baseline-fail")))

    def test_published_unrelated_dev_history_is_rejected_and_retained(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-unrelated", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            empty_tree = git("mktree", cwd=context.path)
            unrelated_sha = subprocess.run(
                ["git", "-C", str(context.path), "commit-tree", empty_tree, "-m", "unrelated"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            git("reset", "--hard", unrelated_sha, cwd=context.path)
            git("push", "-q", str(remote), f"HEAD:refs/heads/{context.remote_branch}", cwd=context.path)

            outcome = manager.finalize(
                canonical, context, Role.DEV, "completed", lambda _sha, _changed: True
            )

            self.assertEqual(unrelated_sha, outcome.final_sha)
            self.assertEqual("retained", outcome.cleanup)
            self.assertIn("descendant", outcome.failure or "")

    def test_unchanged_dev_need_confirmation_can_finish_without_push(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-blocked", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            verifier_calls: list[tuple[str, bool]] = []
            outcome = manager.finalize(
                canonical, context, Role.DEV, "completed",
                lambda sha, changed: verifier_calls.append((sha, changed)) or True,
            )
            self.assertEqual([(source_sha, False)], verifier_calls)
            self.assertFalse(outcome.push_verified)
            self.assertTrue(outcome.handoff_verified)
            self.assertEqual("deleted", outcome.cleanup)

    def test_completed_pm_and_qa_retain_clean_workspaces_when_handoff_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            source = ResolvedSource("default", source_sha, "main", None, None)

            for role, run_id in ((Role.PM, "run-pm-missing"), (Role.QA, "run-qa-missing")):
                with self.subTest(role=role):
                    context = manager.prepare(
                        run_id, canonical, str(remote), source, role, 12
                    )
                    outcome = manager.finalize(
                        canonical, context, role, "completed", lambda _sha, _changed: False
                    )
                    self.assertEqual("retained", outcome.cleanup)
                    self.assertFalse(outcome.handoff_verified)
                    self.assertIsNotNone(outcome.failure)
                    self.assertTrue(context.path.exists())

    def test_finalize_contains_remote_verification_failure_and_retains_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            def clone_repository(_slug: str, destination: Path) -> None:
                git("clone", "--bare", "-q", str(remote), str(destination))

            def failing_remote(_slug: str, _branch: str) -> str | None:
                raise RuntimeError("remote token=super-secret failed")

            manager = WorkspaceManager(
                root / "workspaces", clone_repository=clone_repository,
                remote_head=failing_remote, default_repo_slug="Example/project",
            )
            context = manager.prepare(
                "run-remote-failure", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "change.txt").write_text("change\n", encoding="utf-8")
            git("add", "change.txt", cwd=context.path)
            git("commit", "-q", "-m", "change", cwd=context.path)
            outcome = manager.finalize(canonical, context, Role.DEV, "completed")

            self.assertEqual("retained", outcome.cleanup)
            self.assertIsNotNone(outcome.failure)
            self.assertNotIn("super-secret", outcome.failure or "")
            self.assertTrue(context.path.exists())

    def test_each_run_owns_private_git_metadata_and_cannot_mutate_canonical_or_sibling(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            source = ResolvedSource("default", source_sha, "main", None, None)
            first = manager.prepare("run-one", canonical, str(remote), source, Role.DEV, 12)
            second = manager.prepare("run-two", canonical, str(remote), source, Role.DEV, 13)
            canonical_origin = git("remote", "get-url", "origin", cwd=canonical)
            canonical_refs = git("show-ref", cwd=canonical)

            git("remote", "set-url", "origin", "https://attacker.invalid/repo.git", cwd=first.path)
            git("update-ref", "refs/heads/attacker", source_sha, cwd=first.path)

            self.assertEqual(canonical_origin, git("remote", "get-url", "origin", cwd=canonical))
            self.assertEqual(canonical_refs, git("show-ref", cwd=canonical))
            self.assertEqual(str(remote), git("remote", "get-url", "origin", cwd=second.path))
            self.assertNotIn("refs/heads/attacker", git("show-ref", cwd=second.path))
            self.assertNotEqual(first.git_dir, second.git_dir)
            self.assertTrue(first.git_dir.is_relative_to(first.run_root))

    def test_finalize_verifies_literal_trusted_origin_after_worker_mutates_remote(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            attacker = root / "attacker.git"
            git("init", "--bare", "-q", str(attacker))
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-trusted", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "change.txt").write_text("done\n", encoding="utf-8")
            git("add", "change.txt", cwd=context.path)
            git("commit", "-q", "-m", "done", cwd=context.path)
            git("push", "-q", str(remote), f"HEAD:refs/heads/{context.remote_branch}", cwd=context.path)
            git("remote", "set-url", "origin", str(attacker), cwd=context.path)

            outcome = manager.finalize(canonical, context, Role.DEV, "completed")

            self.assertEqual(str(remote), context.expected_origin_url)
            self.assertTrue(outcome.push_verified)
            self.assertEqual("deleted", outcome.cleanup)

    def test_finalize_ignores_worker_url_rewrite_and_fsmonitor_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            attacker = root / "attacker.git"
            git("init", "--bare", "-q", str(attacker))
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-config-attacks", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "change.txt").write_text("done\n", encoding="utf-8")
            git("add", "change.txt", cwd=context.path)
            git("commit", "-q", "-m", "done", cwd=context.path)
            git("push", "-q", str(remote), f"HEAD:refs/heads/{context.remote_branch}", cwd=context.path)
            executed = root / "fsmonitor-executed"
            monitor = root / "monitor.sh"
            monitor.write_text(f"#!/bin/sh\ntouch {executed}\n", encoding="utf-8")
            monitor.chmod(0o755)
            git("config", "core.fsmonitor", str(monitor), cwd=context.path)
            git("config", f"url.{attacker}.insteadOf", str(remote), cwd=context.path)

            outcome = manager.finalize(canonical, context, Role.DEV, "completed")

            self.assertTrue(outcome.push_verified)
            self.assertEqual("deleted", outcome.cleanup)
            self.assertFalse(executed.exists())
            self.assertEqual(
                1,
                subprocess.run(
                    ["git", "show-ref"], cwd=attacker, capture_output=True, check=False
                ).returncode,
            )

    def test_prepare_ignores_global_url_rewrite_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            attacker = root / "attacker.git"
            git("init", "--bare", "-q", str(attacker))
            home = root / "hostile-home"
            home.mkdir()
            subprocess.run(
                [
                    "git", "config", "--file", str(home / ".gitconfig"),
                    f"url.{attacker}.insteadOf", str(remote),
                ],
                check=True,
            )
            manager = manager_for(root / "workspaces", remote)

            with patch.dict("os.environ", {"HOME": str(home)}):
                context = manager.prepare(
                    "run-global-rewrite", canonical, str(remote),
                    ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
                )

            self.assertEqual(source_sha, git("rev-parse", "HEAD", cwd=context.path))

    def test_prepare_rejects_invalid_remote_branch_before_creating_run_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            with self.assertRaisesRegex(ValueError, "invalid branch"):
                manager.prepare(
                    "run-invalid", canonical, str(remote),
                    ResolvedSource("pr", source_sha, None, "bad\nbranch", 7), Role.DEV, 12
                )
            self.assertFalse((root / "workspaces").exists())

    def test_unpublished_dev_commit_is_retained_as_recovery_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-unpublished", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "change.txt").write_text("unpublished\n", encoding="utf-8")
            git("add", "change.txt", cwd=context.path)
            git("commit", "-q", "-m", "unpublished", cwd=context.path)

            outcome = manager.finalize(canonical, context, Role.DEV, "failed")

            self.assertEqual("retained", outcome.cleanup)
            self.assertTrue(context.path.exists())
            self.assertIn("unpublished", (context.path / "change.txt").read_text())

    def test_dev_commit_reset_to_source_is_retained_as_recoverable_unpublished_work(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-reset-unpublished", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "lost.txt").write_text("recover me\n", encoding="utf-8")
            git("add", "lost.txt", cwd=context.path)
            git("commit", "-q", "-m", "recoverable", cwd=context.path)
            created = git("rev-parse", "HEAD", cwd=context.path)
            git("reset", "--hard", source_sha, cwd=context.path)

            outcome = manager.finalize(canonical, context, Role.DEV, "failed")

            self.assertEqual("retained", outcome.cleanup)
            self.assertTrue(context.run_root.exists())
            self.assertEqual("commit", git("cat-file", "-t", created, cwd=context.path))

    def test_completed_dev_is_removed_only_after_remote_contains_clean_head(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-published", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "change.txt").write_text("done\n", encoding="utf-8")
            git("add", "change.txt", cwd=context.path)
            git("commit", "-q", "-m", "done", cwd=context.path)
            final_sha = git("rev-parse", "HEAD", cwd=context.path)
            git("push", "-q", "origin", f"HEAD:refs/heads/{context.remote_branch}", cwd=context.path)

            outcome = manager.finalize(canonical, context, Role.DEV, "completed")

            self.assertEqual(final_sha, outcome.final_sha)
            self.assertTrue(outcome.push_verified)
            self.assertEqual("deleted", outcome.cleanup)
            self.assertFalse(context.path.exists())

    def test_completed_dev_accepts_assigned_branch_after_pack_refs_prunes_loose_ref(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-packed", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "packed.txt").write_text("published\n", encoding="utf-8")
            git("add", "packed.txt", cwd=context.path)
            git("commit", "-q", "-m", "packed publication", cwd=context.path)
            final_sha = git("rev-parse", "HEAD", cwd=context.path)
            git("push", "-q", "origin", f"HEAD:refs/heads/{context.remote_branch}", cwd=context.path)
            git("pack-refs", "--all", "--prune", cwd=context.path)
            assert context.local_branch is not None
            loose = context.git_dir.joinpath("refs", "heads", *context.local_branch.split("/"))
            self.assertFalse(loose.exists())

            outcome = manager.finalize(canonical, context, Role.DEV, "completed")

            self.assertEqual(final_sha, outcome.final_sha)
            self.assertTrue(outcome.push_verified)
            self.assertEqual("deleted", outcome.cleanup)

    def test_malformed_or_duplicate_packed_assigned_ref_fails_closed_and_is_retained(self) -> None:
        for corruption in ("malformed row\n", None):
            with self.subTest(corruption=corruption), TemporaryDirectory() as tmp:
                root = Path(tmp)
                remote, canonical, source_sha = repository(root)
                manager = manager_for(root / "workspaces", remote)
                context = manager.prepare(
                    "run-packed-bad", canonical, str(remote),
                    ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
                )
                git("pack-refs", "--all", "--prune", cwd=context.path)
                packed = context.git_dir / "packed-refs"
                if corruption is None:
                    corruption = f"{source_sha} refs/heads/{context.local_branch}\n"
                with packed.open("a", encoding="ascii") as stream:
                    stream.write(corruption)

                outcome = manager.inspect(canonical, context, Role.DEV, "completed")

                self.assertEqual("retained", outcome.cleanup)
                self.assertIsNotNone(outcome.failure)
                self.assertTrue(context.run_root.exists())

    def test_completed_dev_fails_when_an_authored_commit_was_abandoned(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-abandoned", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            (context.path / "abandoned.txt").write_text("lost\n", encoding="utf-8")
            git("add", "abandoned.txt", cwd=context.path)
            git("commit", "-q", "-m", "abandoned", cwd=context.path)
            abandoned_sha = git("rev-parse", "HEAD", cwd=context.path)
            git("reset", "--hard", source_sha, cwd=context.path)
            (context.path / "published.txt").write_text("kept\n", encoding="utf-8")
            git("add", "published.txt", cwd=context.path)
            git("commit", "-q", "-m", "published", cwd=context.path)
            final_sha = git("rev-parse", "HEAD", cwd=context.path)
            git("push", "-q", "origin", f"HEAD:refs/heads/{context.remote_branch}", cwd=context.path)

            outcome = manager.inspect(canonical, context, Role.DEV, "completed")

            self.assertEqual(final_sha, outcome.final_sha)
            self.assertTrue(outcome.push_verified)
            self.assertEqual("retained", outcome.cleanup)
            self.assertIn("abandoned", outcome.failure or "")
            self.assertEqual("commit", git("cat-file", "-t", abandoned_sha, cwd=context.path))

    def test_observation_ignores_unrelated_fetched_commit_after_published_tip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-fetch", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            observation = manager.start_object_observation(context)
            (context.path / "published.txt").write_text("published\n", encoding="utf-8")
            git("add", "published.txt", cwd=context.path)
            traced_git_commands(context, observation, "git commit -q -m published")
            final_sha = git("rev-parse", "HEAD", cwd=context.path)
            git("push", "-q", "origin", f"HEAD:refs/heads/{context.remote_branch}", cwd=context.path)

            producer = root / "producer"
            git("clone", "-q", str(remote), str(producer))
            git("config", "user.email", "test@example.com", cwd=producer)
            git("config", "user.name", "Test", cwd=producer)
            git("checkout", "-q", "-b", "unrelated", source_sha, cwd=producer)
            (producer / "unrelated.txt").write_text("remote only\n", encoding="utf-8")
            git("add", "unrelated.txt", cwd=producer)
            git("commit", "-q", "-m", "unrelated remote", cwd=producer)
            unrelated_sha = git("rev-parse", "HEAD", cwd=producer)
            git("push", "-q", "origin", "unrelated", cwd=producer)
            git("fetch", "-q", "origin", "unrelated:refs/remotes/origin/unrelated", cwd=context.path)
            context = replace(context, **observation.stop())

            self.assertIsNone(context.observation_error)
            self.assertIn(final_sha, context.observed_objects)
            self.assertNotIn(unrelated_sha, context.observed_objects)
            outcome = manager.finalize(
                canonical, context, Role.DEV, "completed", lambda _sha, _changed: True
            )
            self.assertEqual("deleted", outcome.cleanup)
            self.assertIsNone(outcome.failure)

    def test_observation_initialization_fails_closed_without_a_stable_audit_link(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-no-audit-link", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )

            with patch("loop_harness.workspace.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "tracing is unavailable"):
                    manager.start_object_observation(context)

            self.assertFalse((manager.root / ".audit" / context.run_root.name).exists())

    def test_worker_source_reflog_cannot_mutate_dispatcher_audit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-reflog-truncate", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            observation = manager.start_object_observation(context)
            source_reflog = context.git_dir.joinpath(
                "logs", "refs", "heads", *context.local_branch.split("/")
            )
            dispatcher_audit = observation.audit_reflog
            before = dispatcher_audit.read_bytes()

            source_reflog.write_bytes(b"")

            self.assertEqual(before, dispatcher_audit.read_bytes())
            self.assertFalse(os.path.samestat(source_reflog.stat(), dispatcher_audit.stat()))

    def test_forbidden_ref_mutations_fail_closed_and_retain_workspace(self) -> None:
        attacks = ("link", "packed-refs", "reflog-truncate")
        for attack in attacks:
            with self.subTest(attack=attack), TemporaryDirectory() as tmp:
                root = Path(tmp)
                remote, canonical, source_sha = repository(root)
                manager = manager_for(root / "workspaces", remote)
                context = manager.prepare(
                    f"run-forbidden-{attack}", canonical, str(remote),
                    ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
                )
                observation = manager.start_object_observation(context)
                assigned_ref = context.git_dir.joinpath(
                    "refs", "heads", *context.local_branch.split("/")
                )
                reflog = context.git_dir.joinpath(
                    "logs", "refs", "heads", *context.local_branch.split("/")
                )
                script = context.run_root / "attack.py"
                if attack == "link":
                    script.write_text(
                        "import os,pathlib,subprocess\n"
                        f"ref=pathlib.Path({str(assigned_ref)!r})\n"
                        f"source={source_sha!r}\n"
                        "tree=subprocess.check_output(['git','rev-parse',source+'^{tree}'],text=True).strip()\n"
                        "oid=subprocess.run(['git','commit-tree',tree,'-p',source],input='lost\\n',text=True,capture_output=True,check=True,env={**os.environ,'GIT_AUTHOR_NAME':'Test','GIT_AUTHOR_EMAIL':'test@example.com','GIT_COMMITTER_NAME':'Test','GIT_COMMITTER_EMAIL':'test@example.com'}).stdout.strip()\n"
                        "bad=ref.with_name('bad-ref'); good=ref.with_name('good-ref')\n"
                        "bad.write_text(oid+'\\n'); good.write_text(source+'\\n')\n"
                        "os.unlink(ref); os.link(bad,ref); os.unlink(ref); os.link(good,ref)\n"
                        "subprocess.run(['git','reflog','expire','--expire=now','--all'],check=True)\n"
                        "subprocess.run(['git','gc','--prune=now','--quiet'],check=True)\n",
                        encoding="utf-8",
                    )
                elif attack == "packed-refs":
                    script.write_text(
                        "import pathlib,subprocess\n"
                        "pathlib.Path('a').write_text('a')\n"
                        "subprocess.run(['git','add','a'],check=True); subprocess.run(['git','commit','-q','-m','A'],check=True)\n"
                        "pathlib.Path('b').write_text('b')\n"
                        "subprocess.run(['git','add','b'],check=True); subprocess.run(['git','commit','-q','-m','B'],check=True)\n"
                        f"subprocess.run(['git','push','-q','origin','HEAD:refs/heads/{context.remote_branch}'],check=True)\n"
                        "subprocess.run(['git','pack-refs','--all','--prune'],check=True)\n",
                        encoding="utf-8",
                    )
                else:
                    script.write_text(
                        "import os\n"
                        f"os.truncate({str(reflog)!r},0)\n",
                        encoding="utf-8",
                    )
                git("config", "user.email", "test@example.com", cwd=context.path)
                git("config", "user.name", "Test", cwd=context.path)
                traced_git_commands(
                    context, observation,
                    f"python3 {script}",
                )
                details = observation.stop()
                context = replace(context, **details)

                self.assertTrue(context.observation_error, attack)
                outcome = manager.inspect(canonical, context, Role.DEV, "completed")
                self.assertEqual("retained", outcome.cleanup)
                self.assertIn("observation", outcome.failure or "")

    def test_transient_assigned_ref_is_deterministically_retained_or_failed(self) -> None:
        # No sleeps or observer acknowledgements are allowed here: this is the
        # commit -> transient ref -> restore -> expire -> prune reviewer attack.
        for trial in range(20):
            with self.subTest(trial=trial), TemporaryDirectory() as tmp:
                root = Path(tmp)
                remote, canonical, source_sha = repository(root)
                manager = manager_for(root / "workspaces", remote)
                context = manager.prepare(
                    f"run-transient-{trial}", canonical, str(remote),
                    ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
                )
                observation = manager.start_object_observation(context)
                tree = git("rev-parse", f"{source_sha}^{{tree}}", cwd=context.path)
                abandoned_sha = subprocess.run(
                    ["git", "-C", str(context.path), "commit-tree", tree, "-p", source_sha],
                    input=f"transient {trial}\n", text=True, capture_output=True, check=True,
                    env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                         "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"},
                ).stdout.strip()
                assigned_ref = f"refs/heads/{context.local_branch}"
                source_reflog = context.git_dir.joinpath(
                    "logs", "refs", "heads", *context.local_branch.split("/")
                )
                traced_git_commands(
                    context, observation,
                    f"git update-ref {assigned_ref} {abandoned_sha} {source_sha} && "
                    f"git update-ref {assigned_ref} {source_sha} {abandoned_sha} && "
                    f": > {source_reflog} && git reflog expire --expire=now --all && "
                    "git gc --prune=now --quiet",
                )
                context = replace(context, **observation.stop())

                audit_reflog = context.audit_path / "assigned-branch.reflog"
                self.assertIn(abandoned_sha, audit_reflog.read_text(encoding="utf-8"))
                outcome = manager.inspect(canonical, context, Role.DEV, "completed")
                self.assertEqual("retained", outcome.cleanup)
                self.assertRegex(outcome.failure or "", "abandoned|observation")

    def test_real_worker_pruned_commit_is_observed_and_blocks_cleanup(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-pruned", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            executable = root / "pruning-worker"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, subprocess, sys\n"
                "sys.stdin.read()\n"
                "def git(*args):\n"
                " p=subprocess.run(['git',*args],text=True,capture_output=True,check=True)\n"
                " return p.stdout.strip()\n"
                "git('config','user.email','test@example.com')\n"
                "git('config','user.name','Test')\n"
                "pathlib.Path('lost.txt').write_text('lost\\n')\n"
                "git('add','lost.txt'); git('commit','-q','-m','abandoned')\n"
                "print(git('rev-parse','HEAD'), flush=True)\n"
                f"git('reset','--hard',{source_sha!r})\n"
                "git('reflog','expire','--expire=now','--all')\n"
                "git('gc','--prune=now','--quiet')\n"
                "pathlib.Path('published.txt').write_text('published\\n')\n"
                "git('add','published.txt'); git('commit','-q','-m','published')\n"
                f"git('push','-q','origin','HEAD:refs/heads/{context.remote_branch}')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            candidate = Candidate(
                "Example/project", canonical, 12, "work", Role.DEV, "todo", "P0",
                __import__("datetime").datetime.now(__import__("datetime").UTC),
            )
            assignment = Assignment(
                WorkerSpec("dev", Role.DEV, ".agents/agent-dev.md", executable=str(executable)),
                candidate, context,
            )

            observation = manager.start_object_observation(context)
            context = replace(context, audit_path=observation.audit_path)
            assignment = replace(assignment, workspace=context)
            def uncontained_popen(*args, **kwargs):
                environment = dict(kwargs["env"])
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
                kwargs["env"] = environment
                return subprocess.Popen(*args, **kwargs)

            result = WorkerRunner(
                RuntimePaths(root / "runtime"), popen=uncontained_popen,
            ).run(
                "run-pruned", assignment, timeout=30
            )
            context = replace(context, **observation.stop())
            final_sha = git("rev-parse", "HEAD", cwd=context.path)

            self.assertEqual(
                "completed", result.status,
                json.loads(result.result_path.read_text()).get("stderr"),
            )
            abandoned_sha = json.loads(result.result_path.read_text())["stdout"].strip()
            self.assertIn(
                abandoned_sha,
                (context.audit_path / "assigned-branch.reflog").read_text(encoding="utf-8"),
                (context.audit_path / "assigned-ref.trace").read_text(encoding="utf-8"),
            )
            self.assertNotEqual(0, subprocess.run(
                ["git", "-C", str(context.path), "cat-file", "-e", abandoned_sha],
                check=False, capture_output=True,
            ).returncode)
            outcome = manager.inspect(
                canonical, context, Role.DEV, "completed", lambda _sha, _changed: True
            )
            self.assertEqual("retained", outcome.cleanup)
            self.assertRegex(outcome.failure or "", "abandoned|observation")
            self.assertTrue(context.audit_path.is_dir())

    def test_durable_observation_index_recovers_pruned_commit_without_stop_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            manager = manager_for(root / "workspaces", remote)
            context = manager.prepare(
                "run-crashed-observer", canonical, str(remote),
                ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
            )
            git("config", "user.email", "test@example.com", cwd=context.path)
            git("config", "user.name", "Test", cwd=context.path)
            observation = manager.start_object_observation(context)
            context = replace(context, audit_path=observation.audit_path)
            (context.path / "lost.txt").write_text("lost\n", encoding="utf-8")
            git("add", "lost.txt", cwd=context.path)
            traced_git_commands(
                context, observation,
                f"git commit -q -m abandoned && git rev-parse HEAD > ../abandoned-oid && "
                f"git reset --hard {source_sha} && git reflog expire --expire=now --all && "
                "git gc --prune=now --quiet",
            )
            abandoned_sha = (context.run_root / "abandoned-oid").read_text().strip()
            details = observation.stop()

            self.assertEqual((), context.observed_objects)
            false_positives = (
                "unknown protected-path syscall fcntl",
                "assigned-reflog replacement lacked captured object id",
                "packed-refs mutation observed",
                "non-atomic assigned-ref lock open observed",
                "incomplete assigned-ref transaction",
            )
            error = details["observation_error"]
            self.assertTrue(error is None or isinstance(error, str))
            error_text = error if isinstance(error, str) else ""
            for message in false_positives:
                self.assertNotIn(message, error_text)
            outcome = manager.inspect(canonical, context, Role.DEV, "completed")

            self.assertEqual("retained", outcome.cleanup)
            self.assertRegex(outcome.failure or "", "abandoned|observation")

    def test_observation_index_rejects_symlinks_invalid_oids_and_oversized_data(self) -> None:
        for corruption in ("symlink", "invalid", "oversized"):
            with self.subTest(corruption=corruption), TemporaryDirectory() as tmp:
                root = Path(tmp)
                remote, canonical, source_sha = repository(root)
                manager = manager_for(root / "workspaces", remote)
                context = manager.prepare(
                    "run-bad-index", canonical, str(remote),
                    ResolvedSource("default", source_sha, "main", None, None), Role.DEV, 12
                )
                observation = manager.start_object_observation(context)
                details = observation.stop()
                context = replace(context, audit_path=details["audit_path"])
                index = context.audit_path / "objects.index"
                if corruption == "symlink":
                    index.unlink()
                    index.symlink_to(context.git_dir / "HEAD")
                elif corruption == "invalid":
                    index.chmod(0o600)
                    index.write_text("not-an-object\n", encoding="ascii")
                else:
                    index.chmod(0o600)
                    index.write_bytes(b"a" * 1_000_001)

                outcome = manager.inspect(canonical, context, Role.DEV, "failed")

                self.assertEqual("retained", outcome.cleanup)
                self.assertIsNotNone(outcome.failure)

    def test_prepare_creates_unique_dispatcher_worktree_without_changing_checkout(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, canonical, source_sha = repository(root)
            before_branch = git("branch", "--show-current", cwd=canonical)
            before_status = git("status", "--porcelain=v1", cwd=canonical)
            manager = manager_for(root / "workspaces", remote)
            source = ResolvedSource("default", source_sha, "main", None, None)

            first = manager.prepare("run-one", canonical, str(remote), source, Role.DEV, 12)
            second = manager.prepare("run-two", canonical, str(remote), source, Role.QA, 12)

            self.assertNotEqual(first.path, second.path)
            self.assertEqual(source_sha, git("rev-parse", "HEAD", cwd=first.path))
            self.assertEqual(source_sha, git("rev-parse", "HEAD", cwd=second.path))
            self.assertTrue(git("branch", "--show-current", cwd=first.path))
            self.assertEqual("", git("branch", "--show-current", cwd=second.path))
            self.assertEqual(before_branch, git("branch", "--show-current", cwd=canonical))
            self.assertEqual(before_status, git("status", "--porcelain=v1", cwd=canonical))


if __name__ == "__main__":
    unittest.main()
