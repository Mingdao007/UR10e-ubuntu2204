# Step5 Flow

Step5 is the active route for trajectory work after Step4f/Step4g. Step4f and
Step4g remain historical evidence packages only.

The source of truth for Step5 trajectory and stage ownership is
`config/step5_stage_table.json`.

| stage id | owner | contact | bridge | reference owner | normal filter | success condition |
|---|---|---:|---:|---|---|---|
| `step5a_cycloid_no_contact_v3` | TP | false | false | TP | none | Complete 22 s fixed-Z cycloid, final phase 6 rad, with the base-X guard clear and shifted taught start/mid/end physical path gate passing. |
| `step5_contact_cycloid_baseline_v1` | bridge+TP | true | true | bridge | `v31_filtered_live` | Bridge computes reference and feedback command; TP consumes registers `37..44` and stops on stale/invalid/over-cap/end/runtime faults. |

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

The contact baseline is not a TP open-loop cycloid player. The bridge reads
the same Step5 table and computes:

- `desired_xy`;
- `desired_vxy`;
- `path_error`;
- `progress`;
- force, normal, and orientation feedback command.

The TP side is executor and guard only. It reads command registers `37..44`,
checks heartbeat, `cmd_valid`, velocity caps, progress/end-hold, and runtime,
then applies `speedl`. It must not embed the cycloid formula as the source of
trajectory truth.

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
