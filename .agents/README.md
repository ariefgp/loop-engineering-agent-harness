# Agent contracts

This directory contains the role and lifecycle contracts loaded by the
Hermes-native loop-engineering harness. The architecture, commands,
configuration, rollout, and transport boundaries are documented in the
repository root [`README.md`](../README.md).

## Files

- `WORKFLOW.md` — shared lifecycle, reservations, handoffs, evidence, cleanup
- `agent-pm.md` — Gibbs
- `agent-dev.md` — McGee
- `agent-dev-torres.md` — Torres
- `agent-dev-kate.md` — Kate
- `agent-qa.md` — Jimmy
- `agent-qa-ducky.md` — Ducky

The deterministic scanner supplies exactly one reserved issue to each profile.
Agents do not scan for additional work. All six profiles may work concurrently;
resource-heavy commands run through `$LOOP_HARNESS_HEAVY <command>`.
GitHub remains lifecycle truth. Bot Mode, peer, A2A, and Telegram are optional
communication/presentation surfaces and cannot claim or transition issues in
single-host harness v2.
