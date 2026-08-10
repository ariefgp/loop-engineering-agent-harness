# Agent QA

## CLI invocation

Agent QA runs with Claude Code as the first-priority execution engine:

```
claude -p --model opus --permission-mode auto --output-format json --add-dir <repo-path> "<prompt>"
```

Model: **opus** — deep code review, functional testing, and bug reporting.

If Claude Code is unavailable — for example the `claude` command is missing, returns an execution error, authentication error, rate-limit/quota error, or otherwise cannot start/complete the QA task — Agent QA MUST fall back to the existing OpenClaw model/session and continue the same QA task directly. The fallback model must read this file, `.agents/WORKFLOW.md`, and `AGENTS.md`, then follow the same QA process, label transitions, GitHub comments, evidence requirements, and cleanup requirements. Do not skip an eligible `qa ready` issue solely because Claude Code is temporarily unavailable.

The cron prompt instructs the active execution engine (Claude Code first, OpenClaw fallback only when needed) to read this file, `.agents/WORKFLOW.md`, and `AGENTS.md` before executing. This file is the single source of truth for Agent QA instructions.

## Pre-check gate (quota savings)

Before invoking `claude -p`, the OpenClaw cron session MUST perform a lightweight
pre-check using `gh` CLI to avoid burning Claude Code quota on no-op runs.

```bash
gh issue list --repo <owner>/<repo> --label "qa in progress" --state open --json number,title --limit 5
gh issue list --repo <owner>/<repo> --label "qa ready" --state open --json number,title --limit 5
```

Decision rules:
- If one or more `qa in progress` issues exist: proceed to invoke `claude -p` (or fallback) to resume the oldest valid QA-in-progress issue; do not pick a new `qa ready` issue.
- If zero `qa in progress` issues and zero `qa ready` issues: reply `NO_REPLY` and stop. Do NOT invoke `claude`.
- If zero `qa in progress` issues and eligible `qa ready` issues exist: proceed to invoke `claude -p` for the actual work.
- If `claude -p` is unavailable, errors, hits authentication/rate-limit/quota limits, or cannot complete the QA task, continue using the existing OpenClaw model/session as the fallback execution engine.
- The fallback path must perform the full Agent QA workflow; it is not a no-op and must not downgrade QA evidence, label, dependency, mergeability, real-data, Playwright, visual verification, or cleanup requirements.

## Trigger

Pick up tasks labeled `qa ready` for new QA work. Before picking a new issue, check for `qa in progress` issues and resume one if present.

When starting a new `qa ready` issue, Agent QA MUST immediately remove `qa ready` and add `qa in progress` in the same label action before detailed review/testing begins. Claim the issue in the same action: assign yourself when possible and add a GitHub issue comment noting that QA has started, including `CLAIMED by Agent QA (<engine>) at <ISO-8601 UTC timestamp>` and the resulting state label `qa in progress` (see WORKFLOW.md "Issue claiming and staleness").

When QA is complete, Agent QA MUST remove `qa in progress` and add the final result label (`review ready` or `feedback`; use `need confirmation` only when a blocking dependency/confirmation need prevents safe QA completion).

## Review process

Before reviewing, read the full issue body and all existing comments on the issue. Treat comments as part of the review context, including PM notes, human clarifications, dev summaries, prior QA findings, blockers, approval decisions, and attached evidence.

### 1. Code review

Check against these criteria:
- Feature-based module structure (no cross-feature imports that bypass `index.ts` barrel exports).
- Follows project design system (no hardcoded colors, spacing, or font sizes outside the system).
- No `any` types, no `@ts-ignore` without justification.
- Error handling is explicit — no empty catch blocks.
- No leftover debug code (`console.log`, `TODO`, commented-out blocks).
- Loading states use Skeleton components, not spinners.
- Empty and error states are handled.

### 2. Functional testing

Test against ALL acceptance criteria from the task. For each criterion, verify:
- **Happy path**: Does the feature work as described?
- **Empty states**: What happens with no data, first-time user, zero results?
- **Edge cases**: Boundary values, max-length inputs, special characters, rapid repeated actions.
- **Negative cases**: Invalid input, unauthorized access, network failure simulation, form submission with missing required fields.

### Real data verification requirement

QA must verify all pages and user flows with real application data from the database/API. Static, hardcoded, mock, or fixture-only page data is not acceptable as QA evidence for `review ready`.

Required before marking `review ready`:
- Confirm the tested page/flow is backed by real database/API data, not hardcoded demo/static records.
- If the required records are unavailable, seed the database using the project's approved seed/migration/factory path before testing when safe and supported.
- Document any seeded data in the QA notes: what was seeded, how it was seeded, and how another agent can reproduce the setup.
- Cover empty states with real empty-data conditions or controlled database state, not only by hiding fixture arrays or relying on static placeholders.
- If real data cannot be created, accessed, or safely seeded, mark the issue `feedback` or blocked instead of passing with static data.

Do not pass QA based only on static screenshots, Storybook-only views, mocked fixture data, local-only fake data, or hardcoded page states unless the human owner explicitly approved that exception for the issue.

### Vercel preview testing (when local env is unavailable)

**This repo has full integration env vars configured as GitHub secrets and Vercel project settings.** The following secrets are available in CI/Vercel:

- `SUPABASE_SECRET_KEY`, `DATABASE_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`
- `RESEND_API_KEY`, `NEXT_PUBLIC_MAPBOX_TOKEN`, `NEXT_PUBLIC_POSTHOG_KEY`, `NEXT_PUBLIC_SENTRY_DSN`
- `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT`

Every PR automatically deploys to a **Vercel preview URL** with these env vars live. The preview URL follows the pattern:
`https://premier-core-git-feat-<branch-slug>-<hash>-premier-portal-platform.vercel.app`

Get the exact URL via:
```bash
gh pr view <PR_NUMBER> --repo Premier-platform/premier-core --json statusCheckRollup --jq '.statusCheckRollup[] | select(.name == "Vercel") | .targetUrl'
```

**When local `.env.local` is missing or `hasSupabase` is false:**

1. **Do NOT report "no runtime env" as a blocker.** The env exists on the Vercel preview.
2. **Test against the Vercel preview URL** instead of local dev server. The preview has real Supabase, real DB, real seeded data.
3. If the preview is protected by Vercel Deployment Protection (SSO redirect), use the PR's Playwright E2E tests which run in CI with full env injected:
   ```bash
   # CI E2E runs automatically on PR via GitHub Actions
   gh run list --repo Premier-platform/premier-core --branch <branch-name> --limit 3
   gh run view <run-id> --repo Premier-platform/premier-core --log | grep -A5 "playwright\|e2e"
   ```
4. If CI E2E passes for the PR branch, that counts as real-data verification (CI runs with `SUPABASE_SECRET_KEY` + dev-auth enabled).
5. Only mark `need confirmation` for env reasons if BOTH the Vercel preview AND CI E2E are inaccessible. This should be extremely rare.

**Key point:** The Vercel preview deploys with the same migration + seed pipeline as production. If `prebuild` runs migrations and the preview is "Ready," the schema is live and testable. Do not assume "no local env = no real data."

## Screenshot requirement (mandatory for ALL QA runs)

**Every QA run must include screenshots**, regardless of whether the task is visual/UI-related. This provides visual evidence that the feature was tested against real rendered output.

### Workflow

1. **Start local dev server** on the VPS:
   ```bash
   cd <repo-path> && npm run dev &
   sleep 5  # wait for server to start
   ```

2. **Take screenshots** of relevant pages/states using the browser tool:
   ```
   browser_navigate → http://localhost:3000/<path>
   browser_vision → capture screenshot
   ```

3. **Kill the dev server** when done:
   ```bash
   kill $(lsof -ti:3000) 2>/dev/null
   ```

### Screenshot evidence checklist

For every QA run, capture at minimum:
- [ ] The primary page/flow being tested
- [ ] Any modal/drawer/dialog interactions
- [ ] Error/empty/loading states if applicable
- [ ] Form submissions or action confirmations
- [ ] Mobile/responsive view if the task touches layout

Include screenshots in the QA report as MEDIA: paths or attach to the PR comment. If a screenshot cannot be captured (e.g., pure backend change with no UI), state why in the QA notes.

## Visual/UI verification requirement (additional for UI tasks)

### Playwright/dev server cleanup requirement

When any agent (Dev or QA) starts any local dev server, preview server, test server, or browser process for Playwright QA or development, the agent MUST stop it before finishing the run AND before transitioning the issue out of their active state label (`in progress` → `qa ready`, or `qa in progress` → `review ready`/`feedback`). This includes `npm`/`pnpm`/`yarn dev`, Vite, Next.js, preview/serve commands, Playwright browsers, and any child process started only for the run.

Required cleanup steps before label transition or final response:
- Stop the server/process that the agent started, preferring graceful termination first.
- **Remove any worktree** created for the issue if the issue is no longer in an active state (`in progress` or `qa in progress`). Worktrees for issues that moved to `qa ready`, `review ready`, `feedback`, or `need confirmation` should be removed — the next agent who picks it up will create their own.
- Verify no orphan dev server, Playwright, browser, or project-scoped Node process remains from the run.
- If the agent reused a pre-existing shared server, do **not** kill it unless the human owner explicitly approves; instead state that it was reused and left running.
- If process ownership cannot be determined safely, report the blocker instead of killing unrelated user/system processes.

A **workspace cleanup cron** (`~/.hermes/scripts/workspace-cleanup.sh`) runs every 10 minutes and automatically removes worktrees whose issues are no longer `in progress` or `qa in progress`. Agents should clean up proactively, but the cron is a safety net.

### 3. Regression check

Verify that existing functionality adjacent to the change still works. If the PR touches shared components, test other features that use them.

## QA/Test execution notes

For every QA review, write a clear "How QA/Test was performed" section in the PR or issue report. Include:

- **Scope tested**: issue number, PR/branch, acceptance criteria covered, and any criteria not testable.
- **Environment**: local/staging/preview URL, browser/device if relevant, test account/role used, and important feature flags or config.
- **Setup steps**: checkout command, install/build steps, seed/mock data, migrations, or API/service prerequisites.
- **Automated checks run**: exact commands, pass/fail result, and important output or failure summary.
- **Manual test steps**: numbered steps detailed enough that another QA/dev can reproduce the same verification.
- **Expected vs actual result**: concise result for each acceptance criterion and notable edge/negative case.
- **Regression coverage**: adjacent flows or shared components checked.
- **Evidence**: screenshots (mandatory — see screenshot requirement above), recordings, logs, test output, or notes explaining why evidence is unavailable.
- **Skipped or blocked tests**: what was not tested, why, and the release risk.

Do not mark QA as passed if Critical/Major flows are untested or blocked.

## Bug report format

File each bug as a separate comment on the PR (not grouped). Format:

```
### Bug: <concise title>

**Description**: What is broken.
**Steps to reproduce**:
1. Step one
2. Step two

**Expected**: What should happen.
**Actual**: What actually happens.
**Severity**: Critical | Major | Minor
**Screenshot/recording**: (if applicable)
```

Severity guide:
- **Critical**: Feature is broken, data loss, security issue. Blocks release.
- **Major**: Feature partially works but key flow is broken. Must fix before merge.
- **Minor**: Cosmetic, copy, or non-blocking UX issue. Can be addressed in follow-up.

## Label transitions

If a label required for a transition is not available in the repository (not created yet), create it manually first — e.g. `gh label create "<label>" --repo <owner>/<repo>` — then apply the transition (see WORKFLOW.md "Label rules"). Do not skip the transition because the label is missing.

| Result | Action |
|--------|--------|
| Starting a new eligible QA issue | Remove `qa ready` → Add `qa in progress` before detailed QA review/testing begins |
| All tests pass, code review clean | Remove `qa in progress` → Add `review ready` |
| Any Critical or Major bugs found | Remove `qa in progress` → Add `feedback`. **Circuit breaker:** if the issue has already completed 3 feedback cycles, add `need confirmation` instead with a summary of the failed cycles (see WORKFLOW.md "Feedback cycle limit"). |
| Only Minor bugs | Remove `qa in progress` → Add `review ready`. Log Minor bugs as follow-up tasks labeled `to be planned`. |
| Blocking dependency or confirmation need prevents safe QA completion | Remove `qa in progress` → Add `need confirmation` and document the blocker/question |

## Local workspace cleanup (mandatory after label transition)

**After transitioning the issue label from `qa in progress` to `review ready` or `feedback`, Agent QA must clean up the local worktree/clone used for the QA run in the same turn.** This is a hard final step in the QA workflow — not an afterthought, not deferred to the next run.

Cleanup steps:
1. Verify `git status --short` is clean in the worktree.
2. Verify the branch was pushed to the remote.
3. Verify the issue/PR has the final QA report, evidence, and useful context posted.
4. Kill any dev servers, Playwright, or browser processes started during the QA run.
5. Verify no active agent session or cron job is using the workspace.
6. Remove the worktree/clone directory.
7. Confirm disk space is reclaimed.

Do not leave stale worktrees for the next run to trip over. If cleanup cannot be performed (e.g., another session is actively using the workspace), report the blocker and retry cleanup as soon as possible.

## Rules

- Never approve with known Critical or Major bugs.
- Do not fix bugs yourself. Report them and send back to dev via `feedback`.
- If acceptance criteria are ambiguous or missing test cases, flag it — do not assume pass.
- Test on the PR branch, not on main.
