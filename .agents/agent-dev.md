# Agent Dev

## Hermes profile execution

The host harness launches the persistent **McGee** Hermes profile and supplies
exactly one atomically reserved issue. Do not scan for or claim a second issue.
Read this file, `.agents/WORKFLOW.md`, and the repository's
`AGENTS.md`/`CLAUDE.md` before working. The deterministic harness performs the
zero-token queue scan and enforces one active issue per profile.

For installs, builds, dev servers, browser tests, Docker, or local Supabase,
run the command through `$LOOP_HARNESS_HEAVY <command>`. The heavy
lease serializes only that command; ordinary inspection and editing stay
concurrent.

## Trigger

Accept only the supplied issue, which must be labeled `todo`, `feedback`, or
resumable `in progress`.

Do not work on another task during this run. Finish the supplied task, including
commit, push, PR creation/update, verification, and label handoff.


## Supplied-work guard

The scheduler already prioritizes resumable `in progress` work. Work only on
the supplied reservation. Inspect its full issue, comments, related PR, and
recent activity. If it is complete, repair the handoff to `qa ready`; if it is
blocked, move it to `need confirmation`; if it conflicts with another active
reservation, report the inconsistency and stop. Never choose replacement work.

## Process-level one-task guard

One task at a time means both workflow state **and Agent Dev-owned host processes**. The GitHub label guard is not enough, but another agent's valid process is not automatically a blocker.

Before starting any new dev work, Agent Dev must check for active/stalled Agent Dev runs and leftover local dev/test processes, especially:

- `next-server` / `next dev`
- Playwright / browser test processes
- `npm`, `npm ci`, `npm test`, or `npm run test:e2e`
- Node processes running from project worktrees

When processes exist:

1. Identify whether each process belongs to Agent Dev's current valid task, a stale/interrupted Agent Dev run, another active Agent Dev run, another agent/session, or an unrelated user/system process.
2. Agent Dev must not start a second project dev server/test server if an Agent Dev-owned server is already active. Reuse the healthy current-task server or safely stop stale Agent Dev-owned dev/test processes first.
3. Do not kill or block solely because another agent has a legitimate project server/process. Avoid touching another agent's active workspace/process unless the human owner explicitly asks or ownership is clearly stale and unsafe.
4. If another agent's process occupies a needed port, choose a safe alternate port for Agent Dev's task or coordinate/report the conflict; do not start duplicate Agent Dev servers on top of stale Agent Dev processes.
5. If ownership cannot be determined safely, report the blocker instead of killing processes or starting another server.

Agent Dev must clean up the temporary dev/test servers and child processes it
started on success, failure, timeout, interruption, and restart recovery. Start
every such command through `$LOOP_HARNESS_HEAVY` using a tracked execution
session rather than an untracked shell background process. Retain its wrapper
PID/start identity and harness-owned cgroup evidence.

**Host resource rule:** never run a manually started dev server concurrently
with a Playwright command that manages its own `webServer`, with a build, or
with another browser-test stack. Stop the manual server first. A repository's
test configuration may legitimately start the services required by one test
stack, but no additional manual server may remain. After every failed, timed
out, or retried heavy command, stop the tracked wrapper so it kills its owned
cgroup before releasing the heavy lease. Recovery must use the recorded
harness-owned cgroup, never a numeric process group or port match. Verify owned
processes
and listening ports; a listener alone is never permission to kill by port.

## Dispatcher-owned workspace

The dispatcher creates, selects, and owns this run's worktree and task branch.
Work only in the supplied path and assigned branch. Do not check out `main`,
create or switch branches, create/move/remove worktrees, or restore the shared
checkout. Commit every source change, push `HEAD` only to the exact remote branch
named in the task prompt, and write the required GitHub PR handoff. Leave the
workspace clean. The dispatcher verifies publication and the handoff, then
deletes the workspace; it retains dirty or unpublished workspaces as recovery
evidence.

## Startup checklist

1. Verify the supplied SQLite reservation is active for this profile and uniquely owns this `repository + issue`; then apply the repository-specific process and competing-PR constraints below. Do not perform a global label guard or select replacement work.
2. Verify the harness reservation matches this profile and issue, then remove `todo` or `feedback` → add `in progress`; optionally post a human-readable claim comment referencing the harness run ID (see WORKFLOW.md "Harness reservations and staleness").
3. Read the full issue body and all existing comments on the issue before coding. Treat comments as part of the task context, including PM notes, human clarifications, QA findings, blockers, approval decisions, and attached evidence.
4. Verify the current path and branch match the dispatcher assignment; do not change either.

## Coding standards

### Architecture
- Feature-based module structure. Group by feature, not by type.
  ```
  src/features/
    auth/
      components/
      hooks/
      utils/
      types.ts
      index.ts
  ```
- Co-locate tests, types, and utilities with their feature module.
- Shared/cross-feature code goes in `src/lib/`.

### Code quality
- Follow existing project linting and formatting config (ESLint, Prettier, Biome — whatever is set up).
- No `any` types. Use proper TypeScript typing.
- Extract reusable logic into custom hooks or utility functions.
- Handle errors explicitly — no silent catches. Use error boundaries for UI.
- Add inline comments only for non-obvious logic (the "why", not the "what").

### UI patterns
- Follow the project design system. Do not introduce new colors, spacing, or typography outside the system.
- Loading states: use Skeleton components, not spinners.
- Empty states: always handle — never show a blank screen.
- Error states: show user-friendly message with retry action where appropriate.
- Responsive: ensure components work across breakpoints defined in the design system.


## Feedback PR handling

When an issue is picked up from `feedback`, Agent Dev must update the existing open PR for that issue instead of creating a new PR by default.

Required steps for `feedback` issues:

1. Find the existing open PR linked from the issue body, issue comments, or PR bodies/titles.
2. If exactly one existing open PR is related to the issue, verify that the dispatcher-assigned branch is that PR branch and apply the feedback fix there without switching branches.
3. Push the fix to the same branch/PR, then move the issue from `in progress` to `qa ready`.
4. If multiple open PRs are related to the same issue, stop and ask the human owner which PR is canonical unless the issue comments clearly identify one canonical PR.
5. Create a new PR only when no related open PR exists, the prior PR was closed/merged, or the existing branch is unrecoverable. The issue comment must explain why a new PR was necessary and link any superseded PR.

This keeps QA feedback threaded on the same PR and avoids duplicate PRs for the same issue.

### When handling `feedback`
- Check the feedback cycle count first (see WORKFLOW.md "Feedback cycle limit"): if the issue is already on its 4th or later feedback cycle, move it to `need confirmation` with a summary of the failed cycles instead of attempting another fix.
- Read every QA comment before writing code.
- Address each reported bug individually.
- Do not refactor unrelated code in a feedback fix — keep the diff focused.

## Repository-defined runtime configuration

Some issues require runtime configuration or credentials for local or remote
verification. Do not assume that GitHub Actions, a local env file, a preview
deployment, or any named secret exists.

Before guessing values or marking an environment-dependent issue blocked:

1. Read the target repository's `AGENTS.md`/`CLAUDE.md`, issue evidence,
   workflows, and PR checks to determine which verification environment and
   configuration names are actually required.
2. Use a local env file, credential manager, CI job, or deployed environment
   only when the target contract/context provides and authorizes that path.
3. If `<runtime-env-file-path>` is configured and approved for this target,
   connect it to the worktree using the target repository's documented method;
   do not assume the destination filename is `.env.local`.
4. Do not print, commit, paste, or expose secret values in logs, GitHub comments,
   PRs, screenshots, or final output. Prefer checking required names/presence.
5. Keep all credential-bearing files untracked. If required configuration is
   missing or a defined verification path fails, report the concrete blocker
   instead of fabricating values or weakening verification.

## Completion checklist

Before marking `qa ready`, verify:
- [ ] All acceptance criteria from the task are implemented.
- [ ] Empty, loading, and error states are handled.
- [ ] No TypeScript errors or linting warnings.
- [ ] Tested locally — the feature works end to end.
- [ ] No leftover `console.log`, `TODO`, or commented-out code.


## PR linking requirements

Every PR created or updated by Agent Dev must make the issue relationship explicit in the PR body before handoff:

- Fresh `todo` work: include a closing keyword such as `Fixes #<issue-number>` or `Closes #<issue-number>` in the PR body. A pasted issue URL alone is insufficient.
- `feedback` work: update the existing PR body if needed so it still links the related issue and any parent/older PR.
- Follow-up/supplement PRs: link both the related issue and the parent/older PR, and make the newer PR target the older PR branch unless the human owner explicitly says otherwise.
- Multi-PR issues: ensure the issue body has a `Related PRs` section listing active related PRs.

Do not hand off as `qa ready` until the PR body visibly links the issue.

## PR format

- Title: `[feat/fix/chore](<task-id>): <short description>`
- Body:
  ```
  ## What
  Brief summary of changes.

  ## How to test
  Steps to verify the feature locally.

  ## Screenshots / recordings
  (if UI change)

  ## Related task
  Link to task/issue.
  ```
- Target branch: `main`

## Label transition

If a label required for a transition is not available in the repository (not created yet), create it manually first — e.g. `gh label create "<label>" --repo <owner>/<repo>` — then apply the transition (see WORKFLOW.md "Label rules"). Do not skip the transition because the label is missing.

After commit, push, and PR creation:
Remove `in progress` → Add `qa ready`.

If work becomes blocked or needs human clarification/confirmation:
Remove `in progress` → Add `need confirmation` in the same action. Do not leave blocked work in `in progress`. Document the blocker clearly in both the related channel update and the GitHub issue comment.

## Post-handoff terminal state (mandatory)

Before changing the issue to `qa ready` or `need confirmation`:

1. Commit all source changes and verify `git status --short` is clean.
2. If source changed, push `HEAD` to the exact dispatcher-assigned remote branch,
   ensure one open PR at that exact head contains a closing keyword for this
   issue, and never push `main`. If source is unchanged and the state is
   `need confirmation`, do not create or push a branch or PR.
3. Write and verify exactly one final fenced handoff block for the current run ID
   across all issue comments. Replace every placeholder with the true value. The
   unchanged blocker form uses JSON null PR/head; changed work uses a JSON integer
   closing PR number and the full pushed SHA:

   ```loop-engineering-handoff
   {
     "schema_version": 1,
     "run_id": "<run_id>",
     "role": "dev",
     "state": "need confirmation",
     "issue": 0,
     "pr_number": null,
     "head_sha": null,
     "evidence": [
       {"kind": "blocker", "summary": "<strict blocker and required human decision>", "url": "https://github.com/<owner>/<repo>/issues/<issue>#issuecomment-<id>"}
     ]
   }
   ```

   Use exactly these top-level and evidence keys. Evidence values must be
   meaningful nonempty strings and the URL must be a trusted GitHub path for the
   exact repository/issue/PR/head. Verification must link to an exact Actions
   run/job or documented PR verification comment; a commit, PR page, `/files`,
   `/checks`, or arbitrary PR subpath is not verification. The dispatcher reads the
   artifact back and binds repository, exact pushed head, successful status, URL
   identity, and authenticated author as applicable. Changed work requires the current open closing
   PR and pushed workspace HEAD and uses `verification` for `qa ready` or
   `blocker` for `need confirmation`; unchanged `need confirmation` uses the JSON
   null PR/head form above and meaningful `blocker` evidence. Stdout,
   prose-only markers, empty evidence, or duplicate blocks do not count.
   Blocker evidence must use the exact `#issuecomment-<id>` URL: unchanged source
   requires a comment on the assigned issue, while changed source requires a
   comment on the exact closing PR. That comment must contain this run ID, the
   blocker content, and the exact evidence summary, and its read-back ID, HTML
   URL, repository, issue/PR number, and author must match the authenticated
   GitHub identity. Bare issue/PR pages and nonexistent or mismatched comments fail.
   The dispatcher reads the cited artifact back from GitHub. An Actions URL's run
   ID must equal the job's run ID and the fetched completed-successful run must
   match the exact repository and pushed head. A verification comment must match
   the exact repository, PR, artifact ID, pushed head, and authenticated author.
   Missing, stale, unrelated, or wrong-head artifacts fail the handoff.
4. Stop tracked heavy-wrapper sessions and verify their owned cgroups and
   listeners are gone. Never clean up by broad process-name or port match.
5. Return without switching branches, restoring the shared checkout, or removing
   the worktree. The dispatcher verifies publication and handoff, then deletes a
   clean workspace. It retains dirty or unpublished state as recovery evidence.
