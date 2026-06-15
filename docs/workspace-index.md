# UR10e Workspace Index

This atlas describes the UR10e lab as an execution workspace plus an external
knowledge vault.

```text
UR10e Lab
|-- Execution Workspace: /home/andy/ur10e_ros2_ws
|   |-- src/                  # live ROS2 packages
|   |-- experiments/          # experiment bundles and bench evidence
|   |-- controller_backups/   # controller snapshots and read-back evidence
|   |-- ft_sensor/            # sensor references and adapter material
|   |-- report/               # polished reports and report assets
|   |-- weekly_meeting/       # meeting-facing outputs
|   |-- docs/                 # atlas, contracts, SOP, workspace index
|   |-- scripts/              # repo-level utilities
|   |-- From_Mac/             # intake and imported historical material
|   `-- build/install/log/    # generated ROS artifacts
`-- Knowledge Vault: /home/andy/ur10e_lab_vault
    |-- reports and summaries
    |-- handoffs
    |-- Obsidian notes
    |-- teaching material
    `-- selected synced evidence
```

## Execution Workspace

| Path | Role | Lifecycle | Contents | Move policy | Entry point / owner doc |
|---|---|---|---|---|---|
| `/home/andy/ur10e_ros2_ws/src/` | Live ROS2 package source | active-code | Bringup, hardware, controllers, interfaces, descriptions, MoveIt config, examples, tests | do-not-move | Package READMEs and source manifests |
| `/home/andy/ur10e_ros2_ws/experiments/` | Experiment bundles and bench evidence | active-experiment, historical-evidence, raw-data | Runs, configs, local programs, scripts, tests, tools, reports | index-only | `experiments/README.md` |
| `/home/andy/ur10e_ros2_ws/controller_backups/` | Controller snapshots and read-back evidence | controller-readback | Controller files copied from or verified against the robot | do-not-move | Local README or manifest when present |
| `/home/andy/ur10e_ros2_ws/ft_sensor/` | Sensor references and adapter material | active-experiment, historical-evidence | Sensor notes, references, adapter files, parser context | mapping-required | Local sensor README files |
| `/home/andy/ur10e_ros2_ws/report/` | Polished reports and assets | report-export | Human-facing reports, plots, exported assets | low-risk-doc-rename | Report index or report README when present |
| `/home/andy/ur10e_ros2_ws/weekly_meeting/` | Meeting-facing outputs | report-export | Weekly report material, generated plots, deck inputs | mapping-required | Meeting generator or README when present |
| `/home/andy/ur10e_ros2_ws/docs/` | Workspace contracts and indexes | active-code | Atlas, SOP, organization rules, archive notes | low-risk-doc-rename | `docs/workspace-organization.md` |
| `/home/andy/ur10e_ros2_ws/scripts/` | Repo-level utilities | active-code | Analysis wrappers, sync helpers, validation scripts | mapping-required | Script `--help`, README, or caller references |
| `/home/andy/ur10e_ros2_ws/From_Mac/` | Imported historical intake | intake | Material copied from another machine before curation | index-only | Add local README before any cleanup |
| `/home/andy/ur10e_ros2_ws/build/`, `/home/andy/ur10e_ros2_ws/install/`, `/home/andy/ur10e_ros2_ws/log/` | ROS generated artifacts | generated-artifact | Build products, installed overlays, colcon logs | generated-ignore | `.gitignore` and ROS build commands |

## Experiment Bundles

`experiments/` is the evidence zone for bench work. A leaf directory may contain
`config/`, `programs/`, `runs/`, `scripts/`, `tests/`, `tools/`, and
`report.md`.

Use experiment-leaf `programs/` for packages that belong to one run or family.
Use `controller_backups/` for controller snapshots and read-back evidence whose
role is controller state preservation rather than a single experiment artifact.

Raw runs are stable evidence by default. Dense leaves should get a local README
or dashboard before any migration is considered.

## Knowledge Vault

| Path | Role | Lifecycle | Contents | Move policy | Entry point / owner doc |
|---|---|---|---|---|---|
| `/home/andy/ur10e_lab_vault` | External knowledge vault | knowledge-vault | Reports, summaries, handoffs, Obsidian notes, teaching material, selected synced evidence | do-not-move | `/home/andy/ur10e_lab_vault/README.md` |
| `/home/andy/ur10e_lab_vault/scripts/sync_from_ur10e_workspace.sh` | Workspace-to-vault sync helper | knowledge-vault | Curated sync path from execution workspace to vault | do-not-move | Script body and vault README |

The vault is not a replacement for the execution workspace. Keep live ROS2
packages, controller packages, raw runs, and generated ROS artifacts in
`/home/andy/ur10e_ros2_ws`; sync only selected knowledge and evidence into the
vault.
