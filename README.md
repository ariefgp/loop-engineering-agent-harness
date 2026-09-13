# Loop Engineering Agent Harness

A Hermes-native, label-driven PM → Dev → QA workflow. A deterministic Python
scheduler scans GitHub, atomically reserves eligible work on one host, and
launches persistent Hermes profiles. GitHub issues, PRs, labels, and QA evidence
remain the engineering source of truth.

## Fleet

- PM: Gibbs (1 concurrent issue)
- Dev: McGee, Torres, Kate (3 concurrent issues)
- QA: Jimmy, Ducky (2 concurrent issues)

All six profiles may inspect, reason, edit, and communicate concurrently. Only
resource-heavy child commands are serialized.

## State machine

```text
to be planned → Gibbs → plan approval | need confirmation
plan approval → human approval → todo
todo/feedback → Dev → in progress → qa ready | need confirmation
qa ready → QA → qa in progress → review ready | feedback | need confirmation
review ready → human review/merge → done
```

Every issue must have exactly one workflow-state label.

## Architecture

```text
python -m loop_harness tick
  → read-only gh issue scans
  → dedupe + deterministic ordering
  → resolve immutable source OIDs and contracts
  → short fcntl claim lock
  → SQLite reservations (one active issue and profile)
  → dispatcher-created per-run Git worktrees
  → six Hermes profile subprocesses via argv + stdin
  → heartbeat + process identity
  → atomic redacted result summaries
```

The harness never invokes a separate coding-agent CLI. Profiles run as:

```text
<profile> chat --query-file - --oneshot -Q --in <repo> --run-budget 2700
```

Task instructions travel on stdin, not a shell command. `shell=True`, `eval`,
and interpolated commands are not used.

All production GitHub reads use a minimal environment allowlist that preserves
only GitHub authentication/configuration variables and basic locale/path values.
Ambient host selectors, proxies, and Git execution/configuration overrides are
discarded. Every `gh api` call pins `--hostname github.com`; repository commands
use `github.com/OWNER/REPO`.

### Dispatcher-owned worktrees

The registry checkout is control-plane state and is never a worker workspace. For
each selected run, the dispatcher resolves the immutable source revision, fetches
it into a private ref, and creates/selects a unique worktree before launching the
profile there. Profiles must not check out `main`, create, switch, move, or remove
worktrees or branches, or restore the shared checkout.

Dev receives a dispatcher-selected task branch, commits and pushes all source
changes to that exact assigned remote branch, and writes the required GitHub PR
handoff. The dispatcher verifies the clean local head, remote branch, and handoff
before deleting the worktree and private refs. QA runs detached at the exact PR
head, never pushes source changes, and leaves cleanup to the dispatcher. PM and QA
must leave the assigned source unchanged and clean.

Every Dev PR must contain a GitHub closing keyword for the exact issue (for
example, `Fixes #123`). A pasted issue URL alone is not a closing relationship.

The dispatcher deletes clean terminal workspaces only after verification. Dirty
or unpublished workspaces are retained and reported as recovery evidence; they
must not be erased merely to make cleanup appear successful. Deterministic
workspace intent is persisted with the reservation before preparation starts, so
an interrupted or partially prepared run remains discoverable. A pending cleanup
or cleanup-result persistence failure makes the originating tick fail while the
terminal work outcome remains unchanged; the journal drives idempotent cleanup
and result reconciliation on a later tick.

For Dev runs, `strace` is a mandatory pre-launch prerequisite. A trusted parent
traces successful writes/renames for only the assigned branch ref and reflog into
a dispatcher-owned journal outside the worker's Landlock write roots. The audit
file is a separate inode, not a hard link or polling observer, so truncating,
replacing, expiring, or pruning worker Git metadata cannot erase recorded ref
updates. The worker shares the host UID but remains unable to write the audit
tree because Landlock is inherited only by the tracee, not the trusted tracer.
This boundary assumes the dispatcher and kernel/strace are trusted; an unavailable
tracer or confinement prerequisite fails before Dev launch.

### Machine-readable handoff

A successful run must leave exactly one fenced handoff block for its run ID
across all comments on the issue. Replace placeholders with true values; do not add or omit JSON
keys:

```loop-engineering-handoff
{
  "schema_version": 1,
  "run_id": "<run_id>",
  "role": "<pm|dev|qa>",
  "state": "<terminal-state>",
  "issue": 0,
  "pr_number": null,
  "head_sha": null,
  "evidence": [
    {"kind": "<kind>", "summary": "<substantive result>", "url": "https://github.com/<durable-evidence>"}
  ]
}
```

The parser requires that exact top-level key set and exact evidence-item key set.
`schema_version` is integer `1`; `issue` is the supplied issue number; `role` and
`state` must match the run and sole final workflow label. PM uses JSON `null` for
`pr_number` and `head_sha` and requires `plan`, or `blocker` for `need confirmation`.
Dev normally uses the current open closing PR number and full pushed head SHA and
requires `verification`; `need confirmation` requires `blocker`. An unchanged Dev
source may use JSON `null` for PR/head and publish no branch, but any source change
requires the exact assigned branch to be pushed and linked by an open closing PR. QA uses
the assigned closing PR number and exact full head SHA and requires all three
kinds: `test`, `review`, and `screenshot`. Every value in an evidence item must be
a nonempty string, every URL must be a trusted `https://github.com` URL bound to
the exact repository/issue/PR/head, and every QA screenshot must use a non-placeholder
GitHub user-attachment UUID URL that is reachable without credentials and
returns verified PNG, JPEG, GIF, or WebP image content. Release-download assets
are not accepted as inline screenshot evidence. Redirects are accepted only
while they remain on allowlisted GitHub-owned image-delivery hosts. The exact
screenshot URL must also appear as an inline Markdown
image in the same comment. A prose run
marker, stdout, an empty evidence array, or duplicate fenced blocks is not proof.
Dev `verification` must link an exact `actions/runs/<run>/job/<job>` result or an
exact PR `#issuecomment-<id>` documenting verification. QA `test` must link an
exact Actions run/job, and QA `review` must link the exact PR
`#pullrequestreview-<id>` artifact. Generic PR pages, `/files`, `/checks`, and
arbitrary PR subpaths are rejected. The dispatcher reads every referenced artifact
back from GitHub and binds its repository, revision, author, status, and identity
to the handoff. PM plan/blocker evidence likewise requires an authenticated exact
issue comment containing the run identity and substantive plan/blocker content.
Dev blocker evidence requires an exact `#issuecomment-<id>` URL on the assigned
issue when source is unchanged or on the exact closing PR when source changed.
The host-pinned API read-back must match its ID, HTML URL, repository,
issue/PR number, authenticated author, run ID, blocker content, and evidence
summary. Bare issue/PR pages and nonexistent or mismatched comments are rejected.
QA review evidence accepts `APPROVED` or a substantive non-blocking `COMMENTED`
review only; blocking, dismissed, trivial, or unrelated reviews are rejected.

## Queue order

Candidates are deduplicated by `repository + issue` and sorted by:

1. resumable `in progress` / `qa in progress` before new work;
2. priority: urgent/P0, high/P1, medium/P2, low, missing;
3. oldest GitHub `updatedAt` value as the v2 fallback age;
4. repository slug;
5. issue number.

`updatedAt` is not an exact state-entry timestamp: comments and unrelated issue
edits can refresh it. A future version may derive state age from timeline events;
v2 uses this deterministic fallback without claiming otherwise.

GitHub API/authentication failures fail visibly and dispatch nothing.

## Local claim authority

Harness v2 deliberately supports one scheduling host. SQLite partial unique
indexes enforce one active reservation per profile and per repository issue.
An OS `flock` is held only while reclaiming stale reservations and committing
new ones; it is released before profiles run.

A reservation may be reclaimed only after 45 minutes without a harness
heartbeat and when its PID plus Linux process-start identity is not live. A
dead process with a fresh heartbeat is not stolen early.

Do not use peer or A2A workers to claim/transition issues until ownership moves
to a transactional shared lease service.

## Heavy commands

Profiles must wrap installs, builds, dev servers, browser tests, Playwright,
Docker, and local Supabase operations:

```bash
python3 -m loop_harness heavy -- npm run build
python3 -m loop_harness heavy -- npx playwright test
```

Launched profiles receive `LOOP_HARNESS_HEAVY` as an absolute wrapper path and
`LOOP_HARNESS_RUNTIME` as the exact runtime root. From a worker repository use:

```bash
$LOOP_HARNESS_HEAVY npm run build
```

The heavy lock covers only the child command. Other profiles continue
lightweight work. Each worker and heavy command runs inside a dedicated,
delegated cgroup v2 scope. A guardian remains outside that scope, retains the
heavy lease when applicable, and uses `cgroup.kill` before releasing ownership.
The target is placed in a subordinate user/PID namespace, preventing it from
addressing or signaling the guardians and dispatcher outside that namespace. A
stacked Landlock policy blocks filesystem mutations outside expected operational
roots and, specifically, grants cgroup membership writes only inside the owned
scope. Repository paths are resolved and used as each worker's current working
directory; candidate write roots that overlap or contain cgroupfs are discarded.
Landlock ABI v1 is used only as part of the cgroup-escape boundary, not claimed
as general file-integrity isolation. A target cannot move itself into the
writable parent hierarchy even if it remounts cgroupfs or enters further
namespaces; nested heavy wrappers create another PID namespace and a child scope
inside the worker-owned subtree. Descendants are therefore
contained even when they call `setsid()`. Empty nested cgroups are removed
bottom-up after termination, including guardian parent-death cleanup. The
harness fails closed when delegated cgroup-v2 kill support, unprivileged user
and PID namespaces, `unshare`, or Landlock filesystem confinement is
unavailable. SIGINT/SIGTERM and Linux parent-death
handling wake ordinary cleanup rather than performing blocking teardown in a
signal handler. Recorded PID/start identity is used for reservation liveness, while
termination targets the owned cgroup instead of a possibly reused numeric
process group. A listening port alone never proves ownership.

## Configuration

Repository membership and enabled/disabled status are defined only in
`config/repositories.json`. Enable a target only after its contracts and local
workspace have been audited.

Each entry supplies:

```json
{
  "slug": "Owner/repository",
  "path": "/absolute/local/path",
  "enabled": true,
  "roles": ["pm", "dev", "qa"]
}
```

Paths must exist. Slugs and paths must be unique.

## Commands

```bash
# Tests
make test

# Read-only queue plan; creates no runtime directory or SQLite state
make shadow
python3 -m loop_harness scan

# Live tick — do not run before canary approval
python3 -m loop_harness tick

# Compatibility wrapper
scripts/loop-engineering-dispatcher.sh --dry-run
```

Runtime state defaults to `/run/user/<uid>/loop-engineering/` and falls back to
`~/.hermes/loop-engineering/`. Directories use mode `0700`; database, locks,
and result summaries use private permissions. Results are schema-versioned,
written atomically, bounded, and credential-pattern redacted.

## Rollout

1. Run the complete unit/integration suite.
2. Run shadow mode across several ten-minute intervals.
3. Verify every proposed assignment manually.
4. Canary Gibbs on one small planning issue.
5. Canary McGee on one small approved issue.
6. Canary Jimmy on the resulting exact PR head.
7. Enable Torres, then Ducky, then Kate.
8. Only after the canary passes, install one ten-minute no-agent cron that runs
   the compatibility wrapper. Do not install six independent polling crons.

No live cron is installed by this repository change.

## Agent communication

GitHub is the durable engineering handoff. Other transports are optional:

- Bot Mode `message_agent`: local consultation between canonical Bot Chats
- `hermes peer`: another Hermes gateway
- A2A: standards-based cross-machine/framework task or status RPC
- Telegram: human-facing presentation only

Telegram does not deliver a bot-authored message to another bot. Never use a
Telegram group message as the event that causes another agent to work. Peer and
A2A envelopes must carry a run ID, idempotency key, sender, recipient, hop cap,
and expiry; they cannot claim or transition GitHub issues in harness v2.

## Contracts

The `.agents/` directory defines role behavior:

- `WORKFLOW.md`
- `agent-pm.md`
- `agent-dev.md`
- `agent-dev-torres.md`
- `agent-dev-kate.md`
- `agent-qa.md`
- `agent-qa-ducky.md`

Target repositories must receive the compatible contracts before their roles
are used. Shadow and live ticks compare each applicable contract byte-for-byte
against this repository and fail before reservation when a target is stale or
missing a lane contract. Project-specific rules remain in each repository's
`AGENTS.md` or `CLAUDE.md`.

## Safety boundaries

- No direct pushes to `main`
- No agent merges or production deployments
- One issue per profile
- One PR per issue unless the existing PR is unusable and documented
- Independent QA on the exact current PR head
- Three-cycle Dev ↔ QA feedback circuit breaker
- Lifecycle writes are performed and verified by the profile, not by the scanner
- Profile results are evidence for reconciliation, not proof of completion
