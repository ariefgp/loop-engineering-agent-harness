from __future__ import annotations

import json
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path

from loop_harness.config import RepositoryConfig
from loop_harness.github import (
    GitHubClient, GitHubError, _default_runner, _screenshot_url_reachable,
    evidence_url_matches,
)
from loop_harness.models import ResolvedSource, Role


def handoff(
    run_id: str,
    role: str,
    state: str,
    *,
    issue: int = 12,
    pr_number: int | None = None,
    head_sha: str | None = None,
    evidence: list[dict[str, str]] | None = None,
) -> str:
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "role": role,
        "state": state,
        "issue": issue,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "evidence": evidence or [{
            "kind": "verification", "summary": "strict suite passed",
            "url": (
                "https://github.com/Example/project/actions/runs/123/job/456"
                if head_sha else "https://github.com/Example/project/issues/12"
            ),
        }],
    }
    body = "```loop-engineering-handoff\n" + json.dumps(payload) + "\n```"
    for item in payload["evidence"]:
        if item["kind"] == "screenshot":
            body += f"\n![QA screenshot]({item['url']})"
    return body


def authored_comment(body: str, *, authored: bool = True) -> dict[str, object]:
    return {
        "body": body,
        "viewerDidAuthor": authored,
        "url": "https://github.com/Example/project/issues/12#issuecomment-99",
    }


def artifact_payload(
    argv: list[str], sha: str, *, run_id: int = 123, job_id: int = 456,
    review_id: int = 1, comment_id: int = 123,
) -> dict[str, object] | None:
    endpoint = argv[-1]
    if endpoint == f"repos/Example/project/actions/jobs/{job_id}":
        return {
            "id": job_id, "run_id": run_id, "status": "completed",
            "conclusion": "success",
        }
    if endpoint == f"repos/Example/project/actions/runs/{run_id}":
        return {
            "id": run_id, "head_sha": sha, "status": "completed", "conclusion": "success",
            "repository": {"full_name": "Example/project"},
        }
    if endpoint == f"repos/Example/project/pulls/44/reviews/{review_id}":
        return {
            "id": review_id,
            "html_url": f"https://github.com/Example/project/pull/44#pullrequestreview-{review_id}",
            "pull_request_url": "https://api.github.com/repos/Example/project/pulls/44",
            "commit_id": sha, "state": "APPROVED", "submitted_at": "2026-09-14T01:02:03Z",
            "user": {"login": "automation-bot"},
        }
    if endpoint == f"repos/Example/project/issues/comments/{comment_id}":
        return {
            "id": comment_id,
            "html_url": f"https://github.com/Example/project/pull/44#issuecomment-{comment_id}",
            "issue_url": "https://api.github.com/repos/Example/project/issues/44",
            "body": f"Strict test suite passed for exact head {sha}",
            "user": {"login": "automation-bot"},
        }
    if endpoint == "user":
        return {"login": "automation-bot"}
    return None


class GitHubClientTests(unittest.TestCase):
    def test_production_reads_pin_github_and_drop_hostile_transport_environment(self) -> None:
        import os
        from unittest.mock import patch

        captured: list[tuple[list[str], dict[str, str]]] = []

        def run(argv, **kwargs):
            captured.append((argv, kwargs["env"]))
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        hostile = {
            "GH_HOST": "evil.example", "HTTPS_PROXY": "https://evil.example",
            "HTTP_PROXY": "http://evil.example", "ALL_PROXY": "socks://evil.example",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url.https://evil/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/", "GIT_SSH_COMMAND": "steal",
            "GIT_EXEC_PATH": "/tmp/evil", "GH_TOKEN": "keep-gh-token",
            "GITHUB_TOKEN": "keep-github-token", "PATH": "/usr/bin:/bin", "HOME": "/tmp/home",
        }
        repo = RepositoryConfig(
            "Example/project", Path("/srv/project"), True, (Role.DEV,)
        )
        with patch.dict(os.environ, hostile, clear=True), patch(
            "loop_harness.github.subprocess.run", side_effect=run
        ):
            GitHubClient(runner=_default_runner).scan([repo])

        argv, environment = captured[0]
        self.assertEqual("github.com/Example/project", argv[argv.index("--repo") + 1])
        self.assertEqual("keep-gh-token", environment["GH_TOKEN"])
        self.assertEqual("keep-github-token", environment["GITHUB_TOKEN"])
        for key in hostile.keys() - {"GH_TOKEN", "GITHUB_TOKEN", "PATH", "HOME"}:
            self.assertNotIn(key, environment)

    def test_exact_dev_and_qa_artifact_urls_reject_generic_pr_pages(self) -> None:
        sha = "a" * 40
        common = dict(repo="Example/project", issue=12, pr_number=44, head_sha=sha)
        rejected = (
            (Role.DEV, "blocker", "https://github.com/Example/project/issues/12"),
            (Role.DEV, "blocker", "https://github.com/Example/project/pull/44"),
            (Role.DEV, "verification", "https://github.com/Example/project/pull/44"),
            (Role.DEV, "verification", "https://github.com/Example/project/pull/44/files"),
            (Role.DEV, "verification", "https://github.com/Example/project/pull/44/arbitrary"),
            (Role.QA, "review", "https://github.com/Example/project/pull/44"),
            (Role.QA, "review", "https://github.com/Example/project/pull/44/files"),
            (Role.QA, "review", "https://github.com/Example/project/pull/44/arbitrary"),
            (Role.QA, "review", "https://github.com/Example/project/pull/44#discussion_r1"),
            (Role.QA, "test", "https://github.com/Example/project/pull/44/checks"),
        )
        for role, kind, url in rejected:
            with self.subTest(role=role, kind=kind, url=url):
                self.assertFalse(evidence_url_matches(role, kind, url, **common))

        self.assertTrue(evidence_url_matches(
            Role.DEV, "blocker",
            "https://github.com/Example/project/issues/12#issuecomment-5",
            repo="Example/project", issue=12, pr_number=None, head_sha=None,
        ))
        accepted = (
            (Role.DEV, "blocker", "https://github.com/Example/project/pull/44#issuecomment-6"),
            (Role.DEV, "verification", "https://github.com/Example/project/pull/44#issuecomment-123"),
            (Role.DEV, "verification", "https://github.com/Example/project/actions/runs/123/job/456"),
            (Role.QA, "test", "https://github.com/Example/project/actions/runs/123/job/456"),
            (Role.QA, "review", "https://github.com/Example/project/pull/44#pullrequestreview-789"),
        )
        for role, kind, url in accepted:
            with self.subTest(role=role, kind=kind, url=url):
                self.assertTrue(evidence_url_matches(role, kind, url, **common))
    def test_actions_evidence_is_read_back_and_bound_to_exact_pr_head(self) -> None:
        sha = "d" * 40
        old_sha = "e" * 40
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.DEV, "state": "todo", "number": 12
        })()

        def verify(
            job_sha: str, *, run_id: int = 123, job_id: int = 456,
            job_run_id: int | None = None, fetched_run_id: int = 123,
            run_repo: str = "Example/project", status: str = "completed",
            conclusion: str = "success",
        ) -> bool:
            handoff_body = handoff(
                "run-actions", "dev", "qa ready", pr_number=44, head_sha=sha,
                evidence=[{
                    "kind": "verification", "summary": "Strict suite completed successfully",
                    "url": f"https://github.com/Example/project/actions/runs/{run_id}/job/{job_id}",
                }],
            )

            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                endpoint = argv[-1]
                if argv[:3] == ["gh", "issue", "view"]:
                    payload = {
                        "state": "OPEN", "labels": [{"name": "qa ready"}],
                        "comments": [authored_comment(handoff_body)],
                    }
                elif "graphql" in argv:
                    payload = {"data": {"repository": {"issue": {
                        "closedByPullRequestsReferences": {
                            "nodes": [{
                                "number": 44, "state": "OPEN", "headRefName": "feature/12",
                                "headRefOid": sha,
                                "headRepository": {"nameWithOwner": "Example/project"},
                            }], "pageInfo": {"hasNextPage": False},
                        }
                    }}}}
                elif endpoint == f"repos/Example/project/actions/jobs/{job_id}":
                    payload = {
                        "id": job_id, "run_id": run_id if job_run_id is None else job_run_id,
                        "status": "completed",
                        "conclusion": "success",
                    }
                elif endpoint == f"repos/Example/project/actions/runs/{run_id}":
                    payload = {
                        "id": fetched_run_id, "status": status, "conclusion": conclusion,
                        "head_sha": job_sha,
                        "repository": {"full_name": run_repo},
                    }
                else:
                    raise AssertionError(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            return GitHubClient(runner=runner).verify_dev_handoff(
                candidate, "feature/12", sha, "run-actions"
            )

        self.assertTrue(verify(sha))
        self.assertFalse(verify(old_sha))
        self.assertFalse(verify(sha, run_id=999))
        self.assertFalse(verify(sha, job_run_id=999))
        self.assertFalse(verify(sha, fetched_run_id=999))
        self.assertFalse(verify(sha, run_repo="Other/project"))
        self.assertFalse(verify(sha, status="in_progress"))
        self.assertFalse(verify(sha, conclusion="failure"))

    def test_review_and_comment_artifacts_are_read_back_with_exact_provenance(self) -> None:
        sha = "a" * 40
        review_url = "https://github.com/Example/project/pull/44#pullrequestreview-1"
        comment_url = "https://github.com/Example/project/pull/44#issuecomment-123"

        def verify(kind: str, url: str, mutate=None) -> bool:
            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                payload = artifact_payload(argv, sha)
                if payload is None:
                    raise AssertionError(argv)
                if mutate is not None and argv[-1] != "user":
                    payload = mutate(dict(payload))
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return GitHubClient(runner=runner)._evidence_readback(
                Role.QA if kind == "review" else Role.DEV, kind, url,
                "Strict verification completed", "Example/project", 12, 44, sha,
                "run-artifact", "review ready" if kind == "review" else "qa ready",
            )

        self.assertTrue(verify("review", review_url))
        self.assertTrue(verify("verification", comment_url))
        self.assertTrue(verify("review", review_url, lambda payload: {
            **payload,
            "state": "COMMENTED",
            "body": "Reviewed the exact head; acceptance criteria and regression checks pass.",
        }))
        for state in ("CHANGES_REQUESTED", "DISMISSED"):
            with self.subTest(review_state=state):
                self.assertFalse(verify("review", review_url, lambda payload, state=state: {
                    **payload, "state": state, "body": "Substantive review body",
                }))
        self.assertFalse(verify("review", review_url, lambda payload: {
            **payload, "state": "COMMENTED", "body": "Looks good",
        }))
        self.assertFalse(verify("review", review_url, lambda payload: {
            **payload,
            "state": "COMMENTED",
            "body": "Do not merge: this is blocked until the authorization defect is fixed.",
        }))
        mutations = (
            lambda payload: {},
            lambda payload: {**payload, "commit_id": "b" * 40},
            lambda payload: {**payload, "pull_request_url": "https://api.github.com/repos/Other/project/pulls/44"},
            lambda payload: {**payload, "user": {"login": "other-user"}},
            lambda payload: {**payload, "submitted_at": None},
        )
        for mutate in mutations:
            with self.subTest(kind="review", mutate=mutate):
                self.assertFalse(verify("review", review_url, mutate))
        comment_mutations = (
            lambda payload: {},
            lambda payload: {**payload, "body": "Strict test suite passed for an old revision"},
            lambda payload: {**payload, "issue_url": "https://api.github.com/repos/Other/project/issues/44"},
            lambda payload: {**payload, "user": {"login": "other-user"}},
        )
        for mutate in comment_mutations:
            with self.subTest(kind="verification", mutate=mutate):
                self.assertFalse(verify("verification", comment_url, mutate))

    def test_handoff_rejects_untrusted_metadata_bad_provenance_and_placeholders(self) -> None:
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.PM,
            "state": "to be planned", "number": 12,
        })()
        run_id = "run-provenance"

        def verify(comment: object) -> bool:
            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                if argv[-1] == "repos/Example/project/issues/comments/7":
                    payload = {
                        "id": 7,
                        "html_url": "https://github.com/Example/project/issues/12#issuecomment-7",
                        "issue_url": "https://api.github.com/repos/Example/project/issues/12",
                        "body": valid_body,
                        "user": {"login": "automation-bot"},
                    }
                elif argv[-1] == "user":
                    payload = {"login": "automation-bot"}
                else:
                    payload = {
                        "state": "OPEN", "labels": [{"name": "plan approval"}],
                        "comments": [comment],
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return GitHubClient(runner=runner).verify_pm_handoff(candidate, run_id)

        valid_body = handoff(run_id, "pm", "plan approval", evidence=[{
            "kind": "plan", "summary": "Reviewed implementation boundaries",
            "url": "https://github.com/Example/project/issues/12#issuecomment-7",
        }])
        self.assertTrue(verify(authored_comment(valid_body)))
        invalid_comments = [
            authored_comment(valid_body, authored=False),
            {"body": valid_body, "viewerDidAuthor": 1,
             "url": "https://github.com/Example/project/issues/12#issuecomment-99"},
            {"body": valid_body, "viewerDidAuthor": True, "url": 99},
            authored_comment(valid_body.replace("github.com/Example", "evil.example/Example")),
            authored_comment(valid_body.replace("issues/12", "issues/13")),
            authored_comment(valid_body.replace("Reviewed implementation boundaries", "TODO")),
            authored_comment(valid_body.replace(
                "Reviewed implementation boundaries", "<substantive result>"
            )),
        ]
        for comment in invalid_comments:
            with self.subTest(comment=comment):
                self.assertFalse(verify(comment))

    def test_pm_evidence_requires_exact_read_back_issue_comment_with_run_content(self) -> None:
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.PM,
            "state": "to be planned", "number": 12,
        })()
        run_id = "run-pm-evidence"

        def verify(
            state: str,
            kind: str,
            evidence_url: str,
            *,
            mutate=None,
            missing: bool = False,
        ) -> bool:
            summary = (
                "Implementation plan defines acceptance criteria and rollout boundaries"
                if kind == "plan"
                else "Blocker requires product decision on the authorization boundary"
            )
            body = handoff(run_id, "pm", state, evidence=[{
                "kind": kind, "summary": summary, "url": evidence_url,
            }])

            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                endpoint = argv[-1]
                if argv[:3] == ["gh", "issue", "view"]:
                    payload = {
                        "state": "OPEN", "labels": [{"name": state}],
                        "comments": [authored_comment(body)],
                    }
                elif endpoint == "repos/Example/project/issues/comments/7":
                    if missing:
                        return subprocess.CompletedProcess(argv, 1, "", "not found")
                    payload = {
                        "id": 7,
                        "html_url": "https://github.com/Example/project/issues/12#issuecomment-7",
                        "issue_url": "https://api.github.com/repos/Example/project/issues/12",
                        "body": f"Run {run_id} {kind}: {summary}",
                        "user": {"login": "automation-bot"},
                    }
                    if mutate is not None:
                        payload = mutate(payload)
                elif endpoint == "user":
                    payload = {"login": "automation-bot"}
                else:
                    raise AssertionError(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            return GitHubClient(runner=runner).verify_pm_handoff(candidate, run_id)

        exact = "https://github.com/Example/project/issues/12#issuecomment-7"
        self.assertTrue(verify("plan approval", "plan", exact))
        self.assertTrue(verify("need confirmation", "blocker", exact))
        self.assertFalse(verify("plan approval", "plan", "https://github.com/Example/project/issues/12"))
        self.assertFalse(verify("plan approval", "plan", exact, missing=True))
        mutations = (
            lambda payload: {**payload, "id": 8},
            lambda payload: {**payload, "html_url": payload["html_url"].replace("-7", "-8")},
            lambda payload: {**payload, "issue_url": "https://api.github.com/repos/Other/project/issues/12"},
            lambda payload: {**payload, "user": {"login": "other-user"}},
            lambda payload: {**payload, "body": "Implementation plan for some other run"},
            lambda payload: {**payload, "body": f"Run {run_id}: status updated"},
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.assertFalse(verify("plan approval", "plan", exact, mutate=mutate))

    def test_dev_blocker_evidence_requires_exact_authenticated_comment_readback(self) -> None:
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.DEV, "state": "todo", "number": 12
        })()
        run_id = "run-blocked-dev"
        summary = "Human decision required before implementation can safely continue"

        def verify(*, changed: bool, mutate=None, missing: bool = False) -> bool:
            pr_number = 44 if changed else None
            head_sha = "a" * 40 if changed else None
            evidence_url = (
                "https://github.com/Example/project/pull/44#issuecomment-5"
                if changed else
                "https://github.com/Example/project/issues/12#issuecomment-5"
            )
            body = handoff(
                run_id, "dev", "need confirmation", pr_number=pr_number,
                head_sha=head_sha, evidence=[{
                    "kind": "blocker", "summary": summary, "url": evidence_url,
                }],
            )

            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                endpoint = argv[-1]
                if argv[:3] == ["gh", "issue", "view"]:
                    payload = {
                        "state": "OPEN", "labels": [{"name": "need confirmation"}],
                        "comments": [authored_comment(body)],
                    }
                elif endpoint == "repos/Example/project/issues/comments/5":
                    self.assertEqual(["gh", "api", "--hostname", "github.com"], argv[:4])
                    if missing:
                        return subprocess.CompletedProcess(argv, 1, "", "not found")
                    target = 44 if changed else 12
                    path = "pull/44" if changed else "issues/12"
                    payload = {
                        "id": 5,
                        "html_url": f"https://github.com/Example/project/{path}#issuecomment-5",
                        "issue_url": f"https://api.github.com/repos/Example/project/issues/{target}",
                        "body": f"Run {run_id} blocker: {summary}",
                        "user": {"login": "automation-bot"},
                    }
                    if mutate is not None:
                        payload = mutate(payload)
                elif endpoint == "user":
                    self.assertEqual(["gh", "api", "--hostname", "github.com"], argv[:4])
                    payload = {"login": "automation-bot"}
                else:
                    payload = {"data": {"repository": {"issue": {
                        "closedByPullRequestsReferences": {
                            "nodes": [{
                                "number": 44, "state": "OPEN", "headRefName": "loop/12-run",
                                "headRefOid": head_sha,
                                "headRepository": {"nameWithOwner": "Example/project"},
                            }], "pageInfo": {"hasNextPage": False},
                        }
                    }}}}
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            return GitHubClient(runner=runner).verify_dev_handoff(
                candidate, "loop/12-run", "a" * 40, run_id, has_changes=changed
            )

        self.assertTrue(verify(changed=False))
        self.assertTrue(verify(changed=True))
        self.assertFalse(verify(changed=False, missing=True))
        mutations = (
            lambda payload: {**payload, "id": 6},
            lambda payload: {**payload, "html_url": payload["html_url"].replace("-5", "-6")},
            lambda payload: {**payload, "issue_url": "https://api.github.com/repos/Other/project/issues/12"},
            lambda payload: {**payload, "issue_url": "https://api.github.com/repos/Example/project/issues/13"},
            lambda payload: {**payload, "user": {"login": "other-user"}},
            lambda payload: {**payload, "body": payload["body"].replace(run_id, "other-run")},
            lambda payload: {**payload, "body": payload["body"].replace("blocker", "note")},
            lambda payload: {**payload, "body": f"Run {run_id} blocker: a different substantive summary"},
        )
        for changed in (False, True):
            for mutate in mutations:
                with self.subTest(changed=changed, mutate=mutate):
                    self.assertFalse(verify(changed=changed, mutate=mutate))

    def test_dev_need_confirmation_with_changes_requires_pushed_closing_pr(self) -> None:
        sha = "a" * 40
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.DEV, "state": "todo", "number": 12
        })()
        run_id = "run-blocked-changed-dev"

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                body = handoff(
                    run_id, "dev", "need confirmation", pr_number=44, head_sha=sha,
                    evidence=[{
                        "kind": "blocker",
                        "summary": "Human decision required after publishing partial work",
                        "url": "https://github.com/Example/project/pull/44#issuecomment-5",
                    }],
                )
                payload = {
                    "state": "OPEN", "labels": [{"name": "need confirmation"}],
                    "comments": [authored_comment(body)],
                }
            elif argv[-1] == "repos/Example/project/issues/comments/5":
                payload = {
                    "id": 5,
                    "html_url": "https://github.com/Example/project/pull/44#issuecomment-5",
                    "issue_url": "https://api.github.com/repos/Example/project/issues/44",
                    "body": (
                        f"Run {run_id} blocker: Human decision required after publishing partial work"
                    ),
                    "user": {"login": "automation-bot"},
                }
            elif argv[-1] == "user":
                payload = {"login": "automation-bot"}
            else:
                payload = {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {
                        "nodes": [{
                            "number": 44, "state": "OPEN", "headRefName": "loop/12-run",
                            "headRefOid": sha,
                            "headRepository": {"nameWithOwner": "Example/project"},
                        }],
                        "pageInfo": {"hasNextPage": False},
                    }
                }}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        self.assertTrue(GitHubClient(runner=runner).verify_dev_handoff(
            candidate, "loop/12-run", sha, run_id, has_changes=True
        ))

    def test_closing_relation_requires_exact_typed_nodes_and_page_info(self) -> None:
        sha = "a" * 40
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.QA, "state": "qa ready", "number": 12
        })()
        valid_node = {
            "number": 44, "state": "OPEN", "headRefName": "feature/12",
            "headRefOid": sha, "headRepository": {"nameWithOwner": "Example/project"},
        }
        malformed = [
            {"nodes": [valid_node]},
            {"nodes": [valid_node], "pageInfo": {}},
            {"nodes": [valid_node], "pageInfo": {"hasNextPage": 0}},
            {"nodes": {"0": valid_node}, "pageInfo": {"hasNextPage": False}},
            {"nodes": [{**valid_node, "number": True}], "pageInfo": {"hasNextPage": False}},
        ]
        for relation in malformed:
            def runner(argv: list[str], relation=relation) -> subprocess.CompletedProcess[str]:
                payload = {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": relation
                }}}}
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            with self.subTest(relation=relation), self.assertRaisesRegex(
                GitHubError, "malformed closing PR relation|malformed closing PR head"
            ):
                GitHubClient(runner=runner).resolve_source(candidate)

    def test_handoff_verifiers_require_exact_run_linked_terminal_evidence(self) -> None:
        sha = "d" * 40
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.DEV, "state": "todo", "number": 12
        })()

        def client_for(labels: list[str], comments: list[str], *, issue_state: str = "OPEN"):
            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["gh", "issue", "view"]:
                    payload = {
                        "state": issue_state,
                        "labels": [{"name": label} for label in labels],
                        "comments": [authored_comment(body) for body in comments],
                    }
                else:
                    payload = artifact_payload(argv, sha)
                    if payload is None:
                        payload = {
                        "data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                            "nodes": [{
                                "number": 44, "state": "OPEN", "headRefName": "feature/12",
                                "headRefOid": sha,
                                "headRepository": {"nameWithOwner": "Example/project"},
                            }], "pageInfo": {"hasNextPage": False},
                        }}}}
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return GitHubClient(runner=runner)

        run_id = "run-exact-123"
        self.assertTrue(client_for(
            ["qa ready"], [handoff(run_id, "dev", "qa ready", pr_number=44, head_sha=sha)]
        ).verify_dev_handoff(candidate, "feature/12", sha, run_id))
        self.assertTrue(client_for(
            ["qa ready"], [
                handoff("run-prior", "dev", "qa ready", pr_number=44, head_sha=sha),
                handoff(run_id, "dev", "qa ready", pr_number=44, head_sha=sha),
            ]
        ).verify_dev_handoff(candidate, "feature/12", sha, run_id))
        for labels, comments, state in (
            (["qa ready"], ["qa ready handoff without run"], "OPEN"),
            (["qa ready", "feedback"], [handoff(run_id, "dev", "qa ready", pr_number=44, head_sha=sha)], "OPEN"),
            (["qa ready"], [handoff(run_id + "0", "dev", "qa ready", pr_number=44, head_sha=sha)], "OPEN"),
            (["qa ready"], [handoff(run_id, "dev", "qa ready", pr_number=44, head_sha=sha)], "CLOSED"),
        ):
            with self.subTest(labels=labels, comments=comments, state=state):
                self.assertFalse(client_for(labels, comments, issue_state=state).verify_dev_handoff(
                    candidate, "feature/12", sha, run_id
                ))

        valid = handoff(run_id, "dev", "qa ready", pr_number=44, head_sha=sha)
        malformed = valid.replace('"schema_version": 1', '"schema_version": 2')
        boolean_version = valid.replace('"schema_version": 1', '"schema_version": true')
        wrong_issue = valid.replace('"issue": 12', '"issue": 13')
        float_issue = valid.replace('"issue": 12', '"issue": 12.0')
        float_pr = valid.replace('"pr_number": 44', '"pr_number": 44.0')
        empty_evidence = valid.replace(
            '"evidence": [{"kind": "verification", "summary": "strict suite passed", "url": "https://github.com/Example/project/actions/runs/123/job/456"}]',
            '"evidence": []',
        )
        for comments in (
            [f"Loop Engineering run {run_id}: qa ready; tests passed"],
            [malformed],
            [boolean_version],
            [wrong_issue],
            [float_issue],
            [float_pr],
            [empty_evidence],
            [valid, valid],
            [valid + "\n" + valid],
        ):
            with self.subTest(comments=comments):
                self.assertFalse(
                    client_for(["qa ready"], comments).verify_dev_handoff(
                        candidate, "feature/12", sha, run_id
                    )
                )

    def test_pm_handoff_accepts_only_one_permitted_state_with_run_linked_comment(self) -> None:
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.PM,
            "state": "to be planned", "number": 12,
        })()
        run_id = "run-pm-123"

        def verify(labels: list[str], comments: list[str], state: str = "OPEN") -> bool:
            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                if argv[-1] == "repos/Example/project/issues/comments/1":
                    payload = {
                        "id": 1,
                        "html_url": "https://github.com/Example/project/issues/12#issuecomment-1",
                        "issue_url": "https://api.github.com/repos/Example/project/issues/12",
                        "body": comments[0] if comments else "",
                        "user": {"login": "automation-bot"},
                    }
                elif argv[-1] == "user":
                    payload = {"login": "automation-bot"}
                else:
                    payload = {
                        "state": state,
                        "labels": [{"name": label} for label in labels],
                        "comments": [authored_comment(body) for body in comments],
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return GitHubClient(runner=runner).verify_pm_handoff(candidate, run_id)

        for terminal in ("plan approval", "need confirmation"):
            self.assertTrue(verify(
                [terminal], [handoff(
                    run_id, "pm", terminal,
                    evidence=[{
                        "kind": "blocker" if terminal == "need confirmation" else "plan",
                        "summary": "decision and code references",
                        "url": "https://github.com/Example/project/issues/12#issuecomment-1",
                    }],
                )]
            ))
        self.assertFalse(verify(
            ["plan approval", "need confirmation"],
            [handoff(run_id, "pm", "plan approval")],
        ))
        self.assertFalse(verify(["plan approval"], ["planning evidence without the run id"]))
        self.assertFalse(verify(
            ["todo"], [handoff(run_id, "pm", "todo")],
        ))
        self.assertFalse(verify(
            ["plan approval"], [handoff(run_id, "pm", "plan approval")], "CLOSED"
        ))

    def test_qa_handoff_requires_unchanged_exact_pr_and_run_linked_final_evidence(self) -> None:
        sha = "e" * 40
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.QA,
            "state": "qa ready", "number": 12,
        })()
        expected = ResolvedSource("pr", sha, None, "feature/12", 44)
        run_id = "run-qa-123"

        def client_for(labels: list[str], comments: list[str], *, head: str = sha):
            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["gh", "issue", "view"]:
                    payload = {
                        "state": "OPEN",
                        "labels": [{"name": label} for label in labels],
                        "comments": [authored_comment(body) for body in comments],
                    }
                else:
                    payload = artifact_payload(argv, sha)
                    if payload is None:
                        payload = {
                        "data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                            "nodes": [{
                                "number": 44, "state": "OPEN", "headRefName": "feature/12",
                                "headRefOid": head,
                                "headRepository": {"nameWithOwner": "Example/project"},
                            }], "pageInfo": {"hasNextPage": False},
                        }}}}
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return GitHubClient(runner=runner, screenshot_checker=lambda _url: True)

        for terminal in ("review ready", "feedback", "need confirmation"):
            self.assertTrue(client_for(
                [terminal], [handoff(
                    run_id, "qa", terminal, pr_number=44, head_sha=sha,
                    evidence=[
                        {"kind": "test", "summary": "complete suite passed", "url": "https://github.com/Example/project/actions/runs/123/job/456"},
                        {"kind": "review", "summary": "review completed", "url": "https://github.com/Example/project/pull/44#pullrequestreview-1"},
                        {"kind": "screenshot", "summary": "rendered user interface", "url": "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"},
                    ],
                )]
            ).verify_qa_handoff(candidate, expected, run_id))
        self.assertFalse(client_for(
            ["review ready"], [handoff(run_id, "qa", "review ready", pr_number=44, head_sha=sha)], head="f" * 40
        ).verify_qa_handoff(candidate, expected, run_id))
        self.assertFalse(client_for(
            ["review ready", "feedback"],
            [handoff(run_id, "qa", "review ready", pr_number=44, head_sha=sha)],
        ).verify_qa_handoff(candidate, expected, run_id))
        self.assertFalse(client_for(
            ["review ready"], ["QA evidence without run link"],
        ).verify_qa_handoff(candidate, expected, run_id))

    def test_qa_screenshot_rejects_placeholder_and_requires_reachable_exact_url(self) -> None:
        sha = "e" * 40
        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.QA,
            "state": "qa ready", "number": 12,
        })()
        expected = ResolvedSource("pr", sha, None, "feature/12", 44)
        run_id = "run-qa-screenshot"
        checks: list[str] = []

        def verify(url: str, reachable: bool) -> bool:
            body = handoff(
                run_id, "qa", "review ready", pr_number=44, head_sha=sha,
                evidence=[
                    {"kind": "test", "summary": "complete suite passed", "url": "https://github.com/Example/project/actions/runs/123/job/456"},
                    {"kind": "review", "summary": "review completed", "url": "https://github.com/Example/project/pull/44#pullrequestreview-1"},
                    {"kind": "screenshot", "summary": "rendered user interface", "url": url},
                ],
            )

            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["gh", "issue", "view"]:
                    payload = {
                        "state": "OPEN", "labels": [{"name": "review ready"}],
                        "comments": [authored_comment(body)],
                    }
                else:
                    payload = artifact_payload(argv, sha)
                    if payload is None:
                        payload = {"data": {"repository": {"issue": {
                            "closedByPullRequestsReferences": {"nodes": [{
                                "number": 44, "state": "OPEN", "headRefName": "feature/12",
                                "headRefOid": sha,
                                "headRepository": {"nameWithOwner": "Example/project"},
                            }], "pageInfo": {"hasNextPage": False}}
                        }}}}
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            checker = lambda checked: checks.append(checked) or reachable
            return GitHubClient(runner=runner, screenshot_checker=checker).verify_qa_handoff(
                candidate, expected, run_id
            )

        placeholder = "https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000"
        attachment = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
        release = "https://github.com/Example/project/releases/download/v1.2.3/screenshot.png"
        self.assertFalse(verify(placeholder, True))
        self.assertEqual([], checks)
        self.assertFalse(verify(attachment, False))
        self.assertFalse(verify(release, True))
        self.assertEqual([attachment], checks)

    def test_default_screenshot_checker_validates_redirect_host_content_type_and_magic(self) -> None:
        attachment = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
        delivery = "https://private-user-images.githubusercontent.com/1/image.png"

        def valid(url: str) -> tuple[int, str | None, str | None, bytes]:
            if url == attachment:
                return 302, delivery, None, b""
            self.assertEqual(delivery, url)
            return 206, None, "image/png", b"\x89PNG\r\n\x1a\nrest"

        self.assertTrue(_screenshot_url_reachable(attachment, request=valid))

        invalid = (
            ("https://attacker.invalid/captured.png", "image/png", b"\x89PNG\r\n\x1a\n"),
            (delivery, "text/html", b"<html>not an image</html>"),
            (delivery, "image/png", b"not-a-png"),
            (delivery, "application/octet-stream", b"\xff\xd8\xffimage"),
        )
        for redirected, content_type, body in invalid:
            with self.subTest(redirected=redirected, content_type=content_type, body=body):
                def bad(url: str) -> tuple[int, str | None, str | None, bytes]:
                    if url == attachment:
                        return 302, redirected, None, b""
                    return 200, None, content_type, body

                self.assertFalse(_screenshot_url_reachable(attachment, request=bad))

        def unavailable(_url: str) -> tuple[int, str | None, str | None, bytes]:
            raise OSError("network unavailable")

        self.assertFalse(_screenshot_url_reachable(attachment, request=unavailable))

    def test_dev_handoff_requires_open_closing_pr_at_pushed_sha_and_qa_ready(self) -> None:
        sha = "d" * 40

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({
                        "state": "OPEN", "labels": [{"name": "qa ready"}],
                        "comments": [authored_comment(handoff("run-dev", "dev", "qa ready", pr_number=44, head_sha=sha))],
                    }), ""
                )
            payload = artifact_payload(argv, sha)
            if payload is None:
                payload = {
                    "data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                        "nodes": [{
                            "number": 44, "state": "OPEN", "headRefName": "feature/12",
                            "headRefOid": sha, "headRepository": {"nameWithOwner": "Example/project"}
                        }], "pageInfo": {"hasNextPage": False}
                    }}}}
                }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.DEV, "state": "todo", "number": 12
        })()
        self.assertTrue(
            GitHubClient(runner=runner).verify_dev_handoff(
                candidate, "feature/12", sha, "run-dev"
            )
        )

    def test_reads_contract_bytes_at_exact_revision_without_local_checkout(self) -> None:
        import base64

        sha = "c" * 40
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            encoded = base64.b64encode(b"contract\n").decode()
            payload = {"encoding": "base64", "content": encoded[:4] + "\n" + encoded[4:]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        content = GitHubClient(runner=runner).file_at_revision(
            "Example/project", ".agents/WORKFLOW.md", sha
        )

        self.assertEqual(b"contract\n", content)
        self.assertEqual(
            [
                "gh", "api", "--hostname", "github.com",
                "repos/Example/project/contents/.agents/WORKFLOW.md",
                "--method", "GET", "-f", f"ref={sha}",
            ],
            calls[0],
        )

    def test_resolves_qa_and_resumed_dev_from_single_open_closing_pr(self) -> None:
        sha = "b" * 40

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            payload = {
                "data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                    "nodes": [{
                        "number": 44, "state": "OPEN", "headRefName": "feature/issue-12",
                        "headRefOid": sha, "headRepository": {"nameWithOwner": "Example/project"}
                    }], "pageInfo": {"hasNextPage": False}
                }}}}
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        client = GitHubClient(runner=runner)
        for role, state in ((Role.QA, "qa ready"), (Role.QA, "qa in progress"),
                            (Role.DEV, "feedback"), (Role.DEV, "in progress")):
            candidate = type("Candidate", (), {
                "repo": "Example/project", "role": role, "state": state, "number": 12
            })()
            source = client.resolve_source(candidate)
            self.assertEqual(("pr", sha, "feature/issue-12", 44), (
                source.kind, source.sha, source.remote_branch, source.pr_number
            ))

    def test_resolves_default_branch_to_exact_head_oid(self) -> None:
        calls: list[list[str]] = []
        sha = "a" * 40

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if "repos/Example/project" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"default_branch": "trunk"}), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"object": {"sha": sha}}), "")

        candidate = type("Candidate", (), {
            "repo": "Example/project", "role": Role.DEV, "state": "todo", "number": 12
        })()
        source = GitHubClient(runner=runner).resolve_source(candidate)

        self.assertEqual(("default", sha, "trunk", None, None), (
            source.kind, source.sha, source.default_branch, source.remote_branch, source.pr_number
        ))
        self.assertEqual("repos/Example/project/git/ref/heads/trunk", calls[1][4])

    def test_rejects_control_characters_and_git_dangerous_branch_names(self) -> None:
        sha = "a" * 40
        dangerous = (
            "bad\nbranch", "bad\tbranch", "bad\x01branch", "bad\u0085branch",
            "-option", "double..dot", "name.lock", "dir/.hidden", "double//slash",
            "bad@{revision", "trailing.", "trailing/", "back\\slash",
        )

        for branch in dangerous:
            with self.subTest(branch=repr(branch)):
                def runner(argv: list[str], branch: str = branch) -> subprocess.CompletedProcess[str]:
                    payload = {
                        "data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                            "nodes": [{
                                "number": 44, "state": "OPEN", "headRefName": branch,
                                "headRefOid": sha,
                                "headRepository": {"nameWithOwner": "Example/project"},
                            }], "pageInfo": {"hasNextPage": False},
                        }}}}
                    }
                    return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

                candidate = type("Candidate", (), {
                    "repo": "Example/project", "role": Role.QA,
                    "state": "qa ready", "number": 12,
                })()
                with self.assertRaisesRegex(GitHubError, "malformed closing PR head"):
                    GitHubClient(runner=runner).resolve_source(candidate)

    def test_scan_uses_one_snapshot_and_maps_labels_to_roles(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            rows = [
                {
                    "number": 12,
                    "title": "Implement safely",
                    "labels": [{"name": "todo"}, {"name": "P1"}],
                    "createdAt": "2026-09-10T00:00:00Z",
                    "updatedAt": "2026-09-11T00:00:00Z",
                }
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

        repo = RepositoryConfig(
            "Example/project", Path("/srv/project"), True, (Role.PM, Role.DEV, Role.QA)
        )
        candidates = GitHubClient(runner=runner).scan([repo])

        self.assertEqual([("Example/project", 12)], [item.identity for item in candidates])
        self.assertEqual(Role.DEV, candidates[0].role)
        self.assertEqual("P1", candidates[0].priority)
        self.assertEqual(1, len(calls))
        self.assertTrue(all(call[:3] == ["gh", "issue", "list"] for call in calls))
        self.assertTrue(all("--state" in call and "open" in call for call in calls))
        self.assertTrue(all("--label" not in call and "1000" in call for call in calls))
        forbidden = {"edit", "comment", "close", "reopen"}
        self.assertFalse(any(forbidden.intersection(call) for call in calls))

    def test_github_failure_is_visible_instead_of_becoming_empty_queue(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, "", "authentication failed")

        repo = RepositoryConfig("Example/project", Path("/srv/project"), True, (Role.DEV,))
        with self.assertRaisesRegex(GitHubError, "authentication failed"):
            GitHubClient(runner=runner).scan([repo])

    def test_github_failure_redacts_credentials(self) -> None:
        secret = "github_pat_example-secret-value"

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 1, "", f"Authorization: Bearer {secret}"
            )

        repo = RepositoryConfig("Example/project", Path("/srv/project"), True, (Role.DEV,))
        with self.assertRaises(GitHubError) as raised:
            GitHubClient(runner=runner).scan([repo])
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_rejects_malformed_github_output(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "not-json", "")

        repo = RepositoryConfig("Example/project", Path("/srv/project"), True, (Role.DEV,))
        with self.assertRaises(GitHubError):
            GitHubClient(runner=runner).scan([repo])

    def test_scan_quarantines_issue_with_multiple_workflow_states(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            rows = [
                {
                    "number": 13,
                    "title": "Ambiguous lifecycle",
                    "labels": [{"name": "todo"}, {"name": "qa ready"}, {"name": "P0"}],
                    "createdAt": "2026-09-10T00:00:00Z",
                    "updatedAt": "2026-09-11T00:00:00Z",
                }
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

        repo = RepositoryConfig(
            "Example/project", Path("/srv/project"), True, (Role.PM, Role.DEV, Role.QA)
        )
        self.assertEqual([], GitHubClient(runner=runner).scan([repo]))

    def test_scan_quarantines_multiple_priorities_but_allows_missing_priority(self) -> None:
        rows = [
            {
                "number": 14,
                "title": "Ambiguous priority",
                "labels": [{"name": "todo"}, {"name": "P0"}, {"name": "high"}],
                "updatedAt": "2026-09-11T00:00:00Z",
            },
            {
                "number": 15,
                "title": "No priority",
                "labels": [{"name": "qa ready"}],
                "updatedAt": "2026-09-11T00:00:00Z",
            },
        ]

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

        repo = RepositoryConfig(
            "Example/project", Path("/srv/project"), True, (Role.DEV, Role.QA)
        )
        candidates = GitHubClient(runner=runner).scan([repo])
        self.assertEqual([15], [item.number for item in candidates])
        self.assertEqual("missing", candidates[0].priority)


if __name__ == "__main__":
    unittest.main()
