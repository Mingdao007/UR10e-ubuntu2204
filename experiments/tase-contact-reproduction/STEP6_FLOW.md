# Step6 8-Shaped Flow

Step6 retains the paper Experiment #2 8-shaped path as Kunwei bridge evidence.
`config/current_stage.json` currently points to Step5d, not Step6. Step4g
remains historical evidence only.

## Selected Target

| program | owner | contact | bridge | target |
|---|---|---:|---:|---|
| `step6b_contact_eight_baseline_v2` | TP + bridge | true | true | `/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v2.urp` |

Step6a remains retained no-contact rehearsal evidence. Step6b v1 remains
retained contact evidence: it completed the runtime/state-machine contract, but
its 4 mm/s path cap and 6 mm/s total cap were infeasible for the Step6 8-shaped
reference.

For Step6a and Step6b ROS2 migration, historical replay, and live-gated refactor
work, the authoritative Local Control textbook is
`config/local_control_textbook_spec.json` plus `config/step6_stage_table.json`,
`config/step6_eight_safe_frame.json`,
`programs/step6/step6a_eight_no_contact_v1.script`,
`programs/step6/step6b_contact_eight_baseline_v2.script`, retained Step6b v1
provenance, and relevant Auditor reports under `/home/andy/codex_handoffs/`.
Implementor handoffs and completion reports must first list these textbook
sources under `Textbook Sources Inspected`, then include a
`Local Control Textbook Alignment` table that marks each field as preserved,
changed with reason, or out of scope. Step6a no-contact fixed-Z/cadence/caps and
Step6b contact scaffold, 5 N target, filtered-live normal correction, bridge
limits, feasibility correction, guards, and operator-only recovery surfaces must
be explicit classifications. This textbook gate is offline evidence only; it does
not authorize bridge start, TP Play, upload, live motion, contact, payload/TCP
writes, safety writes, or `zero_ftsensor()`.

## Waypoint Calibration

Step6 starts with five manually positioned, read-only RTDE snapshots:

| label | phase rad | t s | local along mm | local lateral mm |
|---|---:|---:|---:|---:|
| `center_start` | 0 | 0.000 | 0.000 | 0.000 |
| `right_upper` | pi/4 | 3.927 | 28.284 | 10.000 |
| `right_lower` | 3pi/4 | 11.781 | 28.284 | -10.000 |
| `left_upper` | 5pi/4 | 19.635 | -28.284 | 10.000 |
| `left_lower` | 7pi/4 | 27.489 | -28.284 | -10.000 |

Formula:

```text
along = 0.04 * sin(0.2t)
lateral = 0.01 * sin(0.4t)
duration = 30 s
```

## Contact Baseline

Step6b uses the successful Step5b contact scaffold:

- first-contact normal latch
- 20 mm lift
- 25.2 attitude correction
- second contact
- 25.3 line-entry gate
- 25.0 bridge-owned active 8-shaped path for 30 s

Bridge contract:

```text
--step4e-version step6b_v2 --step4e-path-shape eight --target-force-n 5.0
```

The active path is the Step6 five-waypoint safe frame, not the old Step4g line
midpoint frame:

```text
along = 0.04 * sin(0.2t)
lateral = 0.01 * sin(0.4t)
duration = 30 s
```

Bridge speed profile:

```text
step4e_motion_limit_m_s = 0.015
step4e_total_linear_limit_m_s = 0.015
step4e_normal_velocity_limit_m_s = 0.003
step4e_angular_limit_rad_s = 0.060
```

Offline feasibility gate:

- Step6 reference max speed is 8.944 mm/s.
- With a 3.0 mm/s normal reserve, required total linear speed is 9.434 mm/s.
- The v2 15 mm/s path and total caps pass; v1's 4/6 mm/s caps fail.
- 25.2 attitude capacity is 0.060 rad/s * 8 s = 0.480 rad, covering the
  0.523599 - 0.052360 = 0.471239 rad design window.

Timing:

- `speedl(..., 0.001)` is kept as the near-500Hz cadence request.
- Path time is advanced with `get_steptime()`, not the requested hold value, so
  the 30 s active stage tracks controller elapsed time instead of stretching to
  about 60 s on a 2 ms execution cadence.

The safe frame is no-scale rigid rotation + translation from those five points.
If residuals or the X guard fail, no TP package is generated or uploaded.

## Boundaries

- Step6a: no contact and no Kunwei bridge.
- Step6b: contact motion through the bridge only after explicit operator
  confirmation.
- No `zero_ftsensor()`.
- No TCP/payload write.
- Controller delivery is file upload + read-back verify only; it is not load,
  Play, URScript send, or motion.

## R013 Figure-eight transfer package

`config/step5d/r013_figure8_transfer_template_v1.json` is a non-launchable
offline transfer template. It defines `figure8-force-mae-v1` as 5 N over
`[5,30)` with exactly 250 complete 0.1 s bins, a maximum of 160 novel
candidates, 24 designed warm starts, and a minimum of 80 novel candidates.
After 80, posterior convergence requires 25 fresh valid proposals whose maximum
probability of improving the robust incumbent by at least `0.01 N` is strictly
below 5%, with the top three candidates each at `n=5`; otherwise the separate
budget stop is 160. There is no absolute-MAE early stop.

Only the controller core, anti-windup, selected handoff, admission, and repeat
noise model are designated transferable. Figure-eight `motion Kp`, `Ko`, and
correction remain path-specific; cycloid bias/speed/acceleration/curvature terms
are weak priors and the phase `sin/cos` weights reset to zero. The package
remains `launch_ready=false` until the cycloid 200-novel empirical-floor work,
top-three `n=5`, live handoff A/B, Figure-eight correction runtime, and frozen
campaign seams are all completed. Historical Step6 evidence supplies geometry
and safe-frame clues only, not current live acceptance.

## Step6 Figure-eight direct campaign lineage

Before any Step6 V5 live campaign, the current V4 gate is two serial,
identity-separated campaigns from
`config/step5d/v4_two_stage_campaign_v1.json`:

1. `V4_FF_IOFF_100`: combined historical FF bundle, I-off, 5D local-first
   qLogNEI, 100 total attempts, censored attempts retained but nontrainable;
2. `V4_FF_ION_6D_100`: the same FF bundle, conditional double clamp and fixed
   1.0 N s state limit, full 6D P/D/damping/tau/Ko/motion-Kp/I/P search, also
   100 attempts;
3. `V4_FF_ION_LIMIT_7D_100`: an isolated limit-aware 7D search that inherits
   only the B1 winner coordinates and searches the feasible 0.5--5.0 N s
   state-limit coordinate under the fixed 0.5 N authority bound.

Each winner is confirmed to total `n=3` eligible exact rows. New
curvature/acceleration/history residual FF is deferred until both V4 stages
and the V5 core campaign are complete. The existing Goal-035 phase
force-reference schedule is recorded as FF intervention and never presented as
a pure fixed-5 N feedback floor.

`step6_figure8_autotune_v1` is a separate direct/provisional lineage. Its
source mode is `provisional_cycloid_n3`; the cycloid empirical floor is
unavailable and no historical cycloid or Figure-eight observation enters its
GP. The locked V5 path lifecycle is 60 s. The primary `figure8-force-mae-v2`
objective is filtered-normal evidence over `[0,60)`, exactly 600 contiguous
0.1 s bins; the legacy `[5,60)` 550-bin value is secondary comparability only.
V5 also carries the typed `FreshFrameWaitPolicyV1` with `wait_s=0.004`: the
wait is only a bounded RTDE fresh-frame rendezvous in the hot control loop,
not a 4 ms control period.

Contact keeps the V4 two-stage contract: the far/near boundary is
`11.029311 mm`, speeds are `5/0.2 mm/s`, accelerations are `0.01/0.005 m/s²`,
with a 50 N fuse, 25 mm travel limit, and 90 s timeout. Home is a two-stage
authority, never a guessed constant: run the full no-contact canary at the
calibration-only `z=0.0345 m`, acquire exactly three sealed non-BO contact acquisitions,
then freeze `Home z=max(contact_confirm_z)+13.525311 mm`. The final package and
final no-contact canary must both bind that calibration receipt before direct
trial admission or BO.

V5's primary GP/acceptance objective is the full half-open `[0,60)` evidence
window (600 bins); the historical `[5,60)` 550-bin value is retained only as a
secondary comparability metric. The closure tail `[60,20pi)` is continuity and
safety evidence only. Trial clock starts on the stable-contact tick with a
fixed 5 N target; the 1 N contact-acquisition latch is outside the trial.

The formal V5 kernel is fixed Matérn-5/2 ARD for one campaign fingerprint;
RBF and Matérn-3/2 are offline comparison arms only and cannot be swapped
mid-campaign.

The campaign target is exactly 200 different trial-admitted Exact novel
candidates plus top-three `n=5`. `campaign_target_stop=false`: neither the
first low MAE nor `0.35 N` ends the campaign. Trial-level censoring is active
only for ordinary novel BO after a repeat-confirmed incumbent. It starts after
25 formal bins and requests a sequence-matched graceful PATH end through input
register 35/output register 36 when prefix mean MAE exceeds `2 ×` incumbent
mean. A censored candidate is deduplicated and refilled, but it is nontrainable,
has no penalty MAE, and does not count toward the 200 Exact rows. Its raw prefix,
gaps, watermark, causal `sum(error)/550` lower bound, ack, and final Home closure
remain append-only evidence.

Physical dispatch remains one writer and serial `q=1`. Async work is limited to
one next-proposal materialization/128-point scoring/persistence job after the
current trial has entered safe return; the current candidate is supplied as
`X_pending`, and the next ARM remains forbidden until current Home and admission
close. Each owner segment is bounded by 320 attempts or 10 hours and resumes the
same fingerprint from its persisted Sobol cursor after a safe checkpoint.

The live gate order is calibration-package read-back → calibration Home
no-contact canary → three contact acquisitions → contact-derived Home → final
package read-back → final Home no-contact canary → matched `A-B-B-A-A-B`
handoff (winner `n=5`) → direct trial admission → 200-Exact BO → top-three
`n=5` → final Home/writer release → canonical HTML report. A hard/protective safety event
never auto-retries or auto-Homes; raw lifecycle gaps stay gaps and only complete
strict Exact rows call `tell_exact`.

Current offline closure (2026-08-17): the fixed-5 N entry, process-isolated
durability barrier, typed state-map, primary/secondary metric split, bounded
vision retention, source closure and hermetic focused suites are implemented.
The current official source identity is recorded by
`tools/step6_figure8_autotune_v1/source_identity.py` and the campaign config;
any further edit invalidates package/live evidence and requires a fresh rebuild.
The new main and no-contact canary triplets have fresh controller read-back
byte equality, but the current pointer remains Step5d V3.

The latest live preflight at 2026-08-17 HKT proved the direct Ethernet/Kunwei
route and RTDE/Dashboard ports, but observed `Safetymode: FAULT`,
`Robotmode: POWER_OFF`, and an unrelated loaded program. No Load/ARM/Play,
bridge, contact, V4 validation or V5 campaign is admitted until the controller
safety state is cleared and the route is revalidated. This is a controller
safety blocker, not sensor evidence.

## Long-goal binding (2026-08-19)

The timing/coverage seam is a hard proposal gate: PATH-end flag, trace flush,
candidate materialization, lifecycle seal, filesystem hashing and garbage
collection must complete at verified Home before ARM. A repeated
`CONTACT_SEARCH` first-frame timing signature pauses the current proposal
lane; it is never spent as an optimizer attempt. The V4 sequence is therefore
three consecutive timing canaries, Stage A, B1, B2, then evidence closeout.

Figure-eight is the final live workload. Epoch 0 is explicit `HOME_ONLY_V1`
with zero correction, 200 Exact novel rows and top-three total `n=5`; it does
not import rollover/comparison/capability rows. After the report boundary,
each extension epoch is a fresh fingerprint and state root with 200 Exact
novel rows, top-three `n=5`, a parent ledger-head receipt, and a deterministic
512-observation active set (32 historical champions, 128 lowest-MAE, 160
recent, then farthest-first maximin coverage). The runner continues only from
durable Home checkpoints and stops on an explicit user stop, protective/safety
fault, source drift, or local free space below 100 GiB.

New lifecycle raw is archived as a cold-verified `.r013life.zst` copy with raw
and compressed SHA/size receipts; old artifacts are not modified or deleted.
The combined weekly report is built only at a writer-release boundary and must
contain real local image assets (`<img src="images/*.png">`) for convergence,
timing, force/MAE/BO evolution, parameter evolution and the Figure-eight
trajectory when those sealed arrays exist. Mac delivery contains only HTML,
images, manifest, checksums and ZIP—not raw lifecycle data.

### Current V4 live stop (2026-08-19)

The first Stage-A anchor can be exact under the coalescing-aware timing
receipt, but the second attempt in each fresh seam-fix root reproduced a
controller/RTDE transition gap of 43--46 ms at `ARM_ENTRY -> CONTACT_SEARCH`.
The packet and sample indices stayed continuous and PATH/Home completed, so the
row is preserved but is not eligible.  Three controller-side prewarm variants
(Home pose/dt prewarm, one stationary `sync`, and Home-pose/2 ms first contact
tick) did not remove the gap.  Stage A is therefore
`BLOCKED_HOST_ARM_TRANSITION_ATTRIBUTION`; the 20 ms hard gate and 460 Hz
cadence gate remain unchanged.  New bounded in-memory RTDE poll/drain,
thread-time and timestamp telemetry plus deterministic falsifiers are now
required before another fresh-context canary.  No V4/B1/B2 proposal or V5
workload may resume from these roots.
