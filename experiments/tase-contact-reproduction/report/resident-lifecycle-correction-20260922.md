# Resident lifecycle correction, 2026-09-22

Scope: additive correction of the prior live claims. Original run artifacts are unchanged. No new hardware execution is claimed here.

## Established observations

- Installed-source TP is a resident `while True` program. State 78 is READY_HOME_NEXT, state 80 is COMPLETE, and state 90 is a wire STOP state. None alone proves Dashboard STOPPED.
- The host currently finalizes a full path collector synchronously before `run_live` reaches `stop_and_confirm`. `PathEvidenceCollector.finalize` includes Python motion statistics, JSON material construction and hashing for the full trace.
- In all eight examined attempts the final writer observation is state 78; the next recorded STOP is classified `sensor_stale`, 1.35--1.70 seconds later. This is a demonstrated host-side terminal service gap, not a normal TP shutdown proof. The STOP label alone does NOT prove sensor dropout: `_stop_only_sensor_packet` deliberately sets `sensor_fresh=False`, and the wire encoder labels that stop `sensor_stale`.
- The socket read reports peer reset/EOF in six examined attempts. Historical evidence does not identify the peer-side close cause. Buffer overflow during absent draining is a hypothesis until reproduced or confirmed by transport/controller evidence.
- Commits 685943da and 974dc282 suppress terminal failures and skip actual program-stop verification. Those changes cannot be used to certify cycle infrastructure.
- The original 13:33 UTC attempt has genuine 60s coverage, bound 0.1 integral configuration, Home and final Dashboard STOPPED. It also has the terminal sensor_stale gap; retain its physical results without claiming a healthy repeated-session lifecycle.
- A 78s Home-to-Home duration excludes preparation, durable sealing and next-attempt handoff. It is not the full-cycle 85s acceptance metric.

## Source records

| Run | Full PATH | Timing | MAE (N) | Gap before sensor_stale STOP (s) | Recorded final Dashboard |
|---|---|---|---|---|---|
| `tase-figure8-0p1-canonical-20260922T133309Z` | True | True | 1.989386 | 1.666897 | Program running: false |
| `tase-figure8-0p1-refresh-20260922T134119Z` | True | False | 1.978087 | 1.353834 | Program running: false |
| `tase-figure8-0p1-reuse-cpu4-20260922T134615Z` | True | True | 1.787757 | 1.644663 | not present; inspect recovery |
| `tase-figure8-0p1-validation2-cpu4-20260922T135849Z` | True | True | 1.704903 | 1.693419 | not present; inspect recovery |
| `tase-figure8-0p1-validation3-cpu4-20260922T140600Z` | True | True | 1.611918 | 1.683500 | not present; inspect recovery |
| `tase-figure8-0p1-validation4-cpu4-20260922T142313Z` | True | True | 1.858002 | 1.690805 | not present; inspect recovery |
| `tase-figure8-0p1-validation5-cpu4-20260922T143104Z` | True | True | 2.012662 | 1.672499 | not present; inspect recovery |
| `tase-figure8-0p1-validation6-cpu4-20260922T143658Z` | True | True | 1.889877 | 1.694764 | Program running: true |

## Result interpretation

- Complete PATH diagnostic MAEs remain reportable. Original completion and failure flags are preserved; this correction does not convert recovered attempts to success.
- `validation6` and `screen-A-00-retry` report success while Dashboard is still PLAYING and sockets/observers were closed. They are not continuous-session acceptance evidence.
- The early A/D screening launch preceded infrastructure acceptance. Retain it as exploratory data; do not mix it into the new randomized screening.
- Restarted/failed preparation and preflight attempts remain in all-operation records. New screening starts only after the real session lifecycle, timing and observation tests pass.

## Repair acceptance

- Preserve a single owner and continuous receiver/idle service during collector reduction, provider snapshot, raw serialization and parameter proposal.
- Separate ready-at-Home from protocol stop acknowledgement and actual Dashboard/RTDE program stop.
- Keep observer/sensor/transport errors visible and hand off faults to the existing automatic joint-Home owner before expensive sealing.
- Verify two ARM cycles offline through the real entrypoint, then one cold plus five continuation units with actual freshness renewal and full wall-clock accounting.
- Do not accept unbounded reference clamping, cached-frame stop proofs, reset failure flags, or hidden retries.

## Tuning and screening findings retained for the next implementation slice

- Current tuner uses incumbent Md/Bd as both initial candidate and search-box center. This yields Md [6.763668792656509, 13.527337585313019], Bd [490.4888116059486, 980.9776232118973], which differs from approved Md [8.48528137423857, 16.970562748477143], Bd [388.9087296526011, 777.8174593052023]. Three of eight current initial candidates are out of the approved box. Separate initial values from absolute bounds.
- Confirmation currently calls the same `_write_candidate(..., config)` for both sides. It must freeze baseline at incumbent Md/Bd plus A, and freeze the challenger at the selected integral strategy; a shared selected-strategy config would corrupt the comparison.
- D currently binds the conditional primitive and authority clamp, but its final successful-send joint/slew/Jqdot feedback connection is not established by these bindings. It must be implemented and behavior-tested before D live admission.

## Current offline verification

- Full mature Figure-eight endpoint-double regression: `tests/test_contact_yield_live.py::test_mature_full_figure8_with_trajectory_endpoint_double`, 1 passed in 46.47 s, exit 0. This checks the full-period synthetic endpoint route only; it is not two-ARM or live acceptance. The process was tracked to terminal completion; it was not deselected. Log: `/tmp/tase-resident-repair/baseline-full-test.log`.
- Offline serialization-cost probe on 38,721 recorded robot frames produced 37,005,552 bytes in 1.1248 s (JSON-safe conversion 0.6947 s; encoding/hash 0.4301 s). This is not an exact reconstruction of collector input and does not prove the cause of peer reset, but demonstrates why large sealing operations require continuous transport service.
- Local socket-pair probe using the actual WritableRTDEClient and an artificial 500 Hz peer: continuous reading consumed 950 samples with no close; a 1.7 s read blackout caused the bounded-buffer peer to time out after 13 sends, followed by `RTDE socket closed` on the client. The command exited 0. This demonstrates a possible backpressure mechanism only: peer buffer/timeout settings were synthetic, and the actual controller close cause remains unconfirmed. Probe: `/tmp/tase-resident-repair/socket-backpressure-repro.py`.
- Draft thread-finalizer probe: serializing 38,871 canonical robot frames with `json.dumps` inside the draft `ResidentSession._run_terminal_finalize` worker produced a maximum 0.421484 s service interval. The local peer did not close in this test (the publisher also shares the GIL), so this is evidence of host service starvation, not a peer-close reproduction. A thread alone does not establish the required 80 ms freshness bound. Probe: `/tmp/tase-resident-repair/finalize-thread-probe.py`.
- Repeating that draft-finalizer test with the artificial peer in a separate process removes the shared-GIL confound: the peer timed out after 13 sends and the client raised `RTDE socket closed` before its first completed service call, over 0.437448 s. This reproduces the defect in the draft thread-only service under the stated synthetic peer settings; it still does not identify the historical robot-side close cause. Probe: `/tmp/tase-resident-repair/finalize-independent-peer-probe.py`.
- The revised process-isolated finalizer passed the same independent-peer acceptance (exit 0): 229 service calls while serializing the same 38,871 rows; maximum service interval 0.002863 s, no client error and no peer timeout. The test now asserts no disconnect and a service interval below the existing 80 ms bound. This verifies that finalization seam only, not full session sealing, refresh, two-ARM completion or live acceptance. Probe: `/tmp/tase-resident-repair/finalize-independent-peer-acceptance.py`.
- Interim supervisor/host/admission regression on the evolving repair: `tests/test_contact_yield_supervisor.py`, `tests/test_contact_yield_host_lifecycle.py`, `tests/test_contact_yield_admission_guards.py`: 46 passed in 1.39 s, exit 0 (one dependency deprecation warning). Log: `/tmp/tase-resident-repair/main-lifecycle-regression.log`. Final patch acceptance still requires the new resident scenarios and integrated entrypoint.

## Owner closure before first resident live acceptance

- Six synthetic ARM units completed with 550 bins each, two expiry refreshes, and verified synthetic program termination (`/tmp/tase-resident-repair/main-six-arm-02/dispatch_receipt.json`). This is not live acceptance.
- Astra High (`hp-astra`, `gpt-6-astra`, `high`, session 01a0c9e6-7eab-7e30-a2cc-bc47bd028ea2) identified four findings: inherited FIFO worker scheduling, full receipt serialization before recovery, lost pending service snapshots on failed sealing, and missing terminal STOP/timeline records. Main reproduced and repaired these without another full six-unit offline run. Original findings remain in `/tmp/tase-resident-repair/astra-resident-review.log`.
- Real Linux short pressure probe passed with owner SCHED_FIFO/20 pinned to CPU4; evidence workers demote to SCHED_OTHER before CPU work. The independent peer remained serviced below the existing80ms bound.
- Post-review related checks:80passed; entry/provider/fault regression including full-period endpoint case:50passed in78.93s; return-neutralization checks:2passed. Explicit tests retain failed service batches, exclude large traces from recovery handoff, and restore freshness state.
- The TP rejects stale ARM at Home with reason62. Host now retires ARM during RETURNING and sends session HOLD during resident idle, while preserving new ARM. Existing protections and package bytes are unchanged.
- Packet ids are reserved before attempted send; send failures roll back the unpublished control step and cannot reuse the uncertain packet id.
- First live preparation: `runs/tase-resident-infra-20260922T163000Z`; fresh package readback passed and4751stationary distinct baseline samples collected over10s. Video unavailable under approved evidence-only policy; continuous RTDE and Kunwei remain the alternative observations. No motion completion is claimed by preparation.

## First resident hardware attempt and focused repair

- `tase-resident-infra-20260922T163000Z` executed the Figure-eight and returned to Home, then failed with observer TimeoutError during terminal handoff. Recovery completed automatically at joint Home with Dashboard STOPPED. This attempt remains failed.
- Raw packet reconstruction covers550bins in[5,60), diagnostic normal-bin MAE1.777492N. The original collector result was lost before failure sealing; reconstructed metrics are explicitly labeled and are not a successful BO observation.
- A read-only real-controller probe (no input recipe or motion commands) reproduced an observer gap85.269ms after parent GC74.891ms. Disabling collection for the resident interval passed three40MB CPU/JSON finalizations; max service gap4.787ms under FIFO20/CPU4. This establishes a reproducible starvation trigger; it does not prove every historical peer-close cause.
- Keep cyclic GC outside the session, skip forced per-arm collection, retire collector/raw buffers in serviced chunks, retain completed collector evidence before later handoff checks, and seal per-attempt results for immediate reporting. All raw service frames/packets remain; duplicate per-frame receipt events are removed. Focused lifecycle checks:56passed.
