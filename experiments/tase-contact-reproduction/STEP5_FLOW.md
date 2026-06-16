# Step5 Flow

`config/current_stage.json` currently selects no runnable Step5c joint-space
route. The diagnostic DLS dry-run is quarantined after the 2026-06-13 live run
showed wrong XY/Z motion. Step5d is the named completion target for the
complete strict TASE RNN reproduction. `step5d_strict_rnn_liveprep_v15a` is now
retained live-run evidence after the 2026-06-16 bridge entered TP PLAYING and
Stage25, then stopped by `step5d_contact_safety:hold_duty_limit`. There is no
current Step5d live-prep package after that stop unless a new package is
delivered or an explicit same-version retry decision is recorded. Bridge start,
TP program load/Play, robot motion, payload/TCP writes, and `zero_ftsensor()`
remain separate explicit live gates.
The full reproduction target remains separate and not complete.
Step4f, Step4g, Step5b, and Step5d v1-v15a remain retained evidence packages
only. `step5b_ros2_remote_shadow_v1` is the baseline-first no-motion ROS2
remote-control plumbing candidate and must precede any Step5d live candidate.
`step5d_ros2_remote_shadow_v1` remains diagnostic only for Step5d policy
analysis; it is not a live gate. Neither shadow route is a TP package, live
bridge profile, or current pointer change. Do not infer global current status
from this per-step file without reading the current pointer.

The source of truth for Step5 trajectory and stage ownership is
`config/step5_stage_table.json`. Step5c also has a required offline calibrated
kinematics gate and an offline qdot register path gate in that table; passing
either is evidence only and does not authorize bridge start, TP Play,
controller upload, or contact motion.

| stage id | owner | contact | bridge | reference owner | normal filter | success condition |
|---|---|---:|---:|---|---|---|
| `step5a_cycloid_no_contact_v3` | TP | false | false | TP | none | Complete 22 s fixed-Z cycloid, final phase 6 rad, with the base-X guard clear and shifted taught start/mid/end physical path gate passing. |
| `step5_contact_cycloid_baseline_v1` | bridge+TP | true | true | bridge | `v31_filtered_live` | Retained Step5b evidence: bridge computes Cartesian twist; TP consumes registers `37..44` as `speedl` command. |
| `step5b_ros2_remote_shadow_v1` | ROS2 offline | true | false | ROS2 shadow replay | `v31_filtered_live` input logs | No-motion remote-control plumbing validation before Step5d: replays retained Step5b CSVs, preserves bridge-owned command/reference trace contract, and keeps `cmd_enabled=false`. |
| `step5c_speedj_dryrun_v1` | bridge+TP | false | true | none | none | Blocked/quarantined: 2026-06-13 live run showed wrong XY/Z motion from DLS/Jacobian mapping. Controller package must be stop-only and operator must refuse bridge. |
| `step5c_joint_rnn_cycloid_v1` | bridge+TP | true | true | none | `v31_filtered_live` | Blocked/quarantined: the old contact route was misnamed DLS, not RNN. Controller package must be stop-only and operator must refuse contact. |
| `step5c_strict_rnn_dryrun_v1` | bridge+TP | false | true | strict TASE RNN | none | Blocked until `config/step5c_tase_paper_truth.json` has no `pending_pdf_verify` fields and strict RNN equations are implemented. |
| `step5d_strict_rnn_liveprep_v1` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Evidence route: reached Stage 25.0 and exercised qdot/speedj briefly on 2026-06-14, but stopped after about 0.106 s from TP heartbeat stale. Superseded by v2. |
| `step5d_strict_rnn_liveprep_v2` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Evidence route: reached Stage 25.0 with qdot/speedj, then stopped by force guard after v2 entered 25.0 already preloaded and drove qdot to the 0.30 rad/s cap. Superseded by v3. |
| `step5d_strict_rnn_liveprep_v3` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Evidence route: kept qdot registers `37..42` with TP `speedj`, but the strict 25.3 force-settle gate was too narrow to enter Stage 25.0. Superseded by v4. |
| `step5d_strict_rnn_liveprep_v4` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained failure evidence: entered Stage 25.0 but exposed the Step5d outer-loop force/frame semantic bug. Superseded by v5. |
| `step5d_strict_rnn_liveprep_v5` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained semantic-fix evidence: kept qdot registers `37..42` with TP `speedj`, but reached 25.0 at about 17-21N and was blocked by the v5 2-15N engage gate. Superseded by v6. |
| `step5d_strict_rnn_liveprep_v6` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained failure evidence: widened the entry window to `2-40N`, entered 25.0, then exposed 24.3 re-contact overpressure at about `20-22N`. Superseded by v7. |
| `step5d_strict_rnn_liveprep_v7` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained transition evidence: keeps v5/v6 semantic gate and qdot `speedj` path, restores the `2-15N` entry window, and makes 24.3/24.4 re-contact slow-only before 25.0. Superseded by v8. |
| `step5d_strict_rnn_liveprep_v8` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained failure evidence: v8 made 25.3 an active force-PID settle stage, but low-load dropout below `0.5N` cleared `cmd_valid` and TP stopped with reason `17` before Stage 25.0. Superseded by v9. |
| `step5d_strict_rnn_liveprep_v9` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained failure evidence: 25.3 low-load press recovery fixed the v8 dropout stop, but direct force-PID settle hunted under point contact and timed out before Stage 25.0. Superseded by v10. |
| `step5d_strict_rnn_liveprep_v10` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained failure evidence: 25.3 scalar admittance softened v9 direct PID, but still saturated, flipped sign, and never satisfied the `3-8N` plus settle-speed release window. Superseded by v11. |
| `step5d_strict_rnn_liveprep_v11` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained incomplete evidence: 25.3 deadband acquire released into Stage 25.0, but the live run ended incomplete with Dashboard `PAUSED` and `Safetymode: ROBOT_EMERGENCY_STOP`. Superseded by v12 planning. |
| `step5d_strict_rnn_liveprep_v12` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained read-back evidence: keeps v11 deadband acquire and adds Stage 25.0 low-load/contact-window/TCP-speed guard, `0.050 rad/s` qdot cap, and qdot slew limiting. Superseded by v13 planning before any bridge trigger. |
| `step5d_strict_rnn_liveprep_v13` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained read-back evidence with known P1 gap: actual TCP speed dwell first sample could pass solver before v14. Do not run live. |
| `step5d_strict_rnn_liveprep_v14` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained live-run evidence: first actual TCP speed violation sample holds zero qdot and freezes path time; second sample/`0.004 s` dwell stops; predicted TCP speed stops immediately; TP hard guards are `50/60 N` and 25.3 recovery force stop is `25 N`. 2026-06-15 run stopped by `predicted_tcp_speed_watchdog`; v14 is not current. |
| `step5d_strict_rnn_liveprep_v15` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained controller-readback evidence only: audit found the cage hook was not truly online and bounded hold burden/counters were incomplete. Superseded by v15a before any bridge run. |
| `step5d_strict_rnn_liveprep_v15a` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Retained live-run evidence: online broad AABB TCP cage/braking margin and bounded zero-qdot hold/reacquire were active. The 2026-06-16 bridge entered Stage25 for about `0.998 s` and stopped by `hold_duty_limit`; v15a is not current. |
| `step5d_ros2_remote_shadow_v1` | ROS2 offline | true | false | ROS2 shadow replay | `v31_filtered_live` input logs | Diagnostic-only Step5d policy replay: replays v15a/v14/v11 and Step5b/Step6b CSVs, removes long zero-qdot hold recovery, but is not live-ready and must follow Step5b plumbing validation. |
| `step5d_strict_rnn_reproduction_v1` | bridge+TP | true | true | strict TASE RNN | paper-truth required | Complete-RNN reproduction target. Blocked until paper truth, strict solver, calibrated kinematics, qdot path, numeric sanity, non-quarantine package, controller read-back, and separate live plan all pass. |

## Step5c Calibrated Kinematics Gate

Step5c is still quarantined. The offline kinematics baseline is now:

- calibration YAML: `/home/andy/ur10e_ros2_ws/src/ur10e_bringup/config/ur10e_calibration.yaml`;
- expected calibration hash: `calib_7367377276742883610`;
- URDF source: `/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro`;
- kinematics backend: Pinocchio `base -> tool0` FK and frame Jacobian;
- audit tool: `tools/step5c_calibrated_kinematics_audit.py`;
- reference failed run:
  `runs/bridge_step4e_line_outerloop_step5c_speedj_dryrun_v1_20260613_001228/bridge_rtde_500hz.csv`.

The gate must prove all of these before any Step5c route can be re-enabled:

1. `FK(actual_q)` to RTDE `actual_TCP_pose` differs by a constant active TCP
   offset, inferred as `tool0 +Z ~= 0.122099 m`, with offset std `<0.25 mm`.
2. Active TCP speed from calibrated `J(actual_q) * actual_qd`, including that
   TCP offset, matches RTDE `actual_TCP_speed` with linear/vector RMS `<1e-5`
   and angular/vector RMS `<1e-5`.
3. The qdot register path is repaired and verified end-to-end for registers
   `37..42` before any controller package can consume live joint commands.
4. A fresh `runs/step5c_numeric_sanity_<timestamp>/` artifact passes for the
   exact future route.

The old nominal MuJoCo model
`experiments/archive/legacy/tase-mujoco-reproduction-2026-05-23/assets/mjcf/ur10e_nominal.xml`
is banned for real Step5c IK/Jacobian. It is retained only as a failure
contrast for the quarantined 2026-06-13 dry-run.

Stage25 Step5c command-register semantics are joint mode:

- `37..42 = qd0..qd5 rad/s`;
- `43 = cmd_valid`;
- `44 = progress/path_time`;
- `45 = force_error_n`;
- `46 = pose_or_orientation_error`;
- `47 = controller_state/solver_status`.

Base force, heartbeat, and guard registers `24..36` are unchanged.

## Step5c Qdot Register Path Gate

The qdot register path gate is now an offline code contract, not a live-run
permission. It proves only the carrier mapping between Ubuntu bridge output and
the archived moving TP executor fixture:

- `input_double_register_37..42 = qd0..qd5 rad/s` in exact order;
- `input_double_register_43 = cmd_valid`;
- `input_double_register_44 = path_time_s`;
- `input_double_register_45 = force_error_n`;
- `input_double_register_46 = pose_or_orientation_error`;
- `input_double_register_47 = solver_status`.

The bridge may still use the existing RTDE recipe field names
`step4e_cmd_*` as carriers, but Step5c metadata and CSV debug columns must
label them as qdot carriers. `_step5c_cmd_qd0..5` must match the carrier values
that would be written to registers `37..42`.

The offline proof is:

- `tests.test_step5c_joint.Step5cJointTest.test_step5c_qdot_register_helper_matches_tp_executor_contract`;
- `tests.test_step5c_joint.Step5cJointTest.test_step5c_metadata_marks_step4e_fields_as_qdot_carriers`;
- `tests.test_step5_table_and_contact_architecture.Step5TableAndContactArchitectureTest.test_step5_table_separates_no_contact_and_contact_owners`.

Passing this gate does not make `step5c_speedj_dryrun_v1` or
`step5c_joint_rnn_cycloid_v1` runnable. Both remain stop-only quarantine
programs, and `tools/kunwei_rtde_bridge.py` must still reject those
`--step4e-version` values in `main()`. A future live dry-run needs a separate
calibrated solver integration, fresh numeric sanity artifact, new non-quarantine
package, controller read-back, and explicit live plan.

## Step5a No-Contact Handoff

Teach Pendant target:

```text
/programs/andyl/kunwei/step5/step5a_cycloid_no_contact_v3.urp
```

Boundary:

- no Kunwei bridge;
- no force control;
- no contact search;
- no normal filter;
- no `zero_ftsensor()`;
- no TCP or payload write.

`v1` and `v2` are archived under `programs/step5/step5a/` and must not be used
as active handoff packages.

Motion contract:

- entry is `movel` only;
- cycloid body is `speedl`;
- fixed base Z is `0.029423891 m`;
- duration is `22.0 s`;
- phase law is `phase = 0.272727t`, final phase `6.0 rad`;
- cadence uses `8 ms` hold before `0.100 s`, then `1 ms` hold.

## Step5 Contact Baseline

The Step5b contact baseline is retained evidence, not the active Step5c route.
It is not a TP open-loop cycloid player. The bridge reads the same Step5 table
and computes:

- `desired_xy`;
- `desired_vxy`;
- `path_error`;
- `progress`;
- force, normal, and orientation feedback command.

The TP side is executor and guard only. It reads command registers `37..44`,
checks heartbeat, `cmd_valid`, velocity caps, progress/end-hold, and runtime,
then applies `speedl`. It must not embed the cycloid formula as the source of
trajectory truth.

## Step5c Joint-Space Route

Step5c moves joint-command ownership out of the UR controller's Cartesian
`speedl` path and into the Ubuntu bridge. There is currently no runnable
Step5c joint-space package. The diagnostic DLS dry-run used
`tools/step5c_dls_joint_solver.py`, but the 2026-06-13 live run proved its
MuJoCo Jacobian/frame mapping is not trusted: actual TCP XY/Z diverged from the
small cycloid reference. Do not revive this solver path for a real Step5c run;
the next Step5c joint-space implementation must use the calibrated
URDF/Pinocchio baseline or a separately audited equivalent.

The strict RNN route is blocked:

- source-of-truth config: `config/step5c_tase_paper_truth.json`;
- strict solver gate: `tools/step5c_strict_rnn.py`;
- any `pending_pdf_verify` field means no strict package, no bridge, and no
  contact run.

Archived diagnostic DLS dry-run values, not active:

- no force term;
- short `12 s` Step5 cycloid subset;
- `qdot_limit = 0.20 rad/s`;
- `path_cap = 0.004 m/s`;
- `total_linear_cap = 0.004 m/s`;
- `normal_velocity_cap = 0.0 m/s`;
- attitude cap `0.0 rad/s`;
- `speedj` acceleration `0.300 rad/s^2`;
- command stale/loss watchdog `0.100 s`;
- no contact search, `zero_ftsensor()`, TCP/payload write, TP program load, or
  TP Play from the wrapper;
- controller basename is retained only as a stop-only quarantine target.

Archived contact values, not active:

- old `step5c_joint_rnn_cycloid_v1` used `qdot_limit = 0.15 rad/s`,
  `path_cap = 0.004 m/s`, `total_linear_cap = 0.006 m/s`,
  `normal_velocity_cap = 0.003 m/s`, and attitude cap `0.060 rad/s`;
- the implementation was bounded MuJoCo Jacobian least-squares with qdot
  clipping, not paper RNN;
- the package basename is retained only as a stop-only quarantine target.

Before a Step5c package is uploaded, run the numeric sanity gate and save its
artifact under `runs/step5c_numeric_sanity_<timestamp>/`. If the gate fails,
do not upload and do not start a bridge. Numeric sanity is necessary but not
sufficient: the calibrated kinematics gate and qdot register path gate must
also pass first.

Normal filtering follows the v31 policy:

- latch first-contact normal before line;
- in line/contact stage use filtered live normal;
- `alpha = 0.35`;
- `min_force = 2.0 N`;
- hold the previous filtered normal on stale sensor, low force, or reverse
  normal.

The default contact timing policy is paper-faithful real time: the reference
clock advances with elapsed time and faults stop the run. A virtual-clock or
freeze-on-fault variant must be separately named and recorded before use.

## Step5d Complete RNN Reproduction Route

`step5d_strict_rnn_reproduction_v1` is the completion line for the full
paper-faithful RNN reproduction. It is not a rename of the old
`step5c_joint_rnn_cycloid_v1`; that route is quarantined because it was DLS/IK,
not RNN.

Step5d is complete only if all of the following are true:

1. `config/step5c_tase_paper_truth.json` has no `pending_pdf_verify` fields and
   `strict_rnn_enabled=true`.
2. `tools/step5c_strict_rnn.py` implements the finite-time TASE RNN equations
   with no `NotImplementedError`, no DLS fallback, and no IK fallback.
3. Eq.(23a) is implemented from the visually checked PDF form:
   `projection_input = J.T @ lambda_state`; the old
   `theta_dot_state - J.T @ lambda_state` form is forbidden.
4. The solver exposes audit diagnostics for RNN state, `lambda`,
   projection/saturation, `sigr` exponent, gain matrix, force-motion task, and
   orientation compliance.
5. `tools/step5d_paper_outer_loop.py` implements the paper-form outer loop:
   Eq.(7)/(8)/(16)/(17) force-motion `xdot_p`, Eq.(13)/(14) quaternion
   orientation `xdot_o`, and explicit `xdot_c=[xdot_p; xdot_o]` diagnostics.
6. The solver uses calibrated Pinocchio `base -> tool0` `FK/J(q)`; the old
   nominal MuJoCo model is allowed only as a failure contrast.
7. The qdot register path proof passes for registers `37..47`.
8. A fresh Step5d structural numeric sanity artifact passes for the offline
   chain `outer loop -> calibrated J(q) -> RNN -> registers 37..47`.
   The Step5d implementation qdot cap is `0.30 rad/s`. Force sign convention
   follows the retained Step5/Step6 positive normal-load route, but contact/live
   numeric sanity remains separate.
9. A new non-quarantine Step5d TP package validates locally and is generated
   only after the solver gates pass.
10. Controller upload and read-back SHA/`cachedContents` verification pass.
11. A separate Step5d live dry-run/contact plan is explicitly accepted before
   opening the bridge or pressing TP Play.

Do not mark Step5d complete from calibrated-only, DLS, IK, register-path-only,
or stop-only quarantine evidence. Those can be prerequisites or diagnostics,
but they are not the RNN reproduction.

Current structural sanity scope: `tools/step5d_full_chain_sanity.py` uses the
recorded Step5c dry-run `actual_q/pose`, calibrated Pinocchio `J(q)`, the
paper outer loop, and the strict RNN to prove finite qdot and register ordering.
It uses a synthetic no-contact unit normal and zeroed position error, so it is
not contact evidence. Its force/frame semantics follow
`UR_FORCE_FRAME_CONTRACT.md`: measured force is environment-on-tool reaction;
positive load is `dot(force_base, reaction_normal)`, and posture/press direction
uses `approach_normal = -reaction_normal`.

Latest structural artifact:
`runs/step5d_numeric_sanity_20260614_215555/step5d_numeric_sanity.json`.

## Step5d Live-Prep Package

`step5d_strict_rnn_liveprep_v1` was the first non-quarantine Step5d package
route. The 2026-06-14 live artifact
`runs/step5d_strict_rnn_liveprep_v1_live_20260614_230100` reached contact and
Stage 25.0, then stopped after about 0.106 s with TP final stop reason `2.0`
because the bridge first initialized the calibrated Pinocchio/RNN runtime at
Stage 25.0 and exceeded the TP stale heartbeat limit.

`step5d_strict_rnn_liveprep_v2` is retained as live evidence. The 2026-06-14
run `runs/bridge_step5d_strict_rnn_liveprep_v2_20260614_231720` entered Stage
25.0 for about `0.086 s`, wrote qdot registers through the strict RNN path, and
then stopped on `normal_force_guard` after raw normal force reached about
`-65.5 N`. The root cause was not bridge timeout: 25.3 entered Stage 25.0
already preloaded at about `18 N`, and the unbounded Step5d task velocity drove
qdot to the `0.30 rad/s` cap while TCP speed continued into the contact normal.

`step5d_strict_rnn_liveprep_v3` is retained as evidence. The 2026-06-14 run
`runs/bridge_step5d_strict_rnn_liveprep_v3_20260614_233207` did not enter
Stage 25.0 because the strict 5N-centered 25.3 force-settle gate never became
ready. Its maximum force stayed below the 100N hard guard, but the gate was too
narrow for the observed contact transient.

`step5d_strict_rnn_liveprep_v4` is retained as failure evidence. It reached
Stage 25.0 through the tolerant contact window, but the Step5d outer loop
rebuilt `R_d.z` from the raw force reaction direction. That inverted the
orientation target relative to the Step5b/Step6b contact-search convention:
25.15 saw about `2.1 deg`, while 25.0 computed about `178 deg`.
See `STEP5D_HOLISTIC_ROOTCAUSE_AUDIT.md`.

`step5d_strict_rnn_liveprep_v5` is retained semantic-fix evidence. It proved
the Step5b/Step6b force-frame contract and semantic hard gate were wired into
the live bridge, then reached Stage 25.0 on 2026-06-15 but kept `cmd_valid=0`
because the v5 `2-15N` engage gate rejected the observed `17-21N` normal load.
It is superseded by `step5d_strict_rnn_liveprep_v6`.

`step5d_strict_rnn_liveprep_v6` is retained failure evidence. It kept the v5
force/frame semantic gate and entered Stage 25.0, but the widened `2-40N`
entry window admitted the 24.3 re-contact overpressure: 24.3 never reached
24.4 because the 40 mm near-start was deeper than the about 20 mm re-contact
travel, Stage 25.0 began at about `22N`, qdot hit the `0.30 rad/s` cap, and
contact unloaded to near `0N`. It is superseded by
`step5d_strict_rnn_liveprep_v7`.

`step5d_strict_rnn_liveprep_v7` is retained transition evidence. It kept
the Step5b contact-search/latch scaffold, the v5/v6 force-frame semantic gate,
the qdot `speedj` path, and a slow-only second contact search, but 25.3 was
still a passive contact-window gate. It is superseded by
`step5d_strict_rnn_liveprep_v8`.

`step5d_strict_rnn_liveprep_v8` is retained failure evidence. It made 25.3 an
active force-PID settle stage, but the 2026-06-15 live run
`runs/bridge_step5d_strict_rnn_liveprep_v8_20260615_183351` stopped before
Stage 25.0: low-load dropout to about `0.23N` fell below the v8 `0.5N`
recovery floor, the bridge cleared `cmd_valid`, and TP wrote stop reason `17`
while Dashboard stayed `Safetymode: NORMAL`. It is superseded by
`step5d_strict_rnn_liveprep_v9`.

`step5d_strict_rnn_liveprep_v9` is retained failure evidence. It fixed the
v8 low-load dropout by keeping low/zero load as a press-recovery condition, but
the 2026-06-15 live run `runs/bridge_step5d_strict_rnn_liveprep_v9_20260615_193715`
showed that direct force-PID velocity mapping in Stage 25.3 can hunt under point
contact and time out before Stage 25.0. It is superseded by
`step5d_strict_rnn_liveprep_v10`.

`step5d_strict_rnn_liveprep_v10` is retained failure evidence. It replaced v9
direct force-PID with scalar admittance, but the 2026-06-15 live run
`runs/bridge_step5d_strict_rnn_liveprep_v10_20260615_201707` showed Stage 25.3
still saturated at `+/-0.003m/s`, flipped sign 133 times under point contact,
and never satisfied the `3-8N` plus settle-speed window before Stage 25.0. It
is superseded by `step5d_strict_rnn_liveprep_v11`.

`step5d_strict_rnn_liveprep_v11` is retained incomplete evidence. It kept
the Step5b contact-search/latch scaffold, warms the calibrated
Pinocchio/RNN runtime before Stage 25.0, skips the 20 mm lift and 25.2 attitude
correction when the first-contact orientation error is already `<= 0.069813 rad`,
inherits the v5/v6 force/frame semantic gate, changes the second contact search
after lift/orientation to slow-only 24.3/24.4 re-contact, makes 25.3 a
slew-limited deadband acquire stage for the uneven surface, keeps low-load press
recovery instead of
stopping below `0.5N`, switches Stage 25.0 from Cartesian `speedl`
registers to joint `speedj` qdot registers, and keeps the runtime semantic gate
before RNN/qdot output. The package is no longer current after the
2026-06-15 live run:

- Teach Pendant archive target:
  `/programs/andyl/kunwei/step5/step5d/step5d_strict_rnn_liveprep_v11.urp`;
- local generator: `tools/build_step5d_liveprep.py`;
- operator wrapper: `scripts/step5d-liveprep-operator.sh`;
- bridge profile for evidence replay only:
  `--step4e-version step5d_strict_rnn_liveprep_v11`;
- register contract: Stage 25.3 uses `37..39 = vx/vy/vz m/s` for deadband
  acquire only; Stage 25.0 uses `37..42 = qd0..qd5 rad/s`, `43 = cmd_valid`,
  `44 = path_time_s`, `45 = force_error_n`, `46 = orientation_error`,
  `47 = solver_status`;
- force/frame contract: `UR_FORCE_FRAME_CONTRACT.md`; use reaction normal for
  positive load, use approach normal `-reaction_normal` for posture and press
  direction, and never normalize raw live force to construct `R_d.z`;
- semantic 25.0 gate: contact-search orientation error and Step5d outer-loop
  orientation angle must agree within `5 deg`, otherwise bridge sets
  `cmd_valid=0` and hard-fails before the strict RNN can emit qdot;
- semantic gate scope: this is a replay/regression gate, not a closed-loop
  stability proof. The v4 failure-contrast check must show at least one Stage
  25.0 row where the old logged orientation error is `>=0.9 rad` while the
  fixed outer-loop orientation is `<=0.1 rad`;
- qdot cap: `0.30 rad/s`, `speedj` acceleration `0.300 rad/s^2`;
- second contact search after lift/orientation: `24.3/24.4` uses
  `near_start_depth = 0.000 m`, `max_down = 0.035 m`, and both far/near
  speeds are `2.5 mm/s`; it only re-touches the surface and does not tune force;
- Stage 25.3 deadband acquire: raw `F_n = dot(force_base, reaction_normal)`,
  `F_filtered = EMA(F_n, alpha=0.15)`; if filtered load is `<2N`, command
  press along `approach_normal`; if it is `>15N`, command unload; inside
  `2-15N`, ramp the normal velocity toward zero. The command is capped at
  `0.0012m/s` with `0.012m/s^2` slew limiting;
- Stage 25.3 recovery policy: deadband acquire continues press recovery at low/zero
  load and stops only if raw `normal_load > 40 N` or `force_norm > 100 N`; the
  TP does not enter Stage 25.0 from that hard envelope stop;
- Stage 25.3 release: filtered `2 N <= normal_load <= 15 N`,
  `force_norm <= 25 N`, and bridge `cmd_valid >= 0.5` for `0.150 s` before
  entering Stage 25.0; there is no settle-velocity release gate in v11;
- Stage 25.3 timeout: `10.000 s`; Stage 25.0 remains blocked until the contact
  window passes;
- live bridge limiter: raw Step5d `xdot_c` remains in diagnostics, but the
  value sent to the strict RNN is limited to the existing live caps
  (`step4e_total_linear_limit_m_s` and `step4e_angular_limit_rad_s`);
- hard guards: raw normal `100 N`, force norm `100 N`, torque `3.0 Nm`; `50 N`
  is retained only as an analysis warning threshold;
- force sign convention: retained Step5/Step6 positive normal-load convention;
- live result:
  `runs/bridge_step5d_strict_rnn_liveprep_v11_20260615_204601` entered
  Stage 25.0 for about `1.752 s` after Stage 25.3 ready became true, then the
  program paused with `Safetymode: ROBOT_EMERGENCY_STOP`; bridge summary
  recorded `stop_reason=signal_sigint` after operator safety stop and Kunwei
  quiet stop.

This live-prep route may be delivered to the controller by upload/read-back
verification as retained evidence only. It does not authorize bridge start, TP
program load, TP Play, `zero_ftsensor()`, or robot motion. It is superseded by
the separately planned v12 guarded live-prep package.

`step5d_strict_rnn_liveprep_v12` is retained guarded live-prep evidence generated
from the v11 escape replay. The replay artifact
`runs/step5d_v11_escape_replay_20260615/summary.json` shows Stage 25.3 ran for
about `0.362 s`, Stage 25.0 ran for about `1.752 s`, first low-load contact
loss occurred about `0.046 s` after Stage 25.0 entry, and the v12 guard would
have blocked before both the `0.02 m/s` and `0.05 m/s` TCP speed thresholds.

v12 keeps the v11 contact-search/latch, optional lift/25.2 skip, slow-only
24.3/24.4 re-contact, and 25.3 deadband acquire release gate. Stage 25.0
changes the bridge and package contract:

- retained Teach Pendant target after read-back:
  `/programs/andyl/kunwei/step5/step5d_strict_rnn_liveprep_v12.urp`;
- local generated triplet:
  `programs/step5/step5d_strict_rnn_liveprep_v12.{script,txt,urp}`;
- bridge profile:
  `--step4e-version step5d_strict_rnn_liveprep_v12`;
- qdot cap: `0.050 rad/s`, with qdot slew limited to `0.20 rad/s^2` from zero
  on first Stage 25.0 command;
- Stage 25.0 line guard: bridge clears `cmd_valid` before TP `speedj` when
  `normal_load < 0.5 N`, contact leaves the `1-15 N` and `force_norm <=25 N`
  window for `0.030 s`, or actual TCP speed exceeds `0.050 m/s`;
- v12 remains a retained guarded live-prep package, not a current bridge target
  and not a complete TASE reproduction.

v12 delivery is controller read-back verified:

- source stamp: `2026-06-15T2125HKT_STEP5D_STRICT_RNN_LIVEPREP_V12`;
- controller read-back artifact:
  `runs/controller_readback_step5d_strict_rnn_liveprep_v12_20260615_212614/manifest.json`;
- local/controller/read-back SHA match:
  `.script` `087ac3a40b9bdb1d216f03ffd77e3812a96d10c8207e7a2047fcba7f9046f318`,
  `.txt` `2af1d1150b054b76fa48cb7bd11858d5d36678e7f7a3bf63876e87cdf5349584`,
  `.urp` `ca5c75f17f709fc7ad42da92b017ac3bbb5941a73583a4a95fb414a1eabb7596`;
- fetched-back `.urp` internal gate passed: `URProgram name`,
  `directory=/programs/andyl/kunwei/step5`, `installationRelativePath`,
  Script-node path, `cachedContents` stamp, `STAGE25_GUARD`,
  `local qdot_cap_rad_s = 0.050`, and `speedj([cmd_qd0` all matched;
- `config/current_stage.json` no longer points to v12; v12 is retained
  read-back evidence only.

`step5d_strict_rnn_liveprep_v13` is retained controller-readback evidence only.
It is not a live-current package because actual TCP speed dwell could hold the
first violating sample in the timer while still allowing solver/RNN output.

`step5d_strict_rnn_liveprep_v14` is retained controller-readback and live-run
evidence only. It is not the current live-prep package after the 2026-06-15 run
stopped by the command-side predicted TCP speed watchdog.
It keeps the v11/v12 contact-search scaffold, v11 deadband acquire at Stage
25.3, and the v12 `0.050 rad/s` qdot cap plus `0.20 rad/s^2` qdot slew limit.
Stage 25.0 changes the bridge safety contract:

- local generated triplet:
  `programs/step5/step5d_strict_rnn_liveprep_v14.{script,txt,urp}`;
- controller target after upload/read-back verification:
  `/programs/andyl/kunwei/step5/step5d_strict_rnn_liveprep_v14.urp`;
- bridge profile:
  `--step4e-version step5d_strict_rnn_liveprep_v14`;
- low load does not immediately stop: when `normal_load <0.5 N`, the bridge
  holds `cmd_valid=1`, writes qdot registers `37..42 = 0`, freezes
  `path_time_s/register 44`, and reports `hard_lost_contact_hold` below
  `0.25 N` or `soft_low_contact_hold` from `0.25-0.5 N`;
- actual TCP speed violation does not pass solver: the first violating sample
  holds zero qdot and freezes path time; the second consecutive sample or
  `0.004 s` accumulated dwell sets `stop_request=1` with zero qdot;
- true danger stop also uses input register 28 `stop_request=1` with zero qdot
  when predicted TCP speed immediately exceeds `0.050 m/s`, low-load predicted
  speed immediately exceeds `0.025 m/s`, low-load hold exceeds `0.300 s`, or
  high-window dwell exceeds `0.030 s`;
- TP/global hard guards are raw normal `50 N`, force norm `60 N`, and torque
  `3.0 Nm`; Stage 25.3 recovery force-norm stop is `25 N`;
- force/load semantics remain bound to `UR_FORCE_FRAME_CONTRACT.md`:
  `normal_load_n = dot(force_base, reaction_normal)`, and approach/posture use
  `approach_normal = -reaction_normal`;
- diagnostics include contact-safety state/action/reason, hold/high-window
  timers, actual TCP speed, predicted TCP speed, actual speed dwell time,
  actual speed dwell count, and control-normal angle audits against world-Z and
  TCP-Z.

v14 delivery status:

- source stamp: `2026-06-15T2310HKT_STEP5D_STRICT_RNN_LIVEPREP_V14`;
- local package validation passed with the URP internal gate for program name,
  controller directory, installation path, Script-node path, cached stamp,
  `STAGE25_CONTACT_SAFETY`, `local qdot_cap_rad_s = 0.050`, `speedj([cmd_qd0`,
  `cmd_valid=1 zero-qdot hold`, `first-sample actual speed dwell`,
  `predicted TCP speed`, `actual speed dwell`, and `stop_request`;
- local SHA:
  `.script` `0b7345c532aeebd8035cf2530a6ae78b0b924f68208f0b0a0f3cb88346cf3881`,
  `.txt` `c4f86f76aa8468e874e415761dfced7b9d4bc5d6b4a82d2f373887f7dcb685a7`,
  `.urp` `bf81806fb34cf3c2dc55eb2a062d510fc348c3ca5ecdf2d28f878e1d58712523`;
- controller read-back artifact:
  `runs/controller_readback_step5d_strict_rnn_liveprep_v14_20260615_231304/manifest.json`;
- local/controller/read-back SHA match the local SHA above;
- fetched-back `.urp` internal gate passed: `URProgram name`,
  `directory=/programs/andyl/kunwei/step5`, `installationRelativePath`,
  Script-node path, `cachedContents` stamp, `STAGE25_CONTACT_SAFETY`,
  `local qdot_cap_rad_s = 0.050`, `speedj([cmd_qd0`,
  `cmd_valid=1 zero-qdot hold`, `first-sample actual speed dwell`,
  `predicted TCP speed`, `actual speed dwell`, raw normal `50 N`,
  force norm `60 N`, 25.3 recovery force norm `25 N`, and `stop_request`
  all matched;
- live run artifact:
  `runs/bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v14_20260615_231805/summary.json`;
- live result: TP entered PLAYING with `Safetymode: NORMAL`; bridge ran about
  `41.416 s` and stopped with
  `stop_reason=step5d_contact_safety:predicted_tcp_speed_watchdog`;
- the run stayed below hard force/torque guards: max force norm about
  `12.721 N`, normal force range about `-12.692..3.340 N`, and max torque norm
  about `0.369 Nm`;
- v14 is retained evidence only. It is superseded by v15 and then v15a.

`step5d_strict_rnn_liveprep_v15` is retained controller-readback evidence only.
It was delivered to `/programs/andyl/kunwei/step5/step5d_strict_rnn_liveprep_v15.urp`
with read-back manifest
`runs/controller_readback_step5d_strict_rnn_liveprep_v15_20260616_151843/manifest.json`,
but audit found it should not be run live because the TCP cage was only a hook
and hold burden/counters were not fully bounded in runtime.

`step5d_strict_rnn_liveprep_v15a` is retained live-run evidence. It kept the
v15 permissive recovery goal but made it honest and bounded:

- local generated triplet:
  `programs/step5/step5d_strict_rnn_liveprep_v15a.{script,txt,urp}`;
- controller target after upload/read-back verification:
  `/programs/andyl/kunwei/step5/step5d_strict_rnn_liveprep_v15a.urp`;
- read-back manifest:
  `runs/controller_readback_step5d_strict_rnn_liveprep_v15a_20260616_155322/manifest.json`;
- source stamp: `2026-06-16T1553HKT_STEP5D_STRICT_RNN_LIVEPREP_V15A`;
- SHA match across local/controller/read-back:
  `.script` `cbad5c358dd99617b48a21e16e7b65d971e07c3038c582ae91e2bfcd9306f249`,
  `.txt` `aa3f7192e22a893fe5aed603097f21f6eb5583064bbae3ba289338c967751bc7`,
  `.urp` `8dd90a7fba50d6d2e7b4d9bef2d933881a5523b63a9c91ad42dd2123f7ed4640`;
- offline replay summary:
  `runs/step5d_v15a_permissive_recovery_offline_20260616/summary.json`;
- online cage: broad stage-wise AABB built from successful Step5b/Step6b Stage25
  TCP pose traces, with `0.020 m` padding and braking margin
  `d - (v*tau + v^2/(2a) + model_margin + contact_margin)`;
- runtime Stage25 diagnostics include
  `_step5d_tcp_cage_distance_m`, `_step5d_tcp_cage_braking_margin_m`,
  `_step5d_tcp_cage_signed_distance_m`, `_step5d_tcp_cage_cell_index`,
  `_step5d_tcp_cage_reason`, `_step5d_hold_event_count`,
  `_step5d_consecutive_hold_s`, `_step5d_total_hold_s`,
  `_step5d_hold_duty`, and `_step5d_repeated_hold_count`;
- bounded hold policy from success replay: max consecutive hold `1.200 s`,
  event limit `360`, duty limit `0.400`, repeated-hold limit `120`;
- corrected v11 replay starts at true Stage25 entry; first intervention is
  `early_tcp_escape_recoverable_hold` at active index `17`, before qdot reaches
  the historical `0.300 rad/s` rail and before large TCP escape;
- success replay reports hold duty, max consecutive hold, hold event count, and
  first hold reason by CSV, with no hard stop.
- live run:
  `runs/bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v15a_20260616_160355/summary.json`;
- live result: TP entered PLAYING with `Safetymode: NORMAL`; bridge ran about
  `25.620 s`, Stage25 line control lasted about `0.998 s`, and v15a stopped
  with `stop_reason=step5d_contact_safety:hold_duty_limit`;
- final hold burden at stop: hold duty `0.858`, hold events `10`, consecutive
  hold `0.546 s`, total hold `0.858 s`;
- TCP cage was not exhausted at the stop: final braking margin about
  `0.01435 m`, reason `inside_broad_tcp_cage`;
- hard force/torque guards stayed below limits: max force norm about
  `13.992 N`, normal force range about `-13.967..3.573 N`, and max torque norm
  about `0.370 Nm`.

v15a is not current after the hold-duty stop. A future Step5d bridge needs
either a new current package after analysis or an explicit same-version retry
decision; TP program load/Play, robot motion, payload/TCP writes, and
`zero_ftsensor()` remain separate explicit live gates.

Retained v10-v11 archive delivery status:

- local archive triplets:
  `programs/step5/step5d/step5d_strict_rnn_liveprep_v10.{script,txt,urp}`
  and
  `programs/step5/step5d/step5d_strict_rnn_liveprep_v11.{script,txt,urp}`;
- controller archive triplets:
  `/programs/andyl/kunwei/step5/step5d/step5d_strict_rnn_liveprep_v10.{script,txt,urp}`
  and
  `/programs/andyl/kunwei/step5/step5d/step5d_strict_rnn_liveprep_v11.{script,txt,urp}`;
- v11 source stamp: `2026-06-15T2039HKT_STEP5D_STRICT_RNN_LIVEPREP_V11`;
- v11 semantic gate artifact:
  `runs/ur_contact_semantic_gate_20260615_201109/ur_contact_semantic_gate_summary.json`;
- archive read-back artifacts:
  `runs/controller_readback_step5d_strict_rnn_liveprep_v10_20260615_204852/manifest.json`
  and
  `runs/controller_readback_step5d_strict_rnn_liveprep_v11_20260615_204855/manifest.json`;
- v11 entry-gate replay artifact:
  `runs/step5d_v11_entry_gate_replay_20260615_204005/summary.json`;
- v11 SHA state: local, controller, and fetched-back archive triplet matched
  (`.script` `028b501d2b7249747f3ad314523b1b087c40609ca83942052a22ae1f941d593d`,
  `.txt` `b0882152ba0313060043f060c744d0aa74c86736bad1c03ad5a97658d3f45cd7`,
  `.urp` `27b039030b88841be92837a57f8c0d83eebe2832c80e41a7fdf95262e6431243`);
- controller root cleanup: root v10/v11 triplets were removed after archive
  read-back verification.

Archived v1-v11 delivery status:

- local archive directory: `programs/step5/step5d`;
- controller archive directory: `/programs/andyl/kunwei/step5/step5d`;
- retained versions: `step5d_strict_rnn_liveprep_v1` through
  `step5d_strict_rnn_liveprep_v11`;
- archive read-back artifacts:
  `runs/controller_readback_step5d_strict_rnn_liveprep_v1_20260615_193017`,
  `runs/controller_readback_step5d_strict_rnn_liveprep_v2_20260615_193020`,
  `runs/controller_readback_step5d_strict_rnn_liveprep_v3_20260615_193024`,
  `runs/controller_readback_step5d_strict_rnn_liveprep_v4_20260615_193027`,
  `runs/controller_readback_step5d_strict_rnn_liveprep_v5_20260615_193030`,
  `runs/controller_readback_step5d_strict_rnn_liveprep_v6_20260615_193034`,
  `runs/controller_readback_step5d_strict_rnn_liveprep_v7_20260615_193037`,
  `runs/controller_readback_step5d_strict_rnn_liveprep_v8_20260615_193040`,
  and `runs/controller_readback_step5d_strict_rnn_liveprep_v9_20260615_202010`;
- controller root cleanup: old root v1-v11 triplets were removed after archive
  read-back verification; current root package identity is v12 only.
- bridge/TP Play remain separate explicit operator actions. These read-backs
  verify package delivery only; they do not mark the full reproduction target
  complete.

## Handoff Gate

Before any TP play instruction:

1. Generate and validate local `.script/.txt/.urp`.
2. Upload only the exact package files to the intended controller path.
3. Fetch the controller copies back and compare hashes.
4. Decompress fetched-back `.urp` and verify `URProgram name`, `directory`,
   `installationRelativePath`, Script-node path, and `cachedContents` stamp.
5. Do not load, run, open the bridge, or tell the operator to press Play until
   the read-back gate passes and the live action is explicitly accepted.

## Bridge Trigger

The Step4e trigger vocabulary applies unchanged to Step5, contact stages
included. After Codex states it is waiting for the bridge trigger, any of
`开bridge`, `开 bridge`, single-token `开`, or single-token `1` is a complete
authorization. Codex must not ask the user for any additional confirmation
phrase for any Step5 stage.

On a valid trigger:

1. The first command of the turn is the prepared operator command. Do not
   re-read owner docs, re-validate packages, repeat read-back, or run Git
   checks in the trigger turn.
2. Operator-internal interlocks are supplied by Codex in the same command,
   for example:

   ```bash
   printf 'START_STEP4E_LINE_STEP5B_V1\n' | \
     STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN' \
     scripts/step5b-contact-operator.sh contact-bridge
   ```

3. Long checks (network/Kunwei route) come from the operator's 30-min TTL
   cache. Warm it with `prep-long-checks` once at bench-session start. If the
   cache is stale the operator refreshes it itself; do not add manual checks.
4. Target from user trigger to bridge process start is a few seconds.

Commit hygiene for Step5d live-prep work:

- A live-prep behavior fix, an archive move, and an SOP/documentation guard are
  separate commits. Do not combine them just because the user asked for the
  follow-up while the first change is still in progress.
- If one part needs rollback, the other parts must remain independently
  revertible.

There is no valid Step5c bridge command at this time. Both
`scripts/step5c-speedj-dryrun-operator.sh` and
`scripts/step5c-joint-rnn-operator.sh` must refuse all live modes until the
joint Jacobian/frame mapping is fixed offline through the calibrated kinematics
gate, the qdot register path is repaired, strict RNN paper-truth extraction is
closed where applicable, numeric sanity passes, and a separate live plan is
explicitly accepted.

There is no valid full Step5d reproduction bridge command yet. Step5d remains
the full RNN completion target. `scripts/step5d-liveprep-operator.sh
contact-bridge` is only the explicitly accepted live-prep bridge route after
package read-back; it is not a full reproduction authorization.
