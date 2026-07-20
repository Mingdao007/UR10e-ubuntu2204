# Step5d V3 direct-autotune convergence contract

Status: frozen on 2026-07-20 for the single direct-autotune lane.

This file and `config/step5d/v3_active_surface.json` are the only durable
resume surfaces for the convergence task. Historical rollouts, certification
artifacts, and V1 launchers are not runtime truth.

## Terminal condition

The task remains active until the attended V3 r004 campaign has:

1. loaded and read back the exact versioned TP triplet;
2. started the canonical bridge and emitted `V3_BRIDGE_READY_NO_ARM`;
3. observed user-owned TP Play and entered the command-1 campaign path;
4. reached real Stage25 autotune control;
5. completed all 10 exact ACK rows and the row-10 final-home closure.

Offline tests, a controller readback, a bridge-ready message, a commit, a push,
or an audit cannot complete the task. A safety stop preserves evidence and
leaves the task incomplete.

## Locked decisions

- Release/stage: `step5d_strict_rnn_autotune_v3`.
- TP revision: `step5d_strict_rnn_autotune_v3_r004`. Every later update must
  use a new basename `r005`, `r006`, and so on. A revision is immutable.
- Frozen Stage25 control provenance:
  `step5d_strict_rnn_autotune_v1` at tag
  `archive/step5d-autotune-v1-20260715` (commit
  `6f9ef0912842ac003545eb1906b38d13c7552218`).
- V3 may differ from V1 only in the physical prior/load-gated initialization,
  two-`movel` precontact entry, explicit ten-row lifecycle, and typed return
  destination. The RNN, trajectory, force/orientation outer loop, P/I/damping,
  qdot cap, and Stage25 control equations remain V1-equivalent.
- Stage22 is `PREALIGN_IN_PROGRESS`. Stage23 is `PREALIGN_VERIFIED` only when
  XYZ error is at most `3 mm` and approach-axis error is at most `2 deg`;
  Stage24/24.2 retain the orientation guard without reapplying precontact XYZ.
- TP accepts command 1 (campaign), command 2 (ACK), and command 3 (operator
  stop request) only. Commands 4/5/6 are retired from the active package.
- Rows 1..9 return to NearReady; row 10 returns to CampaignHome. Return motion
  is three standard `movel` segments: rise to safe Z, transfer at safe Z, then
  vertical descent. No custom `speedl` return controller or timing observer is
  active.
- Active V3 campaign safety is the frozen V1 guard set, including its 1.000 s
  Stage25 heartbeat watchdog and 2 ms last-command hold. Moving-sphere
  enforcement and no-contact certification are retired from the active campaign.
- User TP Play is the sole motion authorization. There is no certification or
  campaign authorization file. Codex may start the bridge but must never press
  TP Play or TP Stop.
- A bridge may start in NO_ARM/idle command state, but motion begins only after
  the user presses TP Play and the exact command-1 campaign identity is
  observed.
- Formal timing/stress testing and the temporary CPU/GPU timing service are not
  part of this task.
- Audits never block experiment startup. Only after the real campaign reaches
  Stage25, launch one non-blocking Fable advisory and one Sol/XHigh read-only
  audit in parallel with the experiment.
- The old unversioned V3 package and `step5d_strict_rnn_no_contact_p0_v9`
  must be archived only after a fresh controller readback proves the exact
  source identities.

## Monotonic blockers

The blocker set is closed and may only move open -> complete:

1. direct-autotune contract and active surface;
2. r004 TP generation and V1 control parity;
3. active bridge/campaign wiring without certification, moving sphere, or
   authorization-file gates;
4. canonical selector/readiness/status and truthful failure evidence;
5. targeted, impacted, installed-runtime, and no-network production rehearsal;
6. r004 controller upload/readback and predecessor archives;
7. published OID and formal-runtime fast-forward deployment;
8. actual r004 bridge ready;
9. user Play observed and real Stage25 reached;
10. exact 10/10 ACK plus final-home closure.

A new finding must map to one of these blockers. After two patch/test cycles on
one blocker, write a root-cause checkpoint before a third patch.

## Excluded work

No Step6/7/8, skill-sync, timing pressure test, timing service, kernel, Docker,
system package, Pinocchio, realtime-limit, remote-desktop, broad legacy-code
deletion, TP Play, TP Stop, or unrelated robot program is in scope.
