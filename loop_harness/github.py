from __future__ import annotations

import json
import base64
import binascii
import subprocess
import re
import os
from urllib.parse import quote, urljoin, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import unicodedata
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from .config import RepositoryConfig
from .git_refs import is_valid_branch_name
from .models import Candidate, ResolvedSource, Role, deduplicate_and_sort
from .runtime import redact_text


class GitHubError(RuntimeError):
    pass


_STATE_ROLES: tuple[tuple[str, Role], ...] = (
    ("to be planned", Role.PM),
    ("in progress", Role.DEV),
    ("feedback", Role.DEV),
    ("todo", Role.DEV),
    ("qa in progress", Role.QA),
    ("qa ready", Role.QA),
)
_PRIORITY_NAMES = {"urgent", "p0", "high", "p1", "medium", "p2", "low"}
_WORKFLOW_STATES = {
    "to be planned",
    "plan approval",
    "need confirmation",
    "todo",
    "in progress",
    "qa ready",
    "qa in progress",
    "feedback",
    "review ready",
    "done",
    "hold",
}
_HANDOFF_BLOCK = re.compile(
    r"```loop-engineering-handoff[ \t]*\r?\n(?P<payload>.*?)\r?\n```",
    re.DOTALL,
)
_HANDOFF_KEYS = {
    "schema_version", "run_id", "role", "state", "issue",
    "pr_number", "head_sha", "evidence",
}
_EVIDENCE_KEYS = {"kind", "summary", "url"}
_PLACEHOLDER_SUMMARIES = {
    "todo", "tbd", "n/a", "na", "none", "test", "placeholder",
    "substantive result", "exact commands and results", "meaningful summary",
}
_REVIEW_BLOCKER = re.compile(
    r"\b(?:block(?:ed|er|ing)?|changes? requested|do not merge|not ready|must fix|reject(?:ed)?)\b",
    re.IGNORECASE,
)
_SCREENSHOT_DELIVERY_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "user-images.githubusercontent.com",
    "private-user-images.githubusercontent.com",
    "github-production-user-asset-6210df.s3.amazonaws.com",
}
_SCREENSHOT_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _screenshot_request(url: str) -> tuple[int, str | None, str | None, bytes]:
    headers = {
        "User-Agent": "loop-engineering-screenshot-verifier/1",
        "Range": "bytes=0-31",
        "Accept": "image/png,image/jpeg,image/webp,image/gif",
    }
    # Private-repository user attachments return 404 anonymously. Authenticate
    # only the initial github.com request; never forward the token to signed
    # object-storage redirects.
    if urlsplit(url).hostname == "github.com":
        token_result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=_github_environment(),
        )
        token = token_result.stdout.strip()
        if token_result.returncode != 0 or not token:
            return 401, None, None, b""
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect).open(request, timeout=10) as response:
            return (
                int(response.status), response.headers.get("Location"),
                response.headers.get("Content-Type"), response.read(32),
            )
    except HTTPError as exc:
        return exc.code, exc.headers.get("Location"), exc.headers.get("Content-Type"), b""


def _allowed_screenshot_redirect(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and (
            parsed.hostname in _SCREENSHOT_DELIVERY_HOSTS
            or re.fullmatch(
                r"github-production-user-asset-[a-z0-9-]+\.s3\.amazonaws\.com",
                parsed.hostname or "",
            ) is not None
        )
        and parsed.username is None and parsed.password is None and parsed.port is None
    )


def _screenshot_url_reachable(
    url: str,
    *,
    request: Callable[[str], tuple[int, str | None, str | None, bytes]] = _screenshot_request,
) -> bool:
    parsed = _github_url(url)
    if parsed is None:
        return False
    parts, _fragment = parsed
    if not (
        parts[:2] == ["user-attachments", "assets"]
        and len(parts) == 3
        and _SCREENSHOT_UUID.fullmatch(parts[2])
        and set(parts[2].replace("-", "")) != {"0"}
    ):
        return False
    current = url
    for _redirect in range(5):
        if not _allowed_screenshot_redirect(current):
            return False
        try:
            status, location, content_type, body = request(current)
        except Exception:
            return False
        if 200 <= status < 300:
            media_type = (content_type or "").split(";", 1)[0].strip().casefold()
            magic = (
                body.startswith(b"\x89PNG\r\n\x1a\n")
                or body.startswith(b"\xff\xd8\xff")
                or body.startswith((b"GIF87a", b"GIF89a"))
                or (len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP")
            )
            return media_type.startswith("image/") and magic
        if 300 <= status < 400 and location:
            current = urljoin(current, location)
            continue
        return False
    return False


def _github_url(url: str) -> tuple[list[str], str] | None:
    if "<" in url or ">" in url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https" or parsed.hostname != "github.com"
        or parsed.username is not None or parsed.password is not None
        or parsed.port is not None or parsed.query
    ):
        return None
    return [part for part in parsed.path.split("/") if part], parsed.fragment


def _repo_prefix(parts: list[str], repo: str) -> list[str] | None:
    owner, name = repo.split("/", 1)
    if len(parts) < 2 or [part.casefold() for part in parts[:2]] != [
        owner.casefold(), name.casefold()
    ]:
        return None
    return parts[2:]


def _meaningful_summary(value: str) -> bool:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).strip()
    lowered = normalized.casefold().strip("<>[]{}() .:_-")
    return (
        len(normalized) >= 12
        and "<" not in normalized
        and ">" not in normalized
        and lowered not in _PLACEHOLDER_SUMMARIES
    )


def evidence_url_matches(
    role: Role, kind: str, url: str, *, repo: str, issue: int,
    pr_number: int | None, head_sha: str | None,
) -> bool:
    parsed = _github_url(url)
    if parsed is None:
        return False
    parts, fragment = parsed
    if kind == "screenshot":
        attachment = bool(
            parts[:2] == ["user-attachments", "assets"]
            and len(parts) == 3
            and _SCREENSHOT_UUID.fullmatch(parts[2])
            and set(parts[2].replace("-", "")) != {"0"}
        )
        return attachment
    suffix = _repo_prefix(parts, repo)
    if suffix is None:
        return False
    issue_path = suffix == ["issues", str(issue)]
    pr_path = pr_number is not None and suffix == ["pull", str(pr_number)]
    exact_run = (
        len(suffix) == 5
        and suffix[:2] == ["actions", "runs"]
        and suffix[2].isdigit()
        and suffix[3] == "job"
        and suffix[4].isdigit()
    )
    verification_comment = bool(
        pr_path and re.fullmatch(r"issuecomment-\d+", fragment)
    )
    if role is Role.PM:
        return bool(
            kind in {"plan", "blocker"}
            and issue_path
            and re.fullmatch(r"issuecomment-\d+", fragment)
        )
    if role is Role.DEV:
        if kind == "blocker":
            comment = re.fullmatch(r"issuecomment-\d+", fragment) is not None
            return bool(comment and (pr_path if pr_number is not None else issue_path))
        return kind == "verification" and (exact_run or verification_comment)
    if kind == "test":
        return exact_run
    if kind == "review":
        return bool(pr_path and re.fullmatch(r"pullrequestreview-\d+", fragment))
    return False


def required_evidence_kinds(role: Role, final_state: str) -> set[str]:
    if role is Role.PM:
        return {"blocker"} if final_state == "need confirmation" else {"plan"}
    if role is Role.DEV:
        return {"blocker"} if final_state == "need confirmation" else {"verification"}
    return {"test", "review", "screenshot"}


def _validated_handoff(
    comments: list[object],
    *,
    run_id: str,
    role: Role,
    final_state: str,
    issue: int,
    pr_number: int | None,
    head_sha: str | None,
    repo: str,
    screenshot_checker: Callable[[str], bool],
    evidence_checker: Callable[
        [Role, str, str, str, str, int, int | None, str | None, str, str], bool
    ],
) -> bool:
    blocks: list[tuple[dict[str, object], str]] = []
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
            continue
        body = comment["body"]
        for match in _HANDOFF_BLOCK.finditer(body):
            try:
                payload = json.loads(match.group("payload"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("run_id") == run_id:
                if (
                    type(comment.get("viewerDidAuthor")) is not bool
                    or comment.get("viewerDidAuthor") is not True
                    or not isinstance(comment.get("url"), str)
                    or not evidence_url_matches(
                        Role.PM, "plan", comment["url"], repo=repo, issue=issue,
                        pr_number=None, head_sha=None,
                    )
                ):
                    blocks.append(({}, body))
                else:
                    blocks.append((payload, body))
    if len(blocks) != 1:
        return False
    payload, body = blocks[0]
    if set(payload) != _HANDOFF_KEYS:
        return False
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("run_id") != run_id
        or payload.get("role") != role.value
        or payload.get("state") != final_state
        or type(payload.get("issue")) is not int
        or payload.get("issue") != issue
        or payload.get("head_sha") != head_sha
    ):
        return False
    actual_pr = payload.get("pr_number")
    if (pr_number is None and actual_pr is not None) or (
        pr_number is not None
        and (type(actual_pr) is not int or actual_pr != pr_number)
    ):
        return False
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    kinds: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
            return False
        kind, summary, url = item.get("kind"), item.get("summary"), item.get("url")
        if not all(isinstance(value, str) and value.strip() for value in (kind, summary, url)):
            return False
        assert isinstance(kind, str) and isinstance(summary, str) and isinstance(url, str)
        if not _meaningful_summary(summary) or not evidence_url_matches(
            role, kind, url, repo=repo, issue=issue,
            pr_number=pr_number, head_sha=head_sha,
        ):
            return False
        try:
            if not evidence_checker(
                role, kind, url, summary, repo, issue, pr_number, head_sha,
                run_id, final_state,
            ):
                return False
        except Exception:
            return False
        kinds.add(kind)
        if kind == "screenshot" and re.search(
            rf"!\[[^\]\r\n]+\]\({re.escape(url)}\)", body
        ) is None:
            return False
        if kind == "screenshot":
            try:
                if not screenshot_checker(url):
                    return False
            except Exception:
                return False
    required = required_evidence_kinds(role, final_state)
    return required.issubset(kinds)


def _github_environment() -> dict[str, str]:
    # A minimal allowlist prevents gh and any child Git process from inheriting
    # host selectors, proxies, executable/config overrides, or unrelated secrets.
    allowed = (
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "GH_CONFIG_DIR",
        "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN",
    )
    environment = {
        key: value for key in allowed if (value := os.environ.get(key)) is not None
    }
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment.setdefault("HOME", os.path.expanduser("~"))
    return environment


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=_github_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"GitHub command failed: {redact_text(str(exc))}") from exc


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise GitHubError("GitHub issue is missing updatedAt")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise GitHubError(f"invalid GitHub timestamp: {value!r}") from exc


class GitHubClient:
    def __init__(
        self,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _default_runner,
        screenshot_checker: Callable[[str], bool] = _screenshot_url_reachable,
    ) -> None:
        self._runner = runner
        self._screenshot_checker = screenshot_checker

    def _json(self, argv: list[str], context: str) -> object:
        result = self._runner(argv)
        if result.returncode != 0:
            detail = redact_text(result.stderr.strip()) or f"exit {result.returncode}"
            raise GitHubError(f"{context}: {detail}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"{context}: invalid JSON") from exc

    @staticmethod
    def _repo_identity(payload: object, repo: str) -> bool:
        return bool(
            isinstance(payload, dict)
            and isinstance(payload.get("repository"), dict)
            and isinstance(payload["repository"].get("full_name"), str)
            and payload["repository"]["full_name"].casefold() == repo.casefold()
        )

    def _evidence_readback(
        self, role: Role, kind: str, url: str, summary: str, repo: str,
        issue: int, pr_number: int | None, head_sha: str | None,
        run_id: str, final_state: str,
    ) -> bool:
        if role is Role.PM:
            parsed = _github_url(url)
            if parsed is None:
                return False
            parts, fragment = parsed
            suffix = _repo_prefix(parts, repo)
            match = re.fullmatch(r"issuecomment-(\d+)", fragment)
            if suffix != ["issues", str(issue)] or match is None:
                return False
            comment_id = int(match.group(1))
            comment = self._json(
                ["gh", "api", "--hostname", "github.com", f"repos/{repo}/issues/comments/{comment_id}"],
                f"{repo}: PM issue comment evidence",
            )
            viewer = self._json(
                ["gh", "api", "--hostname", "github.com", "user"],
                "authenticated GitHub identity",
            )
            expected_issue = f"https://api.github.com/repos/{repo}/issues/{issue}"
            author = comment.get("user") if isinstance(comment, dict) else None
            body = comment.get("body") if isinstance(comment, dict) else None
            normalized_body = (
                " ".join(unicodedata.normalize("NFKC", body).split())
                if isinstance(body, str) else ""
            )
            normalized_summary = " ".join(unicodedata.normalize("NFKC", summary).split())
            expected_kind = "blocker" if final_state == "need confirmation" else "plan"
            return bool(
                kind == expected_kind
                and isinstance(comment, dict) and isinstance(viewer, dict)
                and type(comment.get("id")) is int and comment.get("id") == comment_id
                and comment.get("html_url") == url
                and comment.get("issue_url") == expected_issue
                and _meaningful_summary(normalized_body)
                and run_id.casefold() in normalized_body.casefold()
                and expected_kind in normalized_body.casefold()
                and normalized_summary.casefold() in normalized_body.casefold()
                and isinstance(author, dict) and isinstance(author.get("login"), str)
                and isinstance(viewer.get("login"), str)
                and author["login"].casefold() == viewer["login"].casefold()
            )
        if role is Role.DEV and kind == "blocker":
            parsed = _github_url(url)
            if parsed is None or final_state != "need confirmation":
                return False
            parts, fragment = parsed
            suffix = _repo_prefix(parts, repo)
            match = re.fullmatch(r"issuecomment-(\d+)", fragment)
            expected_number = pr_number if pr_number is not None else issue
            expected_path = (
                ["pull", str(pr_number)]
                if pr_number is not None else ["issues", str(issue)]
            )
            if suffix != expected_path or match is None:
                return False
            comment_id = int(match.group(1))
            comment = self._json(
                ["gh", "api", "--hostname", "github.com", f"repos/{repo}/issues/comments/{comment_id}"],
                f"{repo}: Dev blocker comment evidence",
            )
            viewer = self._json(
                ["gh", "api", "--hostname", "github.com", "user"],
                "authenticated GitHub identity",
            )
            expected_issue = f"https://api.github.com/repos/{repo}/issues/{expected_number}"
            author = comment.get("user") if isinstance(comment, dict) else None
            body = comment.get("body") if isinstance(comment, dict) else None
            normalized_body = (
                " ".join(unicodedata.normalize("NFKC", body).split())
                if isinstance(body, str) else ""
            )
            normalized_summary = " ".join(unicodedata.normalize("NFKC", summary).split())
            return bool(
                isinstance(comment, dict) and isinstance(viewer, dict)
                and type(comment.get("id")) is int and comment.get("id") == comment_id
                and comment.get("html_url") == url
                and comment.get("issue_url") == expected_issue
                and _meaningful_summary(normalized_body)
                and run_id.casefold() in normalized_body.casefold()
                and "blocker" in normalized_body.casefold()
                and normalized_summary.casefold() in normalized_body.casefold()
                and isinstance(author, dict) and isinstance(author.get("login"), str)
                and isinstance(viewer.get("login"), str)
                and author["login"].casefold() == viewer["login"].casefold()
            )
        if kind == "screenshot":
            return True
        parsed = _github_url(url)
        if parsed is None:
            return False
        parts, fragment = parsed
        suffix = _repo_prefix(parts, repo)
        if suffix is None:
            return False
        if (
            len(suffix) == 5 and suffix[:2] == ["actions", "runs"]
            and suffix[2].isdigit() and suffix[3] == "job" and suffix[4].isdigit()
        ):
            action_run_id, job_id = int(suffix[2]), int(suffix[4])
            job = self._json(
                ["gh", "api", "--hostname", "github.com", f"repos/{repo}/actions/jobs/{job_id}"],
                f"{repo}: Actions job evidence",
            )
            if not isinstance(job, dict) or (
                type(job.get("id")) is not int or job.get("id") != job_id
                or type(job.get("run_id")) is not int or job.get("run_id") != action_run_id
                or job.get("status") != "completed" or job.get("conclusion") != "success"
            ):
                return False
            run = self._json(
                ["gh", "api", "--hostname", "github.com", f"repos/{repo}/actions/runs/{action_run_id}"],
                f"{repo}: Actions run evidence",
            )
            return bool(
                isinstance(run, dict)
                and type(run.get("id")) is int and run.get("id") == action_run_id
                and self._repo_identity(run, repo)
                and run.get("status") == "completed" and run.get("conclusion") == "success"
                and isinstance(head_sha, str) and run.get("head_sha") == head_sha
            )
        if kind == "review" and pr_number is not None:
            match = re.fullmatch(r"pullrequestreview-(\d+)", fragment)
            if suffix != ["pull", str(pr_number)] or match is None:
                return False
            review_id = int(match.group(1))
            review = self._json(
                ["gh", "api", "--hostname", "github.com", f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}"],
                f"{repo}: pull request review evidence",
            )
            viewer = self._json(
                ["gh", "api", "--hostname", "github.com", "user"],
                "authenticated GitHub identity",
            )
            expected_pull = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
            author = review.get("user") if isinstance(review, dict) else None
            review_state = review.get("state") if isinstance(review, dict) else None
            review_body = review.get("body") if isinstance(review, dict) else None
            commented_ready = bool(
                review_state == "COMMENTED"
                and isinstance(review_body, str)
                and _meaningful_summary(review_body)
                and _REVIEW_BLOCKER.search(review_body) is None
            )
            return bool(
                isinstance(review, dict)
                and isinstance(viewer, dict)
                and type(review.get("id")) is int and review.get("id") == review_id
                and review.get("html_url") == url
                and review.get("pull_request_url") == expected_pull
                and review.get("commit_id") == head_sha
                and (review_state == "APPROVED" or commented_ready)
                and isinstance(review.get("submitted_at"), str)
                and bool(review["submitted_at"].strip())
                and isinstance(author, dict) and isinstance(author.get("login"), str)
                and isinstance(viewer.get("login"), str)
                and author["login"].casefold() == viewer["login"].casefold()
            )
        if kind == "verification" and pr_number is not None:
            match = re.fullmatch(r"issuecomment-(\d+)", fragment)
            if suffix != ["pull", str(pr_number)] or match is None:
                return False
            comment_id = int(match.group(1))
            comment = self._json(
                ["gh", "api", "--hostname", "github.com", f"repos/{repo}/issues/comments/{comment_id}"],
                f"{repo}: verification comment evidence",
            )
            viewer = self._json(
                ["gh", "api", "--hostname", "github.com", "user"],
                "authenticated GitHub identity",
            )
            expected_issue = f"https://api.github.com/repos/{repo}/issues/{pr_number}"
            if not isinstance(comment, dict) or not isinstance(viewer, dict):
                return False
            author = comment.get("user")
            body = comment.get("body")
            normalized = " ".join(body.split()) if isinstance(body, str) else ""
            without_sha = normalized.replace(head_sha or "", "")
            return bool(
                type(comment.get("id")) is int and comment.get("id") == comment_id
                and isinstance(comment.get("html_url"), str)
                and comment["html_url"].casefold() == url.casefold()
                and isinstance(comment.get("issue_url"), str)
                and comment["issue_url"].casefold() == expected_issue.casefold()
                and isinstance(author, dict) and isinstance(author.get("login"), str)
                and isinstance(viewer.get("login"), str)
                and author["login"].casefold() == viewer["login"].casefold()
                and isinstance(head_sha, str) and head_sha in normalized
                and len(without_sha.strip()) >= 20
                and re.search(
                    r"\b(?:test|tests|tested|verify|verified|verification|suite|build|lint|pass|passed)\b",
                    without_sha, re.I,
                ) is not None
            )
        return False

    def resolve_source(self, candidate: Candidate) -> ResolvedSource:
        state = candidate.state.casefold()
        if candidate.role is Role.PM or (candidate.role is Role.DEV and state == "todo"):
            repository = self._json(
                ["gh", "api", "--hostname", "github.com", f"repos/{candidate.repo}"],
                candidate.repo,
            )
            if not isinstance(repository, dict) or not isinstance(
                repository.get("default_branch"), str
            ):
                raise GitHubError(f"{candidate.repo}: malformed default branch")
            branch = repository["default_branch"]
            if not is_valid_branch_name(branch):
                raise GitHubError(f"{candidate.repo}: malformed default branch")
            reference = self._json(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    f"repos/{candidate.repo}/git/ref/heads/{quote(branch, safe='')}",
                ],
                candidate.repo,
            )
            sha = (
                reference.get("object", {}).get("sha")
                if isinstance(reference, dict)
                and isinstance(reference.get("object"), dict)
                else None
            )
            if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None:
                raise GitHubError(f"{candidate.repo}: malformed default branch head")
            return ResolvedSource("default", sha, branch, None, None)

        if (candidate.role is Role.QA and state in {"qa ready", "qa in progress"}) or (
            candidate.role is Role.DEV and state in {"feedback", "in progress"}
        ):
            owner, name = candidate.repo.split("/", 1)
            query = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){issue(number:$number){
    closedByPullRequestsReferences(first:10){
      nodes{number state headRefName headRefOid headRepository{nameWithOwner}}
      pageInfo{hasNextPage}
    }
  }}
}""".strip()
            payload = self._json(
                [
                    "gh", "api", "--hostname", "github.com", "graphql",
                    "-f", f"query={query}",
                    "-F", f"owner={owner}", "-F", f"name={name}",
                    "-F", f"number={candidate.number}",
                ],
                f"{candidate.repo}#{candidate.number}",
            )
            try:
                connection = payload["data"]["repository"]["issue"][
                    "closedByPullRequestsReferences"
                ]
                nodes = connection["nodes"]
                page_info = connection["pageInfo"]
                has_more = page_info["hasNextPage"]
                if (
                    not isinstance(connection, dict)
                    or not isinstance(nodes, list)
                    or not isinstance(page_info, dict)
                    or type(has_more) is not bool
                ):
                    raise TypeError("invalid closing PR connection types")
            except (KeyError, TypeError) as exc:
                raise GitHubError(
                    f"{candidate.repo}#{candidate.number}: malformed closing PR relation"
                ) from exc
            open_prs = [node for node in nodes if isinstance(node, dict) and node.get("state") == "OPEN"]
            if has_more or len(open_prs) != 1:
                raise GitHubError(
                    f"{candidate.repo}#{candidate.number}: expected exactly one open closing PR"
                )
            pr = open_prs[0]
            sha = pr.get("headRefOid")
            branch = pr.get("headRefName")
            head_repo = pr.get("headRepository")
            pr_number = pr.get("number")
            valid_branch = is_valid_branch_name(branch)
            if (
                not isinstance(sha, str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None
                or not valid_branch
                or type(pr_number) is not int
                or not isinstance(head_repo, dict)
                or str(head_repo.get("nameWithOwner", "")).casefold()
                != candidate.repo.casefold()
            ):
                raise GitHubError(
                    f"{candidate.repo}#{candidate.number}: malformed closing PR head"
                )
            return ResolvedSource("pr", sha, None, branch, pr_number)

        raise GitHubError(
            f"{candidate.repo}#{candidate.number}: source resolution is unsupported for state {state}"
        )

    def _verify_issue_handoff(
        self,
        candidate: Candidate,
        run_id: str,
        permitted_states: set[str],
        *,
        role: Role,
        pr_number: int | None,
        head_sha: str | None,
    ) -> bool:
        payload = self._json(
            [
                "gh", "issue", "view", str(candidate.number), "--repo",
                f"github.com/{candidate.repo}",
                "--json", "state,labels,comments",
            ],
            f"{candidate.repo}#{candidate.number}",
        )
        if not isinstance(payload, dict) or payload.get("state") != "OPEN":
            return False
        labels = payload.get("labels")
        comments = payload.get("comments")
        if not isinstance(labels, list) or not isinstance(comments, list):
            return False
        workflow = [
            str(label.get("name", "")).casefold()
            for label in labels
            if isinstance(label, dict)
            and str(label.get("name", "")).casefold() in _WORKFLOW_STATES
        ]
        if len(workflow) != 1 or workflow[0] not in permitted_states:
            return False
        return _validated_handoff(
            comments,
            run_id=run_id,
            role=role,
            final_state=workflow[0],
            issue=candidate.number,
            pr_number=pr_number,
            head_sha=head_sha,
            repo=candidate.repo,
            screenshot_checker=self._screenshot_checker,
            evidence_checker=self._evidence_readback,
        )

    def verify_pm_handoff(self, candidate: Candidate, run_id: str) -> bool:
        return self._verify_issue_handoff(
            candidate, run_id, {"plan approval", "need confirmation"},
            role=Role.PM, pr_number=None, head_sha=None,
        )

    def verify_dev_handoff(
        self,
        candidate: Candidate,
        remote_branch: str | None,
        sha: str,
        run_id: str,
        *,
        has_changes: bool = True,
    ) -> bool:
        if type(has_changes) is not bool:
            return False
        if not has_changes:
            return self._verify_issue_handoff(
                candidate, run_id, {"need confirmation"}, role=Role.DEV,
                pr_number=None, head_sha=None,
            )
        if remote_branch is None:
            return False
        closing_candidate = type(
            "ClosingCandidate",
            (),
            {
                "repo": candidate.repo,
                "role": Role.QA,
                "state": "qa ready",
                "number": candidate.number,
            },
        )()
        source = self.resolve_source(closing_candidate)  # type: ignore[arg-type]
        if source.sha != sha or source.remote_branch != remote_branch:
            return False
        return self._verify_issue_handoff(
            candidate, run_id, {"qa ready", "need confirmation"}, role=Role.DEV,
            pr_number=source.pr_number, head_sha=sha,
        )

    def verify_qa_handoff(
        self, candidate: Candidate, expected_source: ResolvedSource, run_id: str
    ) -> bool:
        actual = self.resolve_source(candidate)
        if (
            actual.kind,
            actual.sha,
            actual.default_branch,
            actual.remote_branch,
            actual.pr_number,
        ) != (
            expected_source.kind,
            expected_source.sha,
            expected_source.default_branch,
            expected_source.remote_branch,
            expected_source.pr_number,
        ):
            return False
        return self._verify_issue_handoff(
            candidate, run_id, {"review ready", "feedback", "need confirmation"},
            role=Role.QA, pr_number=expected_source.pr_number,
            head_sha=expected_source.sha,
        )

    def file_at_revision(self, repo: str, path: str, sha: str) -> bytes:
        payload = self._json(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                f"repos/{repo}/contents/{quote(path, safe='/')}",
                "--method",
                "GET",
                "-f",
                f"ref={sha}",
            ],
            repo,
        )
        if (
            not isinstance(payload, dict)
            or payload.get("encoding") != "base64"
            or not isinstance(payload.get("content"), str)
        ):
            raise GitHubError(f"{repo}: malformed contract response")
        try:
            encoded = "".join(payload["content"].splitlines())
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GitHubError(f"{repo}: malformed contract content") from exc

    def scan(self, repositories: Iterable[RepositoryConfig]) -> list[Candidate]:
        candidates: list[Candidate] = []
        eligible = dict(_STATE_ROLES)
        for repo in repositories:
            if not repo.enabled:
                continue
            argv = [
                "gh",
                "issue",
                "list",
                "--repo",
                f"github.com/{repo.slug}",
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,labels,createdAt,updatedAt",
            ]
            result = self._runner(argv)
            if result.returncode != 0:
                detail = redact_text(result.stderr.strip()) or f"exit {result.returncode}"
                raise GitHubError(f"{repo.slug}: {detail}")
            try:
                rows = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise GitHubError(f"{repo.slug}: invalid JSON") from exc
            if not isinstance(rows, list):
                raise GitHubError(f"{repo.slug}: expected issue list")

            for row in rows:
                if not isinstance(row, dict):
                    raise GitHubError(f"{repo.slug}: invalid issue row")
                labels = row.get("labels", [])
                label_names = [
                    str(label.get("name", ""))
                    for label in labels
                    if isinstance(label, dict)
                ]
                state_labels = [
                    name.casefold() for name in label_names if name.casefold() in _WORKFLOW_STATES
                ]
                if len(state_labels) != 1:
                    continue
                state = state_labels[0]
                role = eligible.get(state)
                if role is None or role not in repo.roles:
                    continue
                priorities = [
                    name for name in label_names if name.casefold() in _PRIORITY_NAMES
                ]
                if len(priorities) > 1:
                    continue
                priority = priorities[0] if priorities else "missing"
                try:
                    number = int(row["number"])
                    title = str(row["title"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise GitHubError(f"{repo.slug}: malformed issue") from exc
                candidates.append(
                    Candidate(
                        repo=repo.slug,
                        repo_path=repo.path,
                        number=number,
                        title=title,
                        role=role,
                        state=state,
                        priority=priority,
                        state_entered_at=_parse_time(row.get("updatedAt")),
                    )
                )
        return deduplicate_and_sort(candidates)
