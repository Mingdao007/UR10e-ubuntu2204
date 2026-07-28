# STEP5 candidate flow: `step5d_autotune_start_hover_r001`

Status: controller read-back verified candidate; not the current Step5 or
Step5d stage.

This candidate documents a standalone Script 1 positioning helper. It does not
replace the canonical root pair `STEP5_FLOW.md` plus
`config/step5_stage_table.json`, and it does not change
`config/current_stage.json` or any release pointer.

| Item | Candidate contract |
|---|---|
| Current stage | Retain `step5d_strict_rnn_autotune_v3`; Script 2 is the existing `step5d_strict_rnn_autotune_v3_r026` triplet, byte-for-byte unchanged. |
| Retained geometry evidence | r026 Stage22 and return-route geometry: target `p[0.487834547, 0.129337053, 0.033000000, 3.120752062, 0.000000000, 0.068626833]`; transfer Z is `max(current Z, 0.033000000)`. |
| Candidate controller target | `/programs/andyl/kunwei/step5/step5d_autotune_start_hover_r001.urp`; uploaded and fresh-read-back verified against the immutable local triplet. |
| Flow | When separately live-gated, explicitly play Script 1 once to position at the campaign Home, then load/play unchanged r026. r026 captures the current pose on Play and keeps its existing lifecycle. |
| Bridge/contact contract | No bridge, registers, sensors, force control, contact, zero/tare, or runtime command protocol in Script 1. The helper is not a bridge stage. |
| Guards | Initial pose must be finite; fixed target and motion profile must pass conservative position/orientation/speed limits; final target and stationary speed checks must pass before the one-shot end. |
| Timing/path sanity | Segment 1 `a=0.060`, `v=0.040`; segment 2 `a=0.135`, `v=0.090`; conditional segment 3 `a=0.060`, `v=0.040`; `r=0.0`, `stopl(0.1)` between segments. See the paired numeric-sanity artifact. |
| Success condition | Script 1 completes its final stationary verification at the exact hover target. r026 remains the only current campaign program and is not changed by this candidate. |

The candidate pair is intentionally additive and non-promoting:

- this file is the candidate flow description;
- `config/step5_stage_table.json` is the candidate stage-table record;
- controller upload/read-back is complete and recorded in
  `controller-readback-verification.json`;
- no Dashboard Load/Play, bridge, ARM, motion, sensor write, current pointer
  change, or live acceptance is claimed by this delivery.
