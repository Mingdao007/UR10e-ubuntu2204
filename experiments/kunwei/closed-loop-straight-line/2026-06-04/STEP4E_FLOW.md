# Step4e Flow Table

This table is the source of truth for the Step4e/TASE process. When the
operator changes the process, update this table first, then update generator,
bridge, and operator scripts to match it.

Current package: `step4e_seed_normal_loop_v26`

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

| order | stage | owner | required behavior | motion parameters | bridge/register contract | success condition | abort/stop evidence |
|---:|---|---|---|---|---|---|---|
| 1 | `20.0` | TP + bridge | Wait for fresh Kunwei heartbeat before any motion. | No motion. | Bridge writes base force/heartbeat/guard registers. | Fresh heartbeat and `sensor_ok=1`. | `3` if heartbeat/sensor is not fresh. |
| 2 | `22.0` | TP | One-step entry: move to path entry `X,Y` and target attitude while preserving current TCP `Z`. Do not run a separate attitude `movel`; do not run a fixed-Z pre-search `movel`. | `movel(entry_xy_pose)`, `a=0.030 m/s^2`, `v=0.020 m/s`. | No Step4e command registers consumed for motion. | TCP is at entry `X,Y`, current `Z`, target rotvec. | Guard stop from force/torque/sensor. |
| 3 | `23.0` | TP + bridge | Request bridge rezero after entry. | No motion, short settle. | TP sets output register `34=1`; bridge re-baselines Kunwei zero. | `codex_wait_for_rezero_complete(5.0)` returns true. | `14` if rezero handshake times out. |
| 4 | `24.0` | TP | Far downward search from the actual post-entry Z until TCP reaches roughly `target_initial_z + 30 mm`. Runtime computes the far-search length from the current entry Z. | `speedl([0,0,-0.015,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`; target initial Z comes from the reference contact-start Z. | Bridge latches first contact normal for current v26 when force is sufficient. | Contact trigger or transition to near-search depth. | `8` depth limit, `10` timeout, guard stops. |
| 5 | `24.2` | TP | Near downward search from about `30 mm` above the target initial point. | `speedl([0,0,-0.003,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`; max depth defaults to target initial Z `-12 mm`. | Continue first-contact normal latch. | First contact detected, then convert stop `11` to continue. | `8` depth limit, `10` timeout, guard stops. |
| 6 | `25.05` | bridge | Confirm first-contact normal is latched. | No robot motion command required. | `cmd_valid` must become `1`; latched normal must be available. | Command valid before `1.0 s`. | `12` command timeout. |
| 7 | `25.1` | TP | Lift 30 mm from first contact before attitude correction. | `movel(lift_pose)`, `a=0.030 m/s^2`, `v=0.020 m/s`. | Bridge keeps linear/angular commands zero. | Lift complete. | Guard stops. |
| 8 | `25.2` | bridge + TP | Lifted attitude correction using angular `speedl` only. | `speedl([0,0,0,wx,wy,wz])`, `a=0.300 m/s^2`, `t=0.002 s`; angular limit `0.120 rad/s`; ignore <= `3 deg`; hard stop > `30 deg`. | For v23/v24, input registers `37..39` must be exactly zero; only `40..42` may be nonzero angular command; `cmd_valid=1`. | Orientation error <= `3 deg`, then continue. | `13` if linear registers are nonzero or angular command exceeds limit; `15` if > `30 deg`; `10` timeout. |
| 9 | `24.3` | TP | Second far downward search after lifted attitude correction. | `speedl([0,0,-0.005,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`, up to `40 mm` near threshold. | Bridge keeps first-contact normal locked. | Contact trigger or transition to near-search depth. | `8` depth limit, `10` timeout, guard stops. |
| 10 | `24.4` | TP | Second near downward search. | `speedl([0,0,-0.003,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`, up to `70 mm` total depth. | Bridge keeps first-contact normal locked. | Contact detected, then convert stop `11` to continue. | `8` depth limit, `10` timeout, guard stops. |
| 11 | `25.3` | bridge + TP | Reacquire target normal load before line motion. | Linear `speedl([vx,vy,vz,0,0,0])`, `a=0.300 m/s^2`, `t=0.002 s`; URScript rejects any component > `0.010 m/s`; bridge default target force `5 N`. | `cmd_valid=1`; registers `37..39` carry linear force command; `40..42` zero. | Force error within `0.750 N` for `0.250 s` after `0.200 s` minimum. | `13` over-limit command, `12` command timeout, `10` reacquire timeout. |
| 12 | `25.0` | bridge + TP | Run straight XY line while maintaining latched-normal force. | `speedl([vx,vy,vz,wx,wy,0])`, `a=0.300 m/s^2`, `t=0.002 s`; operator default line speed `0.003 m/s`. | Linear command from force/path controller; angular `wx/wy` allowed, `wz` rejected above `0.005 rad/s`. | End progress hold for `0.100 s`, stop register `1`. | `13` command over-limit, `12` command timeout, `10` line timeout. |
| 13 | `26.0`/`27.0` | TP | Auto-retract on normal completion or recoverable stops. | Short retract `+10 mm Z` at `0.020 m/s`, then home at `0.050 m/s`. | Bridge continues heartbeat/guard echo only. | Program reaches `29.0` final stage with stop reason recorded. | If stopped at `25.2`, classify as stage-contract/SOP mismatch before tuning path/upload. |

## Handoff / Run Gate / Publish Gate

After any `.urp`, `.script`, generator, bridge, or operator change for the
current Step4e/TASE line, finish in this order:

1. Update this flow table first, then update the generator, bridge, operator,
   `.script`, `.txt`, and `.urp` artifacts to match it.
2. Generate/upload the current TP-openable package, then verify controller
   directory, Script-node path, fetched-back `.script`, `.urp`
   `<cachedContents>`, source stamp, and key motion/search/lift/control rules.
3. Show the latest flow directly in the conversation as concise
   Chinese-English paragraphs, not only as a file path.
4. State the current TP-openable `.urp` path and enter the explicit
   waiting-for-bridge state.

While waiting for bridge, only these user replies trigger live bridge action:
`开bridge`, `开 bridge`, single-token `开`, or single-token `1`. These tokens
are not global commands; they only apply after Codex has just stated that it is
waiting for the Step4e bridge trigger.

On a valid bridge trigger:

1. Check that Dashboard loaded program is the current `.urp`, safety mode is
   `NORMAL`, and no Kunwei RTDE bridge process already exists.
2. Start the current version with
   `STEP4E_VERSION=<current> ... step4e-line-v1-operator.sh line-autowatch`
   or the matching current-version wrapper.
3. Confirm the bridge process, run directory, and
   `bridge_rtde_500hz.csv` have started writing before doing Git publishing.
4. In the same turn, commit/push only intended Git changes. Stage only the
   flow, generator/bridge/operator files, current/archived TP packages, and
   related owner-skill/output-rule files needed for this Step4e change.
5. If push is blocked by no upstream, ambiguous repo/branch, suspected
   sensitive material, failed validation, or unsplittable unrelated dirty state,
   report the exact blocker but keep bridge monitoring active.
6. Continue monitoring until the TP program stops, bridge shutdown completes,
   Kunwei quiet-stop evidence is written, and post-run summaries are generated.
