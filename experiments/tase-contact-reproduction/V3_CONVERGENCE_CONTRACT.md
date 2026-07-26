# Step5d V3 direct-autotune convergence contract

Status: r006 offline release and exact controller readback closed on 2026-07-21;
bridge/runtime and live acceptance remain open.

This file and `config/step5d/v3_active_surface.json` are the only durable
resume surfaces for the convergence task. Historical rollouts, certification
artifacts, and V1 launchers are not runtime truth.

## Terminal condition

The task remains active until an attended V3 r006 campaign has:

1. loaded and read back the exact versioned TP triplet;
2. started the canonical bridge and emitted `V3_BRIDGE_READY_NO_ARM`;
3. observed user-owned TP Play and entered the command-1 campaign path;
4. reached real Stage25 autotune control;
5. completed 10 exact direct commits, entered every next row without ACK, and
   completed the row-10 final-home closure without ARM11.

Offline tests, a controller readback, a bridge-ready message, a commit, a push,
or an audit cannot complete the task. A safety stop preserves evidence and
leaves the task incomplete.

## Locked decisions

- Release/stage: `step5d_strict_rnn_autotune_v3`.
- Historical TP: `step5d_strict_rnn_autotune_v3_r005`, whose exact
  readback remains factual but whose disposition is
  `known_incompatible_do_not_retry`. It cannot produce a bridge context or an
  optimizer observation.
- Deployed/current TP: `step5d_strict_rnn_autotune_v3_r006`, immutable and
  exact controller-readback verified at 2026-07-21T11:00:33+08:00. It is
  bridge-context eligible but not loaded, playing, armed, motion-authorized,
  or live-accepted. r004 and r005 remain immutable incident evidence. Every
  later content update requires a new basename `r007`, `r008`, and so on.
- Future delivery must use `scripts/step5d-autotune-v3.sh deliver-r006`, whose
  single transaction performs the local gate, exact triplet upload, fresh GET,
  SHA/identity comparison, and manifest-driven promotion/rebuild. Direct use of
  the lower-level uploader is not a completed V3 promotion. An unchanged,
  already-promoted triplet is not re-uploaded merely to start a bridge.
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
- Canonical protocol ID is `v3_direct_arm_v1`. The active r006 path accepts
  command 1 (ARM) and safe command 3 (operator stop request). A fresh command 2
  (`ACK_BUNDLE`) is rejected without advancing `consumed_command_seq`.
  Commands 4/5/6 are retired. Legacy ACK schemas remain replay-decode only.
- Rows 1..9 return to NearReady; row 10 returns to CampaignHome. Return motion
  is three standard `movel` segments: rise to safe Z, transfer at safe Z, then
  vertical descent. One read-only return telemetry observer may sample TCP
  angular speed and controller/sample time, but it may not call `movel`,
  `movej`, `speedl`, `speedj`, `servoj`, `stopl`, `stopj`, or otherwise own
  motion. No custom `speedl` return controller is active.
- After return, rows 1--9 publish state 76 and remain stationary for the next
  ARM for at most 30 seconds; timeout produces a typed fault and bounded halt.
  Row 10 publishes state 77 and bounded-halts immediately. State 75, 77, or 90
  can never accept another ARM.
- The return latches phase `40.3`, segment `3`, and its angular envelope. There
  is no r006 TP `WAIT_ACK`, host `pending_ack`, ACK sequence, or active
  PostAckClosureCollector.
- The immutable r004 and r005 CSV/summary/fixture sets are incident evidence
  only. r004 reproduces `return_phase_mismatch`; r005 reproduces 5,013 fresh
  state-76 rows, missing unprefixed fields, consumed ACK sequence 2, and no
  ARM2. Neither can enter the optimizer.
- Production CSV timeout classification is disjoint:
  `no_fresh_rows`, `fresh_rows_never_qualified`, and
  `terminal_seen_capture_not_sealed`.
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
  Stage25, launch one non-blocking Fable advisory and one Sol / xhigh read-only
  audit in parallel with the experiment.
- The old unversioned V3 package and `step5d_strict_rnn_no_contact_p0_v9`
  must be archived only after a fresh controller readback proves the exact
  source identities.

## Monotonic blockers

The blocker set is closed and may only move open -> complete:

1. direct-autotune contract and active surface;
2. r006 TP generation and V1 control parity;
3. active bridge/campaign wiring without certification, moving sphere, or
   authorization-file gates;
4. canonical selector/readiness/status and truthful failure evidence;
5. targeted, impacted, installed-runtime, and cross-process growing-CSV
   production rehearsal;
6. attended r006 controller upload/readback; r004/r005 remain archived incident
   evidence;
7. published OID and formal-runtime fast-forward deployment;
8. actual r006 bridge ready;
9. user Play observed and real Stage25 reached;
10. exact 10/10 direct commits plus final-home closure and no ARM11.

A new finding must map to one of these blockers. After two patch/test cycles on
one blocker, write a root-cause checkpoint before a third patch.

## Excluded work

No Step6/7/8, skill-sync, timing pressure test, timing service, kernel, Docker,
system package, Pinocchio, realtime-limit, remote-desktop, broad legacy-code
deletion, TP Play, TP Stop, or unrelated robot program is in scope.
