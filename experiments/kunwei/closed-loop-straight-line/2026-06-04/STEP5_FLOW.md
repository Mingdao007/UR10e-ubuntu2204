# Step5 Flow

`config/current_stage.json` selects Step5c as the current joint-space route for
cycloid reproduction. Step4f, Step4g, and Step5b remain retained evidence
packages only. Do not infer global current status from this per-step file
without reading the current pointer.

The source of truth for Step5 trajectory and stage ownership is
`config/step5_stage_table.json`.

| stage id | owner | contact | bridge | reference owner | normal filter | success condition |
|---|---|---:|---:|---|---|---|
| `step5a_cycloid_no_contact_v3` | TP | false | false | TP | none | Complete 22 s fixed-Z cycloid, final phase 6 rad, with the base-X guard clear and shifted taught start/mid/end physical path gate passing. |
| `step5_contact_cycloid_baseline_v1` | bridge+TP | true | true | bridge | `v31_filtered_live` | Retained Step5b evidence: bridge computes Cartesian twist; TP consumes registers `37..44` as `speedl` command. |
| `step5c_speedj_dryrun_v1` | bridge+TP | false | true | bridge joint solver | none | First Step5c live gate: bridge computes finite `qdot` from `actual_q`; TP consumes registers `37..44` as `qd0..qd5/cmd_valid/path_time` and executes no-contact `speedj`. |
| `step5c_joint_rnn_cycloid_v1` | bridge+TP | true | true | bridge joint solver | `v31_filtered_live` | Contact Step5c route: bridge computes desired twist plus force/orientation feedback, solves bounded `qdot`, and TP executes only `speedj` in Stage25 joint-control windows. |

Stage25 Step5c command-register semantics are joint mode:

- `37..42 = qd0..qd5 rad/s`;
- `43 = cmd_valid`;
- `44 = progress/path_time`;
- `45 = force_error_n`;
- `46 = pose_or_orientation_error`;
- `47 = controller_state/solver_status`.

Base force, heartbeat, and guard registers `24..36` are unchanged.

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

Step5c moves IK ownership out of the UR controller's Cartesian `speedl` path
and into the Ubuntu bridge. The bridge reads RTDE `actual_q`, `actual_qd`, TCP
pose, Kunwei force, and the Step5 cycloid reference, then solves a bounded
MuJoCo site-Jacobian least-squares command in `tools/step5c_joint_rnn.py`.

The execution order is fixed:

1. `step5c_speedj_dryrun_v1`: no-contact `speedj` register/IK dry-run.
2. `step5c_joint_rnn_cycloid_v1`: contact cycloid joint-space run.

Dry-run defaults:

- no force term;
- short `12 s` Step5 cycloid subset;
- `qdot_limit = 0.10 rad/s`;
- no contact search, `zero_ftsensor()`, TCP/payload write, TP program load, or
  TP Play from the wrapper.

Contact defaults:

- Step5 cycloid reference and filtered-live normal policy retained;
- force target `5 N`;
- `qdot_limit = 0.15 rad/s`;
- `path_cap = 0.004 m/s`;
- `total_linear_cap = 0.006 m/s`;
- `normal_velocity_cap = 0.003 m/s`;
- attitude cap `0.060 rad/s`;
- Fz, force-norm, torque, stale-heartbeat, `cmd_valid`, progress, and abnormal
  TP stop guards remain active.

Before a Step5c package is uploaded, run the numeric sanity gate and save its
artifact under `runs/step5c_numeric_sanity_<timestamp>/`. If the gate fails,
do not upload and do not start a bridge.

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

Step5c explicit bridge commands:

```bash
STEP5C_CONFIRM='LIVE STEP5C SPEEDJ DRY RUN' \
  scripts/step5c-speedj-dryrun-operator.sh joint-bridge
```

```bash
STEP5C_CONFIRM='LIVE STEP5C JOINT RNN CONTACT RUN' \
  scripts/step5c-joint-rnn-operator.sh contact-bridge
```

The contact command is only valid after the dry-run report is acceptable.
