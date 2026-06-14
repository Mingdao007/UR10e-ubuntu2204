# Step5 Flow

`config/current_stage.json` currently selects no runnable Step5c joint-space
route. The diagnostic DLS dry-run is quarantined after the 2026-06-13 live run
showed wrong XY/Z motion. Step5d is now the named completion target for the
complete strict TASE RNN reproduction, but it is blocked until the PDF truth
contract is closed and the strict RNN implementation exists. Step4f, Step4g,
and Step5b remain retained evidence packages only. Do not infer global current
status from this per-step file without reading the current pointer.

The source of truth for Step5 trajectory and stage ownership is
`config/step5_stage_table.json`. Step5c also has a required offline calibrated
kinematics gate and an offline qdot register path gate in that table; passing
either is evidence only and does not authorize bridge start, TP Play,
controller upload, or contact motion.

| stage id | owner | contact | bridge | reference owner | normal filter | success condition |
|---|---|---:|---:|---|---|---|
| `step5a_cycloid_no_contact_v3` | TP | false | false | TP | none | Complete 22 s fixed-Z cycloid, final phase 6 rad, with the base-X guard clear and shifted taught start/mid/end physical path gate passing. |
| `step5_contact_cycloid_baseline_v1` | bridge+TP | true | true | bridge | `v31_filtered_live` | Retained Step5b evidence: bridge computes Cartesian twist; TP consumes registers `37..44` as `speedl` command. |
| `step5c_speedj_dryrun_v1` | bridge+TP | false | true | none | none | Blocked/quarantined: 2026-06-13 live run showed wrong XY/Z motion from DLS/Jacobian mapping. Controller package must be stop-only and operator must refuse bridge. |
| `step5c_joint_rnn_cycloid_v1` | bridge+TP | true | true | none | `v31_filtered_live` | Blocked/quarantined: the old contact route was misnamed DLS, not RNN. Controller package must be stop-only and operator must refuse contact. |
| `step5c_strict_rnn_dryrun_v1` | bridge+TP | false | true | strict TASE RNN | none | Blocked until `config/step5c_tase_paper_truth.json` has no `pending_pdf_verify` fields and strict RNN equations are implemented. |
| `step5d_strict_rnn_liveprep_v1` | bridge+TP | true | true | strict TASE RNN | `v31_filtered_live` | Live-prep route: reuses Step5b contact scaffold, but Stage 25.0 consumes qdot registers `37..42` and executes `speedj`. Requires controller read-back and explicit bridge trigger; not a completed reproduction claim. |
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
`experiments/20260523_tase_finite_time_ur10e_mujoco_reproduction/assets/mjcf/ur10e_nominal.xml`
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
not contact evidence. Its force sign convention is the retained Step5/Step6
positive normal-load convention (`target_force_n=5.0`, `normal_sign=1.0`).

Latest structural artifact:
`runs/step5d_numeric_sanity_20260614_215555/step5d_numeric_sanity.json`.

## Step5d Live-Prep Package

`step5d_strict_rnn_liveprep_v1` is the first non-quarantine Step5d package
route. It intentionally repeats the Step5b contact-search/latch/lift/25.2/25.3
flow, then switches only Stage 25.0 from Cartesian `speedl` registers to joint
`speedj` qdot registers:

- Teach Pendant target:
  `/programs/andyl/kunwei/step5/step5d_strict_rnn_liveprep_v1.urp`;
- local generator: `tools/build_step5d_liveprep.py`;
- operator wrapper: `scripts/step5d-liveprep-operator.sh`;
- bridge profile: `--step4e-version step5d_strict_rnn_liveprep_v1`;
- register contract: `37..42 = qd0..qd5 rad/s`, `43 = cmd_valid`,
  `44 = path_time_s`, `45 = force_error_n`, `46 = orientation_error`,
  `47 = solver_status`;
- qdot cap: `0.30 rad/s`, `speedj` acceleration `0.300 rad/s^2`;
- force sign convention: retained Step5/Step6 positive normal-load convention.

This live-prep route may be delivered to the controller by upload/read-back
verification. It still does not authorize bridge start, TP program load, TP
Play, `zero_ftsensor()`, or robot motion. Those require the explicit operator
trigger and bench state checks.

Delivery status:

- local triplet: `programs/step5/step5d_strict_rnn_liveprep_v1.{script,txt,urp}`;
- controller triplet:
  `/programs/andyl/kunwei/step5/step5d_strict_rnn_liveprep_v1.{script,txt,urp}`;
- read-back artifact:
  `runs/controller_readback_step5d_strict_rnn_liveprep_v1_20260614_225433/manifest.json`;
- SHA state: local, controller, and fetched-back triplet matched.

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

There is no valid Step5c bridge command at this time. Both
`scripts/step5c-speedj-dryrun-operator.sh` and
`scripts/step5c-joint-rnn-operator.sh` must refuse all live modes until the
joint Jacobian/frame mapping is fixed offline through the calibrated kinematics
gate, the qdot register path is repaired, strict RNN paper-truth extraction is
closed where applicable, numeric sanity passes, and a separate live plan is
explicitly accepted.

There is also no valid Step5d bridge command yet. Step5d is the full RNN
completion target, not a live authorization.
