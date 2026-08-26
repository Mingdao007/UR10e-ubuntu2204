Implement one bounded behavior outcome in the R013 worktree:

The current default `R013BudgetedFloorConfig` is permanently offline-only (`launch_ready=false` in `__post_init__`), so a future completed handoff/boundary/correction prerequisite set has no legal typed transition into the requested 200-candidate live campaign. Add the smallest typed/versioned/logged pure transition that preserves the default offline JSON and fail-closed behavior but makes the eventual campaign launch possible.

Required contract:

- Default config remains `status=offline_prepared_awaiting_live_prerequisites`, `launch_ready=false`, pending handoff, and the exact four offline blockers.
- A separate typed live-ready package/config or explicit transition may become `launch_ready=true` only when supplied with all of: a complete `HandoffABPlanV1` plus hash-bound `HandoffSelectionReceiptV1`, an installed outward-boundary runtime receipt, an installed six-context correction runtime receipt, and valid source/runtime identities required by the campaign fingerprint.
- The transition is pure and does no bench/controller/network/bridge/motion/contact/Home/tare I/O. It must reject missing, partial, malformed, mismatched, stale, or wrong-fingerprint receipts.
- Update the eventual live-preparation caller so it can consume that explicit live-ready package. Do not weaken safety, authority, admission, lifecycle, real-gap, or single-writer invariants.
- Keep the direct `dataclasses.replace(..., launch_ready=True)` falsifier failing, and keep no implicit handoff default.

Use the closest existing typed primitives. Add/update only focused tests proving: default remains blocked; missing/partial receipts fail; complete receipts materialize a launch-ready package and `require_launch_ready()` succeeds; fingerprint/receipt mismatch fails; no live callback opens during offline tests. Do not run any live operation. Do not touch unrelated dirty files. Run the existing focused R013 tests plus the new focused tests. Stop at focused green and report changed files/test output.
