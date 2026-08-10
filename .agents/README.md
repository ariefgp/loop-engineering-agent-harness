# Loop Engineering Agent Harness

A label-driven, multi-agent GitHub workflow for running autonomous PM → Dev → QA loops with Claude Code CLI, triggered by cron. Issues move through an explicit state machine; each agent picks up work by trigger label, does its job, and hands off by transitioning the label — no human pressing enter between steps.

Built on the ideas of **loop engineering**: designing the iterative system an agent runs in (trigger → plan → execute → verify → repeat), where the verifier — not the model — is the bottleneck. This harness makes the loop **closed**: measurable acceptance criteria are pinned upfront by the PM agent, an independent QA agent verifies against them, and circuit breakers cap how long any issue can loop without human intervention.

## The state machine

```
to be planned     → [Agent PM]  → plan approval | need confirmation
need confirmation → [Human]     → to be planned (re-plan after answer)
plan approval     → [Human]     → todo
todo              → [Agent Dev] → in progress → qa ready | need confirmation
qa ready          → [Agent QA]  → qa in progress
qa in progress    → [Agent QA]  → review ready → done | feedback | need confirmation
feedback          → [Agent Dev] → in progress → qa ready | need confirmation
```

Every issue has exactly one state label at all times. Humans gate two points: plan approval and the final merge. Everything else runs autonomously.

## Files

| File | Purpose |
| --- | --- |
| [`WORKFLOW.md`](WORKFLOW.md) | The state machine, label rules, claim protocol, feedback circuit breaker, CLI execution model, pre-check gates, cleanup and handoff protocol — the shared contract all agents follow. |
| [`agent-pm.md`](agent-pm.md) | Agent PM: turns raw issues into full specs — code inspection, project-brief alignment, dependency checks, acceptance criteria, Playwright E2E test lists, and recommendations attached to every open question. |
| [`agent-dev.md`](agent-dev.md) | Agent Dev: implements specs in isolated git worktrees, opens linked PRs, guards against duplicate servers/processes, and cleans up its workspace at handoff. |
| [`agent-qa.md`](agent-qa.md) | Agent QA: independent verifier — code review, functional testing against all acceptance criteria, real-data and rendered-UI evidence requirements, structured bug reports. |

## How a run works

```
Cron scheduler (every N min)
  → isolated session (light context, tiny prompt)
    → pre-check gate: gh CLI checks for eligible trigger-label issues
      → no eligible issues → NO_REPLY (no LLM invoked, no quota used)
      → eligible issues found → claude -p --model <model> "<task prompt>"
        → Claude Code reads .agents/ files and does the real work
        → on Claude Code failure → fallback engine runs the same task
  → announce result to the delivery channel
```

The `.agents/` files are the single source of truth — the cron payload is a thin wrapper that tells the execution engine to read them and act.

## Key mechanisms

- **Pre-check gate** — a free `gh issue list` call decides whether to spawn the LLM at all. No-op runs cost one GitHub API call, zero tokens.
- **Independent verifier** — QA is a separate agent from Dev (no self-verification), tests on the PR branch against real application data, and must produce rendered-UI evidence for visual work.
- **Claim protocol** — starting work means self-assigning and posting a structured `CLAIMED by <agent> (<engine>) at <timestamp>` comment. A claim with no activity for 2 hours is stale by definition, so interrupted work is resumable and concurrent runs can't collide.
- **Resume-before-new guards** — Dev and QA must resume any stale `in progress` / `qa in progress` issue before picking up new work, so interrupted work never rots.
- **Feedback circuit breaker** — after 3 dev↔QA feedback cycles on one issue, the loop stops and escalates to `need confirmation` with a summary and recommendation, instead of burning budget indefinitely.
- **Derive before asking** — agents must exhaust related issues, PRs, code, design evidence, and project notes before asking a human, and every open question ships with a recommendation.
- **Worktree isolation + mandatory cleanup** — each task runs in its own git worktree, and cleanup (worktree, processes, disk) happens at handoff, in the same turn as the label transition.
- **Model asymmetry** — PM and QA run on a deep-reasoning model (opus) for specs, edge cases, and verification; Dev runs on a fast model (sonnet).

## Adapting to your repo

These files always refer to one repository: the repo that contains the `.agents/` directory. Project-specific values are expressed as placeholders your cron wrapper/prompt must supply:

1. Copy the `.agents/` directory into your repo.
2. Supply `<owner>/<repo>` (your repo's GitHub slug) for the pre-check `gh` commands, and `<repo-path>` (local checkout path) for the CLI invocation.
3. Optionally supply `<notes-repo-local-path>` / `<notes-repo-url>` (a companion project-context notes repo) and `<runtime-env-file-path>` (a local env file for env-dependent tests). Agents skip those steps if not configured.
4. Adjust project conventions (design system rules, E2E test requirements, coding standards) to match your stack.
5. Set up cron jobs pointing at your repo using the wrapper pattern above.
6. Create the state labels in GitHub: `to be planned`, `need confirmation`, `plan approval`, `todo`, `in progress`, `qa ready`, `qa in progress`, `review ready`, `feedback`, `done`. If any label is missing at runtime, agents create it manually (`gh label create`) before applying it rather than skipping the transition.

## Background

The design follows the loop-engineering framing popularized in 2026: closed loops with explicit success criteria and stop conditions, separate verifier agents, cron triggers with cheap gates, and file-based contracts as the agents' source of truth. See the [Loop Engineering Guide](https://www.aibuilderclub.com/blog/loop-engineering-guide-2026) for the conceptual grounding.
