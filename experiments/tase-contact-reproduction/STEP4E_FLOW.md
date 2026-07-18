# Step4e Flow Table

This table is the source of truth for the Step4e/TASE process. When the
operator changes the process, update this table first, then update generator,
bridge, and operator scripts to match it.

Machine-readable version routing is owned by `config/step4e_stage_table.json`
and resolved by `tools/resolve_step4e_route.py`. The route table contains rules
only; run and controller read-back evidence remains under `runs/`.

Current package: `step4e_seed_normal_loop_v31`

Previous conservative filtered-live-normal package: `step4e_seed_normal_loop_v30`

Locked-normal fallback package: `step4e_seed_normal_loop_v29`

Archived failed packages:

- `step4e_line_outerloop_v17` failed on 2026-06-09 when the run hit
  `torque_norm_guard` during line-control startup after contact search.
- `step4e_line_outerloop_v19` failed on 2026-06-09 because it timed out after
  lifted attitude/normal-alignment work and never entered line control.
- `step4e_line_outerloop_v20` failed on 2026-06-09 because it reached lifted
  attitude correction but stopped before force acquire and line control.
- `step4e_seed_normal_loop_v23` failed on 2026-06-12 at stage `25.2` with stop
  register `13` after the v23 scaffold reached the lifted angular stage.
- `step4e_seed_normal_loop_v24` failed on 2026-06-12 because its one-step entry
  scaffold was right but its first far/near search envelope still used a fixed
  `80 mm` far-search transition, causing near search to start too high when
  entry `Z` changed.
- `step4e_seed_normal_loop_v25` failed on 2026-06-12 because it computed the
  first far/near transition dynamically but used the old validated search-start
  datum `0.09835 m` as target initial `Z`, causing near search to start even
  higher.
- `step4e_seed_normal_loop_v26` failed on 2026-06-12 because it used the
  reference path `contact_start_xyz` Z `0.020279919 m` as the first-contact
  datum. Run evidence shows near search started at about `0.05022 m`, never
  saw the v13/v16 force jump, and depth-limited at about `0.00824 m`.
- `step4e_seed_normal_loop_v27` failed on 2026-06-12 after it corrected the
  first near-search Z. Run evidence shows the force jump at about
  `z=0.00797 m`, then stage `25.05` stopped with register `12` because
  first-contact latch `cmd_valid` was not delivered within the TP timeout.
- `step4e_seed_normal_loop_v28` failed on 2026-06-12 after reaching first
  contact and stage `25.05`. The motion parameters were retained, but the
  bridge runtime did not recognize v28 as an angular-speedl seed-normal
  profile, so `step4e_cmd_valid` stayed `0`.

| order | stage | owner | required behavior | motion parameters | bridge/register contract | success condition | abort/stop evidence |
|---:|---|---|---|---|---|---|---|
| 1 | `20.0` | TP + bridge | Wait for fresh Kunwei heartbeat before any motion. | No motion. | Bridge writes base force/heartbeat/guard registers. | Fresh heartbeat and `sensor_ok=1`. | `3` if heartbeat/sensor is not fresh. |
| 2 | `22.0` | TP | One-step entry: move to path entry `X,Y` and target attitude while preserving current TCP `Z`. Do not run a separate attitude `movel`; do not run a fixed-Z pre-search `movel`. | `movel(entry_xy_pose)`, `a=0.030 m/s^2`, `v=0.020 m/s`. | No Step4e command registers consumed for motion. | TCP is at entry `X,Y`, current `Z`, target rotvec. | Guard stop from force/torque/sensor. |
| 3 | `23.0` | TP + bridge | Request bridge rezero after entry. | No motion, short settle. | TP sets output register `34=1`; bridge re-baselines Kunwei zero. | `codex_wait_for_rezero_complete(5.0)` returns true. | `14` if rezero handshake times out. |
| 4 | `24.0` | TP | Far downward search from the actual post-entry Z until TCP reaches roughly `first_contact_z + 20 mm`. Runtime computes the far-search length from current entry Z. | `speedl([0,0,-0.015,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`; first-contact Z is the v13/v16 force-jump evidence, about `0.00802 m`. | Bridge latches first contact normal for current v31 when force is sufficient. | Contact trigger or transition to near-search depth. | `8` depth limit, `10` timeout, guard stops. |
| 5 | `24.2` | TP | Near downward search within about `20 mm` above the force-jump first-contact point. | `speedl([0,0,-0.0025,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`; max depth extends a few millimeters below the evidence contact Z; raw normal guard is `50 N`. | Continue first-contact normal latch. | First contact detected, then convert stop `11` to continue. | `8` depth limit, `10` timeout, guard stops. |
| 6 | `25.05` | bridge | Confirm first-contact normal is latched. | No robot motion command required. | `cmd_valid` must become `1`; latched normal must be available. | Command valid before `1.0 s`. | `12` command timeout. |
| 7 | `25.1` | TP | Lift 20 mm from first contact before attitude correction. | `movel(lift_pose)`, `a=0.030 m/s^2`, `v=0.020 m/s`. | Bridge keeps linear/angular commands zero. | Lift complete. | Guard stops. |
| 8 | `25.2` | bridge + TP | Lifted attitude correction using angular `speedl` only, after input registers `37..39` have settled to zero. | `speedl([0,0,0,wx,wy,wz])`, `a=0.300 m/s^2`, `t=0.002 s`; angular limit `0.120 rad/s`; ignore <= `3 deg`; hard stop > `30 deg`. | For v23..v31, input registers `37..39` must be exactly zero; only `40..42` may be nonzero angular command; `cmd_valid=1`. v31 still uses the locked first-contact normal here. | Orientation error <= `3 deg`, then continue. | `13` if linear registers are nonzero after settle or angular command exceeds limit; `15` if > `30 deg`; `10` timeout. |
| 9 | `24.3` | TP | Second far downward search after lifted attitude correction. | `speedl([0,0,-0.005,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`, up to `40 mm` near threshold. | Bridge keeps first-contact normal locked. | Contact trigger or transition to near-search depth. | `8` depth limit, `10` timeout, guard stops. |
| 10 | `24.4` | TP | Second near downward search. | `speedl([0,0,-0.003,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`, up to `70 mm` total depth. | Bridge keeps first-contact normal locked. | Contact detected, then convert stop `11` to continue. | `8` depth limit, `10` timeout, guard stops. |
| 11 | `25.3` | bridge + TP | Line-entry gate after posture-adjusted second contact. Do not wait for stable `5 N`; rough printed contact surfaces make that condition non-diagnostic. | No motion command from this stage; TP only `sync()`/echo while checking bridge readiness. | `cmd_valid=1`; registers `37..39` must remain zero; bridge resets normal-force integral/velocity before line. | Zero-linear `cmd_valid` holds for `0.100 s`, then enter `25.0`. | `13` if any linear command is nonzero, `12` command/gate timeout, guard stops. |
| 12 | `25.0` | bridge + TP | Run straight XY line while maintaining normal force. v31 changes only the attitude/normal reference here: use friction-projected filtered live normal with direct `alpha=0.35` EMA. If sensor is stale, candidate force is `<2.0 N`, or the candidate is opposite to the previous filtered normal, hold the previous filtered normal. No slew-rate, latch-angle, or candidate-angle gate is used. | `speedl([vx,vy,vz,wx,wy,0])`, `a=0.300 m/s^2`, `t=0.002 s`; operator default line speed `0.003 m/s`. | Linear command from force/path controller; angular `wx/wy` allowed, `wz` rejected above `0.005 rad/s`; diagnostics log raw live normal, projected candidate, filtered/control normal, candidate force/angle, and filter source (`filtered_live_alpha`, `hold_low_force`, `hold_reverse`, `hold_stale`, `locked_pre_line`). | End progress hold for `0.100 s`, stop register `1`. | `13` command over-limit, `12` command timeout, `10` line timeout. |
| 13 | `26.0`/`27.0` | TP | Auto-retract on normal completion or recoverable stops. | Short retract `+10 mm Z` at `0.020 m/s`, then home at `0.050 m/s`. | Bridge continues heartbeat/guard echo only. | Program reaches `29.0` final stage with stop reason recorded. | If stopped at `25.2`, classify as stage-contract/SOP mismatch before tuning path/upload. |

## Handoff / Run Gate / Publish Gate

After any `.urp`, `.script`, generator, bridge, or operator change for the
current Step4e/TASE line, finish in this order:

1. Update this flow table first, then update the generator, bridge, operator,
   `.script`, `.txt`, and `.urp` artifacts to match it.
2. Build the current TP-openable package and locally validate the exact
   `.script`, `.txt`, and `.urp` basename triplet.
3. Upload that exact triplet to the controller and fetch it back before
   handoff. This is a cross-machine package deploy plus read-back verification,
   not live bridge, TP Play, program load, robot motion, or Git publishing.
   Use `tools/upload_ur_tp_package.py <program> --target-dir /programs/...`
   so the target directory is explicit.
4. Verify the fetched-back package: local SHA equals controller read-back SHA
   for `.script`, `.txt`, and `.urp`; fetched-back `.urp` gzip-decompresses;
   `URProgram name`, controller `directory`, Script-node path,
   `<cachedContents>` stamp, and key motion/search/lift/control rules match the
   current flow.
5. Report the local source paths and controller target paths separately. A
   `/programs/...` path is only a target path unless read-back validation has
   passed; it is not upload evidence by itself.
6. If upload or read-back is blocked, report `local-only` or `upload blocked`
   with the exact blocker. Do not enter the waiting-for-bridge state until the
   current TP-openable package is `controller read-back verified`.
7. Run any offline bridge replay required for the change after package
   read-back, then show the TP-openable uploaded path.
8. Show the latest flow directly in the conversation as concise
   Chinese-English paragraphs, not only as a file path.
9. State the current TP-openable `.urp` path and enter the explicit
   waiting-for-bridge state only after controller read-back verification.

Check classes before live bridge:

- Version-bound checks: local package validation, controller upload,
  controller read-back SHA, and `.urp` cachedContents are tied to the package
  stamp/SHA. Run them after package changes, not at every trigger.
- Long checks: Ubuntu direct-link, UR controller network, Dashboard/RTDE port
  reachability, SSH helper availability, and Kunwei route checks have a
  `30 min` TTL cache. Refresh them during prep or with `prep-long-checks`.
- Trigger checks: every `开` runs only loaded expected `.urp`, safety `NORMAL`,
  program not already running unless intentionally caught late, and no existing
  bridge process. Target budget is `1-3 s`.

While waiting for bridge, only these user replies trigger live bridge action:
`开bridge`, `开 bridge`, single-token `开`, or single-token `1`. These tokens
are not global commands; they only apply after Codex has just stated that it is
waiting for a bridge trigger. They are step-agnostic: they cover any current
delivered package (step4*, step5*, future steps), contact or no-contact. Never
ask the user for an extra confirmation phrase; operator-internal interlocks
(`STEP5B_CONFIRM=...`, `START_STEP4E_..._V*` stdin prompts) are supplied by
Codex itself in the same command via env assignment or piped stdin.

On a valid bridge trigger:

1. Do not upload packages or repeat package read-back, artifact validation, or
   Git checks at trigger time; those belong to the package handoff above.
2. Run the prepared fast bridge command directly. The operator wrapper owns
   current loaded `.urp`, `NORMAL` safety, no existing bridge, fresh long-check
   cache, bridge process lifecycle, and quiet stop.
3. Start the current version bridge-first with
   `STEP4E_VERSION=<current> ... step4e-line-v1-operator.sh line-bridge-fast`.
   Do not use `line-autowatch` as the main trigger path; it may hide a long
   wait if Dashboard never reports the already-played TP program as running.
4. Confirm the bridge process, run directory, and bridge output have started.
   If a local commit was already prepared, `STEP4E_BACKGROUND_PUSH_AFTER_LIVE=1`
   may run a background `git push`; it must not stage, commit, or inspect diff
   during live monitoring.
5. If push is blocked by no upstream, ambiguous repo/branch, suspected
   sensitive material, failed validation, or unsplittable unrelated dirty state,
   report the exact blocker but keep bridge monitoring active.
6. Continue monitoring until the TP program stops, bridge shutdown completes,
   Kunwei quiet-stop evidence is written, and post-run summaries are generated.
