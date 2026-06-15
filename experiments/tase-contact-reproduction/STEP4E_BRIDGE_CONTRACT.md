# Step4e Bridge Contract

This file is the execution contract for Step4e bridge/runtime guard values.
`STEP4E_FLOW.md` remains the motion/process table. This file owns the values
that must stay identical across the operator, bridge CLI defaults, generated TP
script guard, and upload/read-back validation.

## Current Package

- TP package: `step4e_seed_normal_loop_v31`
- Local triplet: `programs/step4e_seed_normal_loop_v31.{script,txt,urp}`
- Controller triplet: `/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v31.{script,txt,urp}`
- Bridge profile: `--step4e-version v31`
- Normal follow: `--step4e-normal-follow-mode filtered_live`
- Alpha/min-force: `--step4e-normal-filter-alpha 0.35`, `--step4e-normal-min-force-n 2.0`

## Bridge Trigger Contract

- `line-bridge`: starts the Kunwei/RTDE bridge immediately, then waits up to
  `WAIT_FOR_PLAY_S` for the Teach Pendant program to run.
- `line-bridge-fast`: requires a fresh long-check cache, runs only the short
  loaded-program/safety/no-old-bridge trigger checks, then starts the bridge.
- Late-start behavior: if Dashboard reports the expected program is already
  running, the trigger returns `10` and the operator starts the bridge with
  `already_running=1`.
- `autowatch`: waits for TP Play before starting the bridge. Keep it for manual
  testing, not as the normal "open bridge" trigger.

No trigger in this file authorizes TP Play, program load, URScript send,
`zero_ftsensor()`, payload/TCP writes, or robot motion from Ubuntu.

## Guard Table

| guard | canonical value | owner |
|---|---:|---|
| raw normal guard | `50 N` | TP script, operator `MAX_NORMAL_FORCE_N`, bridge `--max-normal-force-n` |
| force norm guard | `60 N` | TP script, operator `MAX_FORCE_NORM_N`, bridge `--max-force-norm-n` |
| torque norm guard | `3.0 Nm` | TP script, operator `MAX_TORQUE_NORM_NM`, bridge `--max-torque-norm-nm` |

## Parameter Source Of Truth

The guard table above is the source of truth for the current package. These
surfaces must match it before a package is handed off:

- `scripts/step4e-line-v1-operator.sh`
  - `MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}"`
  - `MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}"`
  - `MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}"`
  - bridge invocation passes those values through CLI args
- `tools/kunwei_rtde_bridge.py`
  - `--max-normal-force-n` default remains the conservative generic bridge default unless explicitly set by the operator
  - `--max-force-norm-n` default is `60.0`
  - `--max-torque-norm-nm` default is `3.0`
  - run metadata records the active guard values
- `tools/build_step4e_p0p1_programs.py`
  - generated v31 script contains `force_norm > 60.0`
  - generated v31 script contains `torque_norm > 3.0`
  - generated `.urp` cachedContents contains the same guard lines
- `tools/upload_ur_tp_package.py`
  - local and read-back validation reject current-package guard mismatches
  - failure messages must identify bridge contract mismatch

## Change Protocol

For every bridge/operator/package guard change:

1. Update this contract first.
2. Update operator defaults and bridge CLI defaults.
3. Update TP package generation and upload/read-back validation.
4. Rebuild `step4e_seed_normal_loop_v31.{script,txt,urp}`.
5. Validate local package and `.urp` cachedContents.
6. Upload the exact `.script/.txt/.urp` triplet to
   `/programs/andyl/kunwei/step4/`.
7. Fetch back and verify local/controller/read-back SHA plus semantic guard
   greps.

Do not start the bridge, send URScript, load the TP program, press TP Play, or
command robot motion as part of this contract sync.
