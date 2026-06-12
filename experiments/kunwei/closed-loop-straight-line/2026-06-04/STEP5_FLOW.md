# Step5 Flow

`config/current_stage.json` selects Step5c diagnostic DLS dry-run as the
current runnable joint-space route. Strict TASE RNN is blocked until the PDF
truth contract is closed. Step4f, Step4g, and Step5b remain retained evidence
packages only. Do not infer global current status from this per-step file
without reading the current pointer.

The source of truth for Step5 trajectory and stage ownership is
`config/step5_stage_table.json`.

| stage id | owner | contact | bridge | reference owner | normal filter | success condition |
|---|---|---:|---:|---|---|---|
| `step5a_cycloid_no_contact_v3` | TP | false | false | TP | none | Complete 22 s fixed-Z cycloid, final phase 6 rad, with the base-X guard clear and shifted taught start/mid/end physical path gate passing. |
| `step5_contact_cycloid_baseline_v1` | bridge+TP | true | true | bridge | `v31_filtered_live` | Retained Step5b evidence: bridge computes Cartesian twist; TP consumes registers `37..44` as `speedl` command. |
| `step5c_speedj_dryrun_v1` | bridge+TP | false | true | diagnostic DLS solver | none | Diagnostic plumbing gate: bridge computes finite DLS `qdot` from `actual_q`; TP consumes registers `37..44` as `qd0..qd5/cmd_valid/path_time` and executes no-contact `speedj`. This is not RNN. |
| `step5c_joint_rnn_cycloid_v1` | bridge+TP | true | true | none | `v31_filtered_live` | Blocked/quarantined: the old contact route was misnamed DLS, not RNN. Controller package must be stop-only and operator must refuse contact. |
| `step5c_strict_rnn_dryrun_v1` | bridge+TP | false | true | strict TASE RNN | none | Blocked until `config/step5c_tase_paper_truth.json` has no `pending_pdf_verify` fields and strict RNN equations are implemented. |

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

Step5c moves joint-command ownership out of the UR controller's Cartesian
`speedl` path and into the Ubuntu bridge. The currently runnable package is
only a no-contact diagnostic DLS dry-run using
`tools/step5c_dls_joint_solver.py`. It verifies RTDE register plumbing,
`actual_q` parsing, and TP `speedj` execution; it must not be called a TASE RNN
reproduction.

The strict RNN route is blocked:

- source-of-truth config: `config/step5c_tase_paper_truth.json`;
- strict solver gate: `tools/step5c_strict_rnn.py`;
- any `pending_pdf_verify` field means no strict package, no bridge, and no
  contact run.

Diagnostic DLS dry-run defaults:

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
  TP Play from the wrapper.

Archived contact values, not active:

- old `step5c_joint_rnn_cycloid_v1` used `qdot_limit = 0.15 rad/s`,
  `path_cap = 0.004 m/s`, `total_linear_cap = 0.006 m/s`,
  `normal_velocity_cap = 0.003 m/s`, and attitude cap `0.060 rad/s`;
- the implementation was bounded MuJoCo Jacobian least-squares with qdot
  clipping, not paper RNN;
- the package basename is retained only as a stop-only quarantine target.

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

There is no valid Step5c contact bridge command at this time.
`scripts/step5c-joint-rnn-operator.sh` must refuse all modes until strict RNN
paper-truth extraction, offline validation, and a separate contact ladder plan
are complete.
