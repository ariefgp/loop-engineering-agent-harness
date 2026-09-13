# Agent Workflow

## State machine

```
to be planned     → [Agent PM]  → plan approval | need confirmation
need confirmation → [Human]     → to be planned (re-plan after answer)
plan approval     → [Human]     → todo
todo              → [Agent Dev] → in progress → qa ready | need confirmation
qa ready          → [Agent QA]  → qa in progress
qa in progress    → [Agent QA]  → review ready → done | feedback | need confirmation
feedback          → [Agent Dev] → in progress → qa ready | need confirmation
```

## Label rules

- A task must have exactly ONE state label at any time.
- If a required label is not available in the repository (not created yet), create it manually before applying it — e.g. `gh label create "<label>" --repo <owner>/<repo> --description "<state description>"` — then proceed with the normal transition. Never skip a label transition, fail silently, or leave an issue in the wrong state because a label is missing.
- When adding a new state label, ALWAYS remove the previous state label in the same action.
- Never leave a task with zero state labels or two state labels.
- If work becomes blocked or needs human clarification/confirmation, do not leave the task `in progress` or `qa in progress`. Remove the active in-progress label and add `need confirmation` in the same action, then document the blocker clearly in the related channel and GitHub issue comment.


## Resume priority and supplied work

The scheduler, not the profile, chooses work. It prioritizes resumable
`in progress` / `qa in progress` reservations before new `todo`, `feedback`, or
`qa ready` work. Each profile receives one issue and must not scan for or choose
a replacement. If the supplied issue is already complete, conflicts with a
different active owner, or is no longer in the expected state, report the
inconsistency and stop without taking other work.

When QA starts a supplied `qa ready` issue, it removes `qa ready` and adds
`qa in progress` before detailed review. After QA, it replaces `qa in progress`
with `review ready`, `feedback`, or `need confirmation` in one verified action.

## Harness reservations and staleness

GitHub labels remain lifecycle truth. The single-host harness SQLite ledger is
the authority for concurrent ownership: one active reservation per
`repository + issue` and one per profile. Claim comments are useful audit text,
but they are not a compare-and-swap lock.

The dispatcher holds an OS `flock` only while it refreshes reservations,
selects work, and commits reservations. It releases that lock before any Hermes
profile starts. A run heartbeat is persisted while the profile is active.

A reservation is reclaimable only when BOTH are true:

1. its harness heartbeat is at least **45 minutes** old; and
2. no matching local process identity (PID plus Linux process start token) is alive.

A dead process with a fresh heartbeat is not stolen early. PID alone is not
proof of liveness. This v2 protocol is single-host; peer/A2A workers must not
claim or transition issues until ownership uses a transactional shared lease.

## Three developer agents (McGee + Torres + Kate)

The harness provides three independent Dev lanes:

- **McGee** — `agent-dev.md`
- **Torres** — `agent-dev-torres.md`
- **Kate** — `agent-dev-kate.md`

Each profile receives at most one issue. The same `repository + issue` cannot
be reserved twice. Resume work sorts ahead of new work, then priority, oldest
GitHub issue `updatedAt` value (the v2 fallback age), repository, and issue
number determine order. `updatedAt` is not an exact state-entry timestamp:
comments and unrelated issue edits can refresh it. Every Dev uses a unique
branch/worktree and verifies that no competing PR exists.

## Two QA agents (Jimmy + Ducky)

The harness provides two independent QA lanes. They follow the same reservation
rules, test the exact PR head, use distinct worktrees, and publish independent
evidence before changing lifecycle state.

All six profiles may perform lightweight work concurrently. Installs, builds,
dev servers, browser tests, Playwright, Docker, and local Supabase must run via
`$LOOP_HARNESS_HEAVY <command>` so only heavy host work is serialized.

## Feedback cycle limit (circuit breaker)

The dev↔QA loop must not ping-pong indefinitely on the same issue. A "feedback cycle" is one round trip: QA transitions the issue to `feedback` and Dev returns it to `qa ready`.

- Before transitioning an issue to `feedback`, Agent QA must count the issue's prior feedback cycles from the issue timeline/comments (prior QA `feedback` transitions or QA bug reports).
- If the issue has already completed **3 feedback cycles**, do not add `feedback` again. Remove the active state label and add `need confirmation` instead, with a comment summarizing what each cycle attempted, what keeps failing, and a recommendation for how the human should unblock it (re-scope, re-plan, split the issue, pair review, or accept as-is).
- Agent Dev applies the same check when picking up a `feedback` issue: if it is already on its 4th or later cycle, move it to `need confirmation` with the same summary instead of attempting another fix.

This caps the budget an issue can burn without human intervention and surfaces systematically failing work instead of letting it loop silently.

## Hermes-native execution model

The deterministic Python harness scans GitHub and launches persistent Hermes
profiles directly. No coding CLI subprocess is part of the harness path.

```text
python -m loop_harness tick
  → read-only GitHub scan
  → deterministic queue ordering
  → short local claim lock + SQLite reservations
  → gibbs/mcgee/torres/kate/jimmy/ducky profile processes
  → concurrent lightweight work
  → one shared heavy-command lease
  → atomic result summaries for reconciliation
```

The profile invocation is an argument vector, never a shell string:

```text
<profile> chat --query-file - --oneshot -Q --in <repo-path> --run-budget 2700
```

The bounded task envelope is sent on stdin. Each profile reads its own persistent
memory plus `.agents/WORKFLOW.md`, its role contract, and project context files.
The dispatcher never changes lifecycle labels; the selected profile performs
and verifies transitions under this contract.

### Pre-check and scheduling

`gh issue list` is executed before any profile starts. Empty queues use no model.
GitHub authentication/API failures fail the tick visibly and dispatch nothing.
Candidates are deduplicated by `repository + issue`, and invalid or competing
reservations are excluded before launch.

### Cron activation

Do not install the live ten-minute cron until shadow mode and a one-PM/one-Dev/
one-QA canary pass. The cron invokes the script directly in no-agent mode; it
does not create six independent polling crons.

### Peer, A2A, Bot Mode, and Telegram

GitHub is the engineering handoff and lifecycle authority. Local Bot Mode,
`hermes peer`, and A2A may carry consultation or status envelopes only. They
must not claim or transition issues in v2. Telegram is presentation-only:
Telegram does not deliver bot-authored messages to other bots, so agents must
never depend on observing another bot's group message.

### Portability and host configuration

These files always refer to exactly one repository: **the repository that contains this `.agents/` directory** (the "target repo"). Placeholders used throughout the files are supplied by the harness task envelope or target-repository context:

- `<owner>/<repo>` — the GitHub slug of the target repo.
- `<repo-path>` — the local checkout path of the target repo.
- `<notes-repo-local-path>` / `<notes-repo-url>` — optional companion notes repository for project context. Skip notes steps if not configured.
- `<runtime-env-file-path>` — optional local runtime env file for env-dependent tests. Report a blocker if required but not configured.

To reuse this workflow in another repo:
1. Byte-synchronize all seven contracts: `WORKFLOW.md`, `agent-pm.md`, `agent-dev.md`, `agent-dev-torres.md`, `agent-dev-kate.md`, `agent-qa.md`, and `agent-qa-ducky.md`. Shadow and live ticks must fail before reservation if an applicable target contract is missing or stale.
2. Supply concrete values for the placeholders above in harness configuration/task context, and keep project-specific conventions in the target repository's `AGENTS.md`/`CLAUDE.md`.
3. After the shadow → one-PM/one-Dev/one-QA canary succeeds, install one dispatcher cron using the wrapper; configure repositories in the harness rather than creating per-repository or per-profile polling crons.

## PR and issue linking

Every PR must explicitly link its related GitHub issue in the PR body. Use a full issue URL or GitHub closing/reference keyword such as `Fixes #<issue-number>`, `Closes #<issue-number>`, or `Related issue: <full URL of the issue in this repository>`.

If a PR is a follow-up, feedback fix, supplement, or stack on another PR, the PR body must also link the related parent/older PR. The related issue body should include a `Related PRs` section when multiple active PRs belong to the same issue.

Do not leave active PRs discoverable only by branch name, title, comments, or agent memory. The issue/PR relationship must be visible from GitHub.

## Playwright E2E test requirement

Every user story or issue spec MUST include a Playwright E2E test section. This is mandatory for all issues that touch the UI, including fixes, features, and chores.

- Agent PM must include a "Playwright E2E tests" section in every spec, listing the specific E2E test cases tied to the acceptance criteria.
- Agent Dev must implement the Playwright E2E tests listed in the spec alongside the feature/fix. Tests are part of the deliverable, not an afterthought.
- Agent QA must verify that the Playwright E2E tests pass as part of the QA review. If tests are missing or failing, the task goes back to `feedback`.
- If an issue is a pure backend/infra change with no UI surface, the spec must explicitly state "No E2E tests needed — backend-only change" with justification.

## Real data requirement

All agents must use real application data for development, testing, and QA across all pages. Static, hardcoded, mock, or fixture-only page data is not acceptable as proof that a page or feature works.

- Agent PM must write specs and acceptance criteria that expect real data-backed behavior, including empty/loading/error states when real records are absent.
- Agent Dev must implement pages and UI states against real data sources, not hardcoded demo/static records. If required records do not exist for the flow, seed the database using the project's approved seed/migration/factory path and document the seed data.
- Agent QA must verify pages against real application data from the database/API. If the needed data is unavailable, QA must seed the database before testing when safe and supported by the project. If seeding is not possible, QA must mark the test as blocked/feedback instead of passing with static data.
- Any seeded test data must be documented in the issue/PR QA notes, including what was seeded, how it was seeded, and how another agent can reproduce it.

### Repository-defined environments, CI, deployments, and credentials

Do not assume that a target uses a particular hosting provider, creates preview
deployments, runs E2E in CI, or exposes any credential or secret. Determine the
available and required verification paths from that target's `AGENTS.md`/
`CLAUDE.md`, issue acceptance criteria, repository workflows, and PR checks.

- Use a local, staging, preview, or CI environment only when the target contract
  or observable repository/PR context defines it and access is authorized.
- Treat CI as evidence only for the exact PR head and only for checks whose
  logs/artifacts prove the acceptance criterion. CI does not replace required
  rendered, interactive, negative-case, or real-data verification.
- Treat a preview deployment as evidence only after verifying its exact PR/head,
  configuration, data source, and accessibility. A successful deployment check
  alone is not functional proof.
- Use credentials only through the target's approved mechanism. Never infer
  secret availability from another repository, guess values, or expose values.
- If a required environment, check, deployment, or credential is absent or
  inaccessible, record the exact attempted evidence and move to `feedback` or
  `need confirmation` as appropriate; never downgrade or fabricate verification.

See `.agents/agent-qa.md` "Repository-defined remote verification" for the full
procedure.
- Do not mark an issue `review ready` based only on static screenshots, hardcoded page states, mocked fixture data, Storybook-only views, or local-only fake data unless the issue is explicitly scoped to that isolated fixture and the human owner approves that exception.

## Active ticket dependency check

Before finalizing any ticket or moving it to the next state, the owning agent must check whether the current ticket is dependent on, linked to, related to, duplicated by, or blocked by any other active GitHub issue or PR.

The check must include:

- Explicit links in the issue body and all comments.
- GitHub keywords and relationship language such as `blocked by`, `depends on`, `related to`, `duplicate of`, `parent`, `follow-up`, `supersedes`, and `part of`.
- Open issues and PRs with overlapping scope, shared screenshots/evidence, shared affected UI/components, or matching Figma/code references.
- Existing PRs or branches that already implement, partially implement, or conflict with the ticket.

If an active dependency, blocker, duplicate, conflict, or related ticket exists, document it in the ticket's `Dependencies` / related-ticket section and in the GitHub issue comment before transitioning labels. If the relationship blocks safe development or review, transition to `need confirmation` instead of the normal next state and ask the specific confirmation needed. If no active relationship exists, explicitly state `None found` in the Dependencies section or final comment.

### Derive before asking

The owning agent must not ask an open confirmation question until it has tried to derive the answer from all available project context:

- Related active GitHub issues and PRs.
- The current issue body, comments, screenshots, Figma links, and other evidence.
- Relevant source code and existing product patterns.
- The project notes repository context and brief (if configured — see "Project brief and notes alignment").

If another active issue or PR answers the question, document the answer as `Resolved from #<issue-or-pr>: <answer>` instead of asking the human again.

If another active issue defines the parent context, entry point, shared component, or upstream behavior but is not approved or delivered yet, document it as `Dependency: #<issue-or-pr>` instead of treating it as an unanswered product question.

Only ask the human when the answer cannot be determined from related issues, design evidence, code, project notes, or documented project decisions.

### Open-question recommendation requirement

When an agent must ask an open confirmation question, it should include its best current recommendation whenever it has enough context to form one. The question should separate the uncertainty from the recommendation, for example:

- `Question: <what needs confirmation>`
- `Recommendation: <recommended answer or option>`
- `Why: <short reason based on code, design evidence, related issues, or project notes>`

If the agent does not have enough evidence to recommend an answer safely, it must say `No recommendation yet` and explain what evidence is missing. Do not present guesses as recommendations.


## Project brief and notes alignment

If the project has a companion notes repository, its canonical project context and discovery notes are kept at:

- Local path: `<notes-repo-local-path>`
- Source: `<notes-repo-url>`

Before creating, planning, finalizing, developing, or reviewing a ticket, the owning agent must use these notes as project context and make sure the ticket remains aligned with the project brief. At minimum, read `README.md` and `context.md` when unfamiliar with the area, then inspect the most relevant files for the ticket, such as:

- `spec/` for canonical domain rules, glossary, states, permissions, notifications, handoffs, source of truth, and compliance lineage.
- Design review notes, design screen references, and story-to-screen maps for design/screen alignment.
- `user-stories/` and discovery documents for backlog intent and accepted user flows.

If the project has no notes repository configured, skip the notes steps and rely on the issue evidence, source code, and in-repo documentation.

If the ticket conflicts with the notes, is underspecified compared with the brief, or the agent is confused after checking the notes, do not guess. Ask the needed clarification directly in the GitHub ticket, record the relevant note/file reference, and use `need confirmation` when the ambiguity blocks safe work.



## Process ownership and dev server safety

One issue at a time must include process ownership, not only labels. Agents should avoid leaving behind dev/test servers, but one agent must not treat another agent's legitimate active server as an automatic blocker.

For project dev work:

- Before starting a dev server, E2E run, or long-running local process, check existing project dev-server, Playwright, npm, and worktree Node processes.
- Identify ownership where possible: current agent/current task, stale interrupted run, another active agent/session, or unrelated system/user process.
- Each agent is responsible for not running multiple project dev/test servers by itself. Reuse or safely stop its own stale processes before starting another.
- Do not kill or block solely because another agent has a legitimate active process. If another agent occupies a needed port, use a safe alternate port or report/coordinate.
- Always clean up dev/test processes started by the agent on success, failure, timeout, interruption, and restart recovery.

## Handoff protocol

Each agent only picks up tasks with its trigger label. Agents must work on only one issue at a time to preserve context. Do not pick up, plan, develop, review, or update multiple issues in the same run.

Before working on any issue, the agent must fully inspect the issue context and evidence before planning, developing, reviewing, or asking for confirmation:

- Read the full issue body.
- Read all existing comments on that issue.
- Inspect all attached or embedded screenshots/images.
- Open and check all Figma links, including linked nodes/frames when available.
- Check all other URLs, documents, videos, logs, and attachments referenced by the issue or comments.
- For GitHub `user-attachments` URLs, do not rely on unauthenticated fetch results. In private repos these URLs may return `404` without authentication even when the attachment is valid. Use authenticated GitHub access/the configured GitHub token before treating an attachment as unavailable.
- Treat evidence as part of the spec, not optional context.
- Only ask for confirmation after the available evidence has been inspected, or when access limits, missing permissions, expired links, API/rate limits, or unavailable attachments genuinely block inspection.

### Mandatory code inspection

Before asking any confirmation question or writing a spec, the agent MUST inspect the relevant source code in the repository. This applies to all agents, especially Agent PM during planning.

- Identify the files, components, and functions related to the issue (e.g., if the issue is about a button in the Portfolio filter bar, read the Portfolio view component, the button component, and any shared UI components involved).
- Read the actual source code using authenticated GitHub API access or a local clone.
- Derive answers from the code wherever possible: styling, layout, state management, data flow, component structure, existing patterns, and configuration.
- Only ask for confirmation when something genuinely cannot be determined from the code and the available evidence. Do not ask questions that the code already answers.
- Include a "Code reference" section in the PM spec citing the specific files, line patterns, and current behavior observed in the source.
- If the issue references a Figma design, attempt to fetch and compare the Figma design against the current code before asking what doesn't match.

Comments may contain human clarifications, prior agent summaries, QA findings, blockers, or approval decisions, and must be treated as part of the task context.

After completing work, the agent transitions the label to hand off to the next agent. No agent should work on a task outside its trigger labels.

After an agent completes any action, including the initial QA transition from `qa ready` to `qa in progress`, the agent must update or notify the related channel with a concise summary of what changed, the resulting state label, and any blocker or confirmation needed.

After each action on an issue, including the initial QA transition from `qa ready` to `qa in progress`, the agent must also add a new comment on the related GitHub issue with a concise summary of what changed, the resulting state label, and any blocker or confirmation needed. The comment must contain the actual rendered message body. Never post local file paths, temp file references, shell redirection tokens, or placeholder text such as `@/tmp/...`.

### QA screenshot evidence

Agent QA must include screenshots in every QA report, regardless of task type. For local-dev testing:
1. Start the repository-defined dev server through `$LOOP_HARNESS_HEAVY <command>` in a tracked background execution session (never with an untracked shell `&`), record the wrapper PID/process identity, and wait for the repository-defined readiness signal.
2. Navigate to relevant pages using the browser tool and capture screenshots
3. Upload every screenshot to the issue/PR as an inline-rendered GitHub image and visually verify that it renders in the comment
4. Reject local paths, bare URLs, ordinary links, and download-only release assets as QA evidence
5. If inline upload is blocked, move to `need confirmation` rather than `review ready`
6. Stop the tracked wrapper gracefully so it kills its harness-owned cgroup before releasing the heavy lease. Recovery cleanup must target that recorded cgroup, never a numeric process group or every listener on a port. Verify its processes and listeners are gone.

Screenshots are mandatory evidence — not optional. See `.agents/agent-qa.md` for the full screenshot checklist and workflow.

| Agent     | Trigger labels   | Output labels                |
| --------- | ---------------- | ---------------------------- |
| Agent PM  | to be planned    | plan approval or need confirmation |
| Agent Dev | todo, feedback   | qa ready or need confirmation |
| Agent QA  | qa in progress (resume), qa ready (new) | review ready, feedback, or need confirmation |


## Local workspace cleanup at handoff

Local clones and worktrees are disposable once the canonical work is safe on the remote. **Cleanup happens at handoff** — the moment an issue is moved to `qa ready`, `need confirmation`, `review ready`, or `feedback`, the owning agent must clean up its local workspace in the same turn, not later.

Cleanup steps at handoff (`qa ready`, `need confirmation`, `review ready`, or `feedback`):

1. Verify `git status --short` is clean, or remaining files are only disposable build/cache output.
2. Verify the branch was pushed to the appropriate remote(s).
3. Verify the PR/MR or issue has the final useful context, screenshots, logs, notes, and verification evidence.
4. Remove the worktree: `git worktree remove <worktree-path>` (or `git worktree prune` if already deleted).
5. Delete bulky generated folders from the worktree before removal if not already done: `node_modules`, `.next`, build outputs, caches, temp dirs.
6. Verify no temporary dev/test server process was left behind by this run. Stop any `next-server`, Playwright, or npm child process started for verification.
7. Confirm disk space is reclaimed after cleanup.

Do not keep completed worktrees around "in case QA sends feedback." If feedback comes later, create a fresh worktree from the PR branch at that time.

Keep only the smallest canonical checkout (main branch) needed for the next run. Do not delete another agent's active workspace without checking its current session/cron status.

## Completion

After `review ready`, a human reviewer merges the PR and transitions the label to `done`.
