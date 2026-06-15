# UR10e Workspace Organization Contract

This document is the organization contract for `/home/andy/ur10e_ros2_ws`.
It governs future cleanup work, but it does not authorize a whole-tree rename
or any cosmetic movement of existing evidence.

The workspace model is:

```text
UR10e Lab = Execution Workspace + Knowledge Vault
```

- Execution Workspace: `/home/andy/ur10e_ros2_ws`
- Knowledge Vault: `/home/andy/ur10e_lab_vault`

The vault is indexed as an external knowledge layer. It is not merged into the
ROS2 execution workspace. The normal sync direction is workspace -> vault, as
implemented by `/home/andy/ur10e_lab_vault/scripts/sync_from_ur10e_workspace.sh`.

## Architecture Rules

- Treat organization as information architecture, not cosmetic filename work.
- Keep live ROS code package/function-first.
- Keep experiment outputs artifact/evidence-first.
- Use dates, people, and sensor names as leaf-level context or metadata, not
  as the main workspace taxonomy.
- Do not make `kunwei` a special global category. Kunwei and zero_ftsensor may
  be active lifecycle contexts; OnRobot is historical evidence.
- Parent directories should carry domain, date, or run context instead of
  forcing all metadata into long basenames.

## Franka Reference Rule

Use Franka as a source/package boundary reference, not as a whole-workspace
directory template. The useful comparison target is
`/home/andy/franka_ws/src/franka_ros2`, where ROS packages are separated by
domain, for example `franka_bringup/`, `franka_hardware/`, `franka_msgs/`,
`franka_example_controllers/`, and `franka_gazebo/`.

Do not mirror `/home/andy/franka_ros2_ws` or other local Franka workspace roots
as organization examples. Those roots can contain local CSV, PDF, log, and
experiment artifacts; the reusable principle is the official source repo's
package/domain boundary.

For UR10e, apply that principle as follows:

- `src/` is only for ROS package/source code. The current package boundary is
  `src/ur10e_bringup/`; future hardware, controller, message, description, or
  simulation code should become function-domain ROS packages only when that
  split is real.
- `experiments/` is for experiment campaigns, runs, controller packages, raw
  evidence, local experiment scripts/tools/tests, and controller readbacks.
- `docs/` is for workspace contracts, indexes, migration maps, SOPs, and
  organization decisions.
- `report/` is for human-facing report exports and paired report assets.
- `build/`, `install/`, and `log/` are generated ROS workspace artifacts and
  must not be used as organization templates.

Do not turn `experiments/tase-contact-reproduction/` into a ROS package, and do
not move CSV/log data, `.urp`/`.script`/`.txt` controller packages, or readback
evidence into `src/`.

## Move And Rename Gates

- Do not rename the whole tree in one pass.
- Do not move ROS package paths, launch/config/calibration paths, Python import
  paths, raw runs, controller backups, URP/script/txt controller packages,
  vendor backups, archives, generated documentation, `build/`, `install/`, or
  `log/` for cosmetic cleanup.
- Every migration requires an explicit `old -> new` mapping before any file is
  moved or renamed.
- A migration that changes paths must update README files, indexes, Markdown
  links, scripts, tests, skills, context docs, and other exact references in
  the same batch.
- After each migration batch, search for both old and new path tokens to catch
  content-blind partial edits.
- Dense evidence directories should first receive a local README or dashboard;
  do not move raw evidence just to make names prettier.

## Naming Policy

- New user-readable files should generally use lowercase, hyphen-separated,
  concise semantic names.
- Keep role names short when the parent directory already carries context, for
  example `report.md`, `protocol.md`, `summary.json`, and `run-notes.md`.
- Existing historical run folders may keep older names until a specific
  migration is approved.
- Controller-openable packages, readbacks, raw logs, and generated artifacts
  may keep tool-native names when stability matters more than readability.

## Experiment Layout

Use folders to carry repeated context and keep filenames short.

```text
experiments/<domain>/<experiment-family>/<condition>/<yyyy-mm-dd>/
  README.md
  report.md
  protocol.md
  config/
  programs/
  runs/
  scripts/
  tests/
  tools/
```

The active campaign root is currently:

```text
experiments/tase-contact-reproduction/
```

Future cleanup inside that campaign should be mapped as campaign/step/evidence
work, for example `steps/`, `runs/`, `shared/`, and `archive/`. Step-level moves
must have their own mapping and commit boundary; do not mix them with ROS
package cleanup.

Examples:

```text
experiments/onrobot/three-stream/coldstart-drift/2026-05-30/
experiments/onrobot/three-stream/static-capture/2026-05-28/
experiments/ur10e/poweron-readonly/2026-05-20/
experiments/tase/finite-time-mujoco/2026-05-23/
```

Inside a run folder, prefer role names:

```text
report.md
protocol.md
summary.json
run-notes.md
data/raw.csv
plots/fz-overlay.png
diagnostics/dashboard-state.json
```

## Batch Policy

Only Batch 0 is authorized by this document.

| Batch | Status | Surface | Policy |
|---|---|---|---|
| 0 | Completed | Audit, index, naming policy, taxonomy | Document only; no file moves. |
| 1 | Active | Retired OnRobot evidence archive | Approved by `docs/onrobot-archive-migration-20260615.md`; keep original basenames. |
| 2 | Future | Report and assets relationship cleanup | Move paired reports/assets only after reference search. |
| 3 | Future | Experiment family README/index cleanup | Add local indexes; do not migrate raw runs. |
| 4 | Future | Active scripts/package paths | Requires mapping approval, import/glob/reference search, and smoke tests. |

Do not execute Batch 2 or later from this document alone.

## Verification Per Batch

For each approved batch:

```bash
git -C /home/andy/ur10e_ros2_ws status --short --branch
rg -n "OLD_PATH|OLD_BASENAME" /home/andy/ur10e_ros2_ws
python3 /home/andy/codex-private-skills/skills/file-naming-governor/scripts/audit_names.py /home/andy/ur10e_ros2_ws --format markdown --limit 80
```

Then run the local smoke command appropriate to the batch, such as a Markdown
link check for docs, a report build command for report artifacts, or a script
`--help`/dry-run command for scripts.
