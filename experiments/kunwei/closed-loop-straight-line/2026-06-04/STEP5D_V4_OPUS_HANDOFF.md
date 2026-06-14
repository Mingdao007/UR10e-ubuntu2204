# Step5d v4 Opus Handoff

Date: 2026-06-14

Purpose: hand off the latest Step5d strict-RNN live-prep changes, run data, and
failure evidence for an independent audit. The current suspicion is that the
RNN/outer-loop implementation or task-frame/sign mapping is wrong, because the
latest run entered Stage 25.0 but immediately lost contact.

## Short Verdict

`step5d_strict_rnn_liveprep_v4` fixed the previous "cannot enter Stage 25.0"
problem. It reached Stage 25.0 for about `0.158 s`.

The run did not fail because of the 100 N force guard. The max force norm was
only `9.306 N`. It failed because contact disappeared during Stage 25.0:
normal load went from about `6.06 N` to `0 N`, the bridge engage gate went
false, `cmd_valid` became `0`, the TP program set stop reason `12`, then
unloaded/retracted.

Therefore the next audit should focus on why the Step5d qdot/RNN line-control
command moves the TCP out of contact or otherwise cannot maintain the normal
force after entering Stage 25.0.

## Latest Commits

Repository root:

`/home/andy/ur10e_ros2_ws`

Experiment directory:

`/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04`

Branch:

`archive/ur10e-materials-20260520-20260602`

Recent commits:

- `f00ab0f Add Step5d liveprep v4 contact window gate`
- `b8680fa Raise Step5d lift skip threshold to four degrees`

## What Changed

### v3 change: 4 deg orientation skip

Goal: do not lift away and re-orient if the first-contact orientation error is
already small.

Changed:

- `tools/build_step5d_liveprep.py`
- regenerated `programs/step5/step5d_strict_rnn_liveprep_v3.*`
- current v4 also inherits this value

Contract:

- `orientation_skip_error_rad = 0.069813` (`4 deg`)
- if orientation error is below this threshold, the TP skips Stage `25.1`
  detach/lift and Stage `25.2` orientation correction.

The latest v4 run confirms this skip happened:

- Stage `25.1`: `0 rows`
- Stage `25.2`: `0 rows`

### v4 change: tolerant 25.3 contact-window gate

Goal: v3's strict force-settle gate was too narrow and prevented entry into
Stage 25.0. v4 changed the Stage 25.3 entry gate to a contact window.

Current v4 gate:

- `2 N <= normal_load <= 15 N`
- `force_norm <= 25 N`
- hold for `0.050 s`
- Stage 25.3 timeout `10.000 s`

Code:

- `tools/kunwei_rtde_bridge.py`
  - `STEP5D_LIVEPREP_STAGE_ID = "step5d_strict_rnn_liveprep_v4"`
  - `step5d_contact_window_ready(...)`
  - `--step5d-qdot-limit-rad-s` default is `0.30`
- `tools/build_step5d_liveprep.py`
  - `PROGRAM_NAME = "step5d_strict_rnn_liveprep_v4"`
  - `STEP5_STAGE_ID = "step5d_strict_rnn_liveprep_v4"`
- `config/step5_stage_table.json`
- `STEP5_FLOW.md`
- `tests/test_step5d_full_chain_sanity.py`
- `tests/test_step5_table_and_contact_architecture.py`

## Controller Package

Program:

`step5d_strict_rnn_liveprep_v4`

Local triplet:

- `programs/step5/step5d_strict_rnn_liveprep_v4.script`
- `programs/step5/step5d_strict_rnn_liveprep_v4.txt`
- `programs/step5/step5d_strict_rnn_liveprep_v4.urp`

Controller target:

`/programs/andyl/kunwei/step5/step5d_strict_rnn_liveprep_v4.urp`

Read-back manifest:

`runs/controller_readback_step5d_strict_rnn_liveprep_v4_20260614_234749/manifest.json`

Read-back status:

`controller read-back verified`

SHA256:

- `.script`: `ad5df2927da70978315000cc138dda9d258bbfcf4ead0c0eaa7924c61304748e`
- `.txt`: `bb76131cab9b12191e5b9b155601781846de48c7b2abb20e430ead1ad02f405f`
- `.urp`: `3e249041e3abb7289b85f43ff4035e78c1706ab1fcfe1b5af45feff04ccaa803`

Safety boundary for upload:

- file deploy only
- no URScript send from Ubuntu
- no program load/start from Ubuntu
- no live bridge during upload/read-back

## Latest Bridge Run

Run directory:

`runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951`

Important files:

- bridge CSV:
  `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/bridge_rtde_500hz.csv`
- Kunwei sensor CSV:
  `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/kunwei_sensor_1khz.csv`
- summary:
  `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/summary.json`
- stage timing:
  `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/stage_frequency_summary.json`
- metadata:
  `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/metadata.json`
- raw frames:
  `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/raw_frames.bin`

Bridge command used:

```bash
printf 'START_STEP4E_LINE_STEP5D_STRICT_RNN_LIVEPREP_V4\n' | \
  STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP' \
  scripts/step5d-liveprep-operator.sh contact-bridge
```

Bridge stop behavior:

- Dashboard showed the TP program running, then stopped.
- Bridge auto-stopped after Dashboard reported the TP program stopped.
- `summary.json` stop reason: `dashboard_program_stopped`
- Kunwei quiet stop passed.
- RTDE reconnects: `0`
- parse errors: `0`

Frequency:

- bridge writes: `15056`
- bridge write rate: `500.4006 Hz`
- RTDE output logging rate: about `500 Hz`

Force/torque:

- `force_norm_stats_n.max = 9.305958857747328`
- `normal_force_stats_n.min = -7.947801994728312`
- `torque_norm_stats_nm.max = 0.3702723278394201`

This confirms the run did not hit the 100 N hard force guard.

## Stage Timeline

From `stage_frequency_summary.json` and CSV `ur_output_double_register_35`.

| Stage | Meaning | Rows / duration | Result |
| --- | --- | ---: | --- |
| `20` | start/wait | `2508 rows` | ok |
| `22` | baseline/wait | `4724 rows` | ok |
| `23` | ready | `620 rows` | ok |
| `24.0` | first far search | `1773 rows`, `3.544 s` | ok |
| `24.2` | first near search | `1967 rows`, `3.932 s` | contact found |
| `25.05` | latch | `3 rows`, `0.004 s` | ok |
| `25.15` | orientation-skip decision | `1 row` | skip path selected |
| `25.3` | contact-window entry gate | `52 rows`, `0.102 s` | passed |
| `25.0` | strict RNN qdot line control | `80 rows`, `0.158 s` | contact lost |
| `26` | unload | `574 rows`, `1.146 s` | stop reason `12` |
| `27` | retract | `2634 rows`, `5.266 s` | completed retract |
| `29` | final | `105 rows` | final stop register `12` |

Stages `25.1` and `25.2` were not executed, which matches the 4 deg
orientation-skip change.

## Key CSV Evidence

### Stage 25.3 passed quickly

Stage `25.3`:

- rows: `52`
- duration: `0.101999 s`
- `_step5d_force_settle_ready`: `1 -> 1`
- `step4e_cmd_valid`: `1 -> 1`
- `_step4e_normal_load_n`: `3.522 -> 6.058 N`
- `force_norm_n`: `3.522 -> 6.059 N`

This means v4's contact-window gate did its job and allowed entry into Stage
25.0.

### Stage 25.0 immediately lost contact

Stage `25.0`:

- rows: `80`
- duration: `0.158000 s`
- `_step5d_engage_gate_ok`: `1 -> 0`
- `_step5d_force_settle_ready`: `1 -> 0`
- `step4e_cmd_valid`: `1 -> 0`
- `ur_output_double_register_30`: remained `0` during Stage 25.0, then became
  `12` at Stage 26
- `ur_output_double_register_31` progress/time: about `0.100 -> 0.154 s`

Contact force:

- `_step4e_normal_load_n`: `6.056 N -> 0.0 N`
- `_step4e_normal_load_n.max`: `7.552 N`
- `normal_force_n`: min `-7.571 N`, last `0.0086 N`
- `force_norm_n`: `6.059 N -> 0.056 N`

TCP pose during Stage 25.0:

- `ur_actual_TCP_pose_0`: `0.487815 -> 0.488281`
- `ur_actual_TCP_pose_1`: `0.129356 -> 0.129812`
- `ur_actual_TCP_pose_2`: `0.017916 -> 0.018489`
- z increased by about `0.57 mm`, consistent with moving out of contact.

Actual TCP speed during Stage 25.0:

- linear components reached about `0.0118`, `0.0123`, `0.0135 m/s`
- angular components reached about `0.0302`, `0.0323`, `0.0050 rad/s`

Solver/debug:

- `_step5d_solver_status = 40.0`
- `_step5d_outer_xdot_norm`: about `4.999`
- `_step5d_outer_xdot_limited_norm`: about `0.0150 -> 0.01546`
- `_step5d_outer_xdot_limiter_active = 1`
- `_step5d_qdot_max_abs_rad_s`: `0 -> 0.30`
- `_step5d_constraint_residual_norm`: `0.015 -> 1.115`
- `_step5d_active_bounds_count`: `0 -> 3`

This is a strong warning sign: even after limiting task velocity to about
`0.015`, the RNN qdot reaches the `0.30 rad/s` cap and the residual grows
quickly.

### Qdot path evidence

Important: TP input registers and TP output echo registers are offset in the
script echo. Do not read `ur_output_double_register_37` as "input register 37".

TP Stage 25.0 reads:

```urscript
local cmd_qd0 = read_input_float_register(37)
local cmd_qd1 = read_input_float_register(38)
local cmd_qd2 = read_input_float_register(39)
local cmd_qd3 = read_input_float_register(40)
local cmd_qd4 = read_input_float_register(41)
local cmd_qd5 = read_input_float_register(42)
local cmd_valid = read_input_float_register(43)
local progress_s = read_input_float_register(44)
speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5], ...)
```

TP echo mapping in `codex_echo_step4e`:

```urscript
write_output_float_register(36, read_input_float_register(37)) # qd0 echo
write_output_float_register(37, read_input_float_register(38)) # qd1 echo
write_output_float_register(38, read_input_float_register(39)) # qd2 echo
write_output_float_register(39, read_input_float_register(40)) # qd3 echo
write_output_float_register(40, read_input_float_register(41)) # qd4 echo
write_output_float_register(41, read_input_float_register(42)) # qd5 echo
write_output_float_register(42, read_input_float_register(43)) # cmd_valid echo
write_output_float_register(43, read_input_float_register(44)) # progress echo
```

Bridge debug columns `_step5c_cmd_qd0..5_rad_s` and the Step4e carrier columns
match during Stage 25.0:

- qd0 max: `0.236153767`
- qd1 max: `0.300000000`
- qd2 max: `0.300000000`
- qd3 max: `0.300000000`
- qd4 max: `0.152454034`
- qd5 min: `-0.119244192`

The qdot register path appears to be active and reaching the TP `speedj`
executor. The problem is likely the generated qdot itself, the task twist
feeding it, the Jacobian frame/sign convention, or RNN dynamics/scaling.

## Current Step5d Implementation Map

Core files to audit:

- `tools/step5d_paper_outer_loop.py`
  - paper-form outer loop
  - force/motion decomposition
  - orientation target/error
  - `force_sign_convention = "step5_step6_positive_normal_load"`
- `tools/step5c_strict_rnn.py`
  - strict TASE Eq.(23) RNN state update
  - `proj_input = J.T @ lambda_state`
  - `sigr(x) = abs(x)^r * sign(x)`
  - returns `theta_dot_state` as qdot
- `tools/kunwei_rtde_bridge.py`
  - calibrated Pinocchio runtime
  - `step5d_tcp_jacobian_base(...)`
  - `step5d_omega_bounds(...)`
  - `compute_step5d_outer_loop(...)`
  - `limit_step5d_live_xdot(...)`
  - register write via `step5c_joint_register_values(...)`
- `tools/step5c_calibrated_kinematics_audit.py`
  - calibrated UR10e kinematic model / Pinocchio backend
  - inferred TCP offset
- `tools/step5d_full_chain_sanity.py`
  - structural offline sanity
- `programs/step5/step5d_strict_rnn_liveprep_v4.script`
  - TP executor
  - Stage 25.0 reads `input_float_register(37..42)` and calls `speedj`

Supporting spec/docs:

- `config/step5c_tase_paper_truth.json`
- `config/step5d_liveprep_solver_gate.json`
- `config/step5_stage_table.json`
- `STEP5_FLOW.md`
- `STEP5D_RNN_MAPPING_REVIEW.md`

## Things Already Excluded By This Run

Not a "bridge did not run" problem:

- bridge wrote at about `500 Hz`
- RTDE output logging was about `500 Hz`
- reconnects `0`
- parse errors `0`

Not a "never entered 25.0" problem:

- Stage 25.0 had `80 rows` and `0.158 s`

Not a "posture skip failed" problem:

- Stage `25.1` and `25.2` were skipped

Not a "100 N force guard" problem:

- max force norm was `9.306 N`

Not an obvious TP qdot executor absence:

- script reads `37..42`
- script sends `speedj([cmd_qd0..cmd_qd5])`
- qdot debug columns reached nonzero values and caps during Stage 25.0

## Main Suspicions For Opus

1. Force normal/sign convention in the Step5d outer loop may still be wrong.

   The implementation uses the Step5/Step6 convention:
   `force_sign_convention = "step5_step6_positive_normal_load"`.
   But the paper decomposition and our force-base/force-tool direction may not
   be using the same normal orientation. If the normal component is inverted,
   the controller can command away from contact.

2. Task twist frame may not match the Jacobian frame.

   `xdot_c` is produced as a 6D base-frame task velocity. The Jacobian is from
   calibrated Pinocchio in base frame via `step5d_tcp_jacobian_base(...)`.
   Audit whether:

   - linear velocity is in the same base frame;
   - angular velocity convention matches Pinocchio's geometric Jacobian;
   - TCP lever-arm correction sign is correct;
   - `J @ qdot = xdot_c` uses the same spatial velocity ordering as expected.

3. The RNN dynamics may be unstable or badly scaled for the live tick.

   Current parameters:

   - `dt ~= 0.002 s`
   - `epsilon = 0.022`
   - `r = 0.2`
   - qdot cap `0.30 rad/s`

   In Stage 25.0:

   - `xdot_c` is limited to about `0.015`
   - qdot hits `0.30 rad/s`
   - active bounds count grows to `3`
   - residual grows to `1.115`

   This suggests an RNN state transient, sign error, frame error, or scaling
   mismatch.

4. The outer loop produces a very large raw task velocity before limiting.

   `_step5d_outer_xdot_norm` is about `4.999`, then limited to `0.015`.
   Audit whether this huge raw norm is physically intended. It may come from:

   - force error scaling;
   - `Md/Bd/kf` implementation;
   - using raw force direction as desired orientation;
   - pose error component in the wrong frame.

5. Engage gate behavior is safe but not root cause.

   v4 Stage 25.0 only commands while contact window is valid. Once normal load
   drops below `2 N`, bridge sets `step5d_result = None`, writes `cmd_valid=0`,
   and TP stops after the command-valid loss limit with stop reason `12`.
   This explains the retraction, but not why contact vanished.

6. RNN state initialization/carryover may matter.

   `theta_dot_state` and `lambda_state` are persistent in
   `StrictTaseRnnSolver`. The solver warms before Stage 25.0, but only Stage
   25.0 is `step5d_joint_line_profile`. Audit whether state should be reset at
   first contact, frozen before 25.0, or initialized differently.

## Exact Questions To Answer

1. For the first 80 rows of Stage 25.0, what is the sign of `J @ qdot` along
   the measured contact normal? Is it moving into the surface or away from it?

2. Is `xdot_c`'s normal-force component signed consistently with the Step5/Step6
   convention and with the paper's `Phi_O / Phi_bar_O` decomposition?

3. Does the calibrated Pinocchio Jacobian produce TCP velocity in the same frame
   and convention as `compute_step5d_outer_loop(...)`?

4. Is the Eq.(23) update stable at `dt=0.002`, `epsilon=0.022`, `r=0.2`, and
   qdot cap `0.30` for this measured `J` and `xdot_c`?

5. Is `proj_input = J.T @ lambda_state` still the intended paper-faithful form
   after applying the live limiter and omega bounds?

6. Should `lambda_state` and `theta_dot_state` reset on entry into Stage 25.0?

7. Why is raw `_step5d_outer_xdot_norm` about `5.0` while the desired line
   velocity is tiny? Is this a force-control velocity term, a unit mismatch, or
   a bug?

8. For the same first Stage 25.0 state, what qdot would a known-safe DLS or
   Cartesian admittance controller produce? Does it keep contact while strict
   RNN moves away?

## Suggested Offline Reproduction For Opus

Use the run CSV and replay the first Stage 25.0 row offline:

1. Load:

   `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/bridge_rtde_500hz.csv`

2. Filter:

   `ur_output_double_register_35 == 25.0`

3. For each Stage 25.0 row, reconstruct:

   - `actual_q[0..5]`
   - `actual_qd[0..5]`
   - `actual_TCP_pose`
   - `actual_TCP_speed`
   - zeroed force/tool force
   - `_step4e_normal_load_n`
   - `_step4e_*normal*` diagnostics
   - qdot command debug columns `_step5c_cmd_qd0..5_rad_s`

4. Recompute:

   - calibrated `J`
   - `xdot_c`
   - limited `xdot_c`
   - RNN `proj_input`, `projected`, `sigr_arg`, `theta_dot_state`,
     `lambda_state`
   - `J @ qdot`
   - projection of `J @ qdot` onto the contact normal

5. Compare against:

   - the logged qdot columns;
   - a simple DLS/min-norm qdot for the same `J` and limited `xdot_c`;
   - the observed TCP z/contact-force change.

## Validation Already Run Before v4 Commit

Before committing and pushing v4:

```bash
python3 -m unittest discover tests
python3 -m py_compile tools/*.py
python3 -m json.tool config/step5_stage_table.json
git diff --check
```

Controller delivery was also read-back verified as listed above.

## Current Status

Step table pair:

- `STEP5_FLOW.md`: updated
- `config/step5_stage_table.json`: updated

Numeric sanity:

- structural/live-prep sanity exists from previous Step5d gating
- latest live run shows the remaining problem is runtime behavior in Stage
  25.0, not package generation

TP package:

- v4 generated
- uploaded
- controller read-back verified

Bridge/run artifact:

- v4 live bridge artifact exists at
  `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951`

Report evidence:

- not complete
- current evidence is failure/debug evidence, not a successful full
  reproduction claim

Recommended next action:

Do not keep loosening TP gates first. Audit the Stage 25.0 qdot/RNN/outer-loop
math offline against the run CSV, especially the contact-normal component of
`J @ qdot`.
