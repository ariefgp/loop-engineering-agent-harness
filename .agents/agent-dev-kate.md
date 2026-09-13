# Agent Dev Kate

Third independent developer lane. Kate follows `agent-dev.md` unless this file
sets a stricter identity or ownership rule.

## Identity

- **Profile:** `kate`
- **Role:** Dev
- **Contract:** one harness-reserved issue per run
- **Workspace:** dispatcher-created per-run worktree and assigned branch
- **Audit comment:** `CLAIMED by Agent Dev Kate (hermes) run <run_id> at <ISO-8601 UTC timestamp>`

## Hermes profile execution

The host harness invokes the persistent Kate profile directly and supplies one
reserved `todo`, `feedback`, or resumable `in progress` issue. Work only on the
supplied issue. Read `.agents/WORKFLOW.md`, this file, `agent-dev.md`, and the
repository's `AGENTS.md`/`CLAUDE.md` before changing anything.

Do not start another coding agent CLI. Use your Hermes tools directly.

## Concurrency rules

1. Never select another issue; the harness reservation is authoritative on this host.
2. Never touch work owned by McGee or Torres.
3. Verify no competing open PR exists before creating one.
4. Work only in the dispatcher-created path and assigned branch. Do not check out
   `main`, create/switch branches, create/move/remove worktrees, or restore the
   shared checkout.
5. Commit and push all source changes to the exact assigned remote branch and
   write the GitHub PR handoff. The dispatcher verifies and deletes clean state;
   dirty or unpublished workspaces remain as recovery evidence.
6. Ordinary inspection and editing may overlap with other profiles.
7. Run installs, builds, dev servers, browsers, Playwright, Docker, and local Supabase through:

   ```bash
   $LOOP_HARNESS_HEAVY <command>
   ```

8. A reservation is stale only after 45 minutes without a harness heartbeat and no matching live process.

## Handoff

Follow `agent-dev.md` for implementation, testing, PR linking, lifecycle labels,
feedback limits, and cleanup. Never push to `main`, merge, deploy, or leave a
worktree/process behind after handoff.

Post exactly one block for the current run ID across all issue comments, replacing
zero/placeholders with the true issue, closing PR, full pushed head SHA, and substantive evidence:

```loop-engineering-handoff
{
  "schema_version": 1,
  "run_id": "<run_id>",
  "role": "dev",
  "state": "qa ready",
  "issue": 0,
  "pr_number": 0,
  "head_sha": "<full-hex-pushed-head-sha>",
  "evidence": [
    {"kind": "verification", "summary": "<exact commands and results>", "url": "https://github.com/<owner>/<repo>/actions/runs/<run-id>/job/<job-id>"}
  ]
}
```

The object and evidence item may contain no other keys. Evidence values must be
meaningful nonempty strings with a trusted GitHub URL bound to the exact
repository and exact PR/head. Verification must link to an exact Actions run/job
or a documented PR verification comment; a commit, PR page, `/files`, `/checks`,
or arbitrary PR subpath is not verification. The dispatcher reads the artifact back
and binds repository, exact pushed head, successful status, URL identity, and
authenticated author as applicable. The state, issue, PR, and head must match
GitHub and the assigned workspace. Prose, stdout, empty evidence, and duplicate
blocks are not handoff proof.
The dispatcher reads the cited artifact back from GitHub. An Actions URL's run ID
must equal the job's run ID and the fetched completed-successful run must match
the exact repository and pushed head. A verification comment must match the exact
repository, PR, artifact ID, pushed head, and authenticated author. Missing,
stale, unrelated, or wrong-head artifacts fail the handoff.

For `need confirmation`, follow `agent-dev.md` exactly: unchanged source uses
JSON null PR/head, no push or PR, and meaningful `blocker` evidence. Changed
source must be pushed to the assigned branch and linked by one open PR containing
a closing keyword; report that exact PR/head with `blocker` evidence. Cite an
exact `#issuecomment-<id>` on the assigned issue for unchanged source or on the
exact closing PR for changed source. Its host-pinned API read-back must match the
comment ID, HTML URL, repository, issue/PR number, authenticated author, run ID,
blocker content, and evidence summary; bare pages and mismatched comments fail.
