# Yield measured-observation adapter v1

Status: transport-free adapter implemented and tested; sole-writer lifecycle integration remains incomplete.
No robot endpoints, command publication, activation, or physical qualification occurred.

The Homepool Grok High writer terminated with exit 143 without a completion receipt.
Its four partial files were retained unchanged in runs/yield-provider-partial-20260920
and the isolated worktree. Main explicitly took over, reviewed, repaired, and tested.
Launch metadata exists in the recorded task-run directory; no successful native
completion or substitute model is claimed. See writer-failure.json.

## Implemented and verified

- Shared measured-observation admission preserves raw wrench limits, active TCP/payload/CoG,
  real host-monotonic receive age, 20/80 ms semantics, and calibrated Jacobian.
- YieldContactRuntime drives baseline, explicit one-second entry, and PATH. It rejects
  skipped entry, entry-clock jumps, and return to baseline. The first formal PATH time
  preserves the actual boundary crossing (zero for an exact 2 ms grid; 2 ms for
  the tested 3 ms grid) instead of freezing time or inventing a zero sample.
- The pre-PATH stationary seam carries native memory/filter/normal/integral/QP state,
  advancing only the explicit observed clock. It cannot pause an active PATH.
- The baseline force ramp is opt-in, bounded to 0..5 N, and included in controller identity.
  Original default 5 N behavior and identity remain intact. PATH remains 5 N.
- Task basis must be orthonormal, not merely determinant one. Runtime snapshot identity
  binds controller, calibration, task basis, anchor, and entry duration.
- Orientation error follows estimated normal and the transported roll anchor.
- Failed steps restore control state and committed clocks. Freshness rejection counters
  remain diagnostic evidence; they are intentionally not erased by rollback.
- The first sample must match the configured interval; subsequent intervals must match
  observed monotonic differences. The adapter cannot silently replace a supplied 3 ms
  first interval with the inherited 2 ms default.

149 contact regression tests passed before the final first-interval guard; all 5 adapter
checks then passed with that guard. One existing ROS xacro deprecation warning remains.
The original g50 normal full-cycle artifact replayed for 31,916 ticks with no full-state
mismatch. This verifies implementation compatibility, not independent physical validity.
See tests.txt, first-interval-tests.txt, and legacy-full-state-replay.json.

## Remaining integration work

The mature CanonicalQualificationControl currently transitions directly from baseline
to PATH after TP state 25. It does not yet emit the explicit one-second entry accepted
by this adapter. The new adapter deliberately rejects that shortcut. Next, bind entry
and formal PATH clocks in the sole writer and evidence collector together; count entry
separately without freezing or truncating the complete 62.83 s formal path.
An adapter-only test is not qualification through the mature writer seam.

The provider still exposes the legacy software diagnostic disturbance selector. It is
not equivalent to TR-v1's physical plant disturbance applied along true surface axes.
Before non-nominal provider qualification, version the injection schedule, frame, and
claim separately; raw force admission must remain before injection. No injected force
will be counted as measured human or contact force.

Route remains inactive. Admission still requires current platform/tool/package identity,
complete transport qualification, measured execution timing, and actual contact pilot.
The shared normal observer defect and unverified plant assumptions remain research work.

## Entry boundary correction

Main inspection found an extra-sample defect in v1: requiring a last entry sample
at exactly 1.000 s would delay formal PATH by one hold interval. The final sample
at 0.998 s plus its 2 ms hold now completes entry. Nonintegral sample intervals
carry their actual elapsed formal time. Runtime identity names this policy v2.
Six focused adapter checks pass, including 2 ms and 3 ms boundaries.

The mature writer still binds coverage to first TP state-25 / RTDE echo, builds
legacy XY references, and starts its duration fence there. Entry-aware control,
coverage boundary, complete-duration fence, and reference generation must be
changed together before activation. This correction does not claim that integration.
