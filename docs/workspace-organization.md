# UR10e Workspace Organization Plan

This plan starts the filename cleanup from low-risk areas and future structure
only. It does not authorize a whole-workspace rename.

## Current State

- `/home/andy/ur10e_ros2_ws` is dirty and contains many untracked experiment,
  report, backup, and sensor folders.
- A naming audit reports many findings, but many are in vendor backups,
  controller backups, archives, generated files, and raw measurement runs.
- Those findings are not all actionable. Many paths should stay stable because
  reports, scripts, manifests, tools, or hardware programs may reference them.

## Guardrails

- Do not rename the whole tree in one pass.
- Do not move ROS package directories, launch/config/calibration paths,
  Python modules used by imports, URScript/URP controller programs, raw
  measurement runs, vendor backups, archives, generated documentation, `build/`,
  `install/`, or `log/` as part of cosmetic cleanup.
- For any batch rename, produce an `old -> new` mapping first and update links
  by exact path replacement.
- After each batch, run targeted `rg` searches for old paths and old basenames.
- Historical run folders can keep older names. New structure should govern new
  experiments first.

## Future Experiment Layout

Use folders to carry repeated context and keep filenames short.

```text
experiments/<domain>/<experiment-family>/<condition>/<yyyy-mm-dd>/
  README.md
  report.md
  protocol.md
  data/
  plots/
  diagnostics/
```

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

Avoid repeating all context in every filename once the directory already says
the domain, experiment family, condition, and date.

## Low-Risk First Batches

These are candidates for review before any move happens.

| Batch | Surface | Action | Risk |
|---|---|---|---|
| 1 | New experiments | Use the future layout above for new runs. | Low |
| 2 | Standalone docs | Normalize root/vault docs after link search. First batch completed for `ur10e-lab.md`. | Low |
| 3 | Human reports | Normalize report filenames and matching `report/assets/<report>/` folders together. | Low to medium |
| 4 | Weekly meeting artifacts | Normalize generated meeting report/deck names only if the generator paths are updated. | Medium |
| 5 | Active scripts | Rename only with import/glob/reference search and a smoke command. | Medium |

## Candidate Mapping To Review Later

Do not execute this table without a separate confirmation.

| Current path | Proposed path | Notes |
|---|---|---|
| `UR10e_Lab.md` | `ur10e-lab.md` | Completed in first low-risk batch. |
| `report/onrobot_three_stream_coldstart_drift_20260528.md` | `report/onrobot-three-stream-coldstart-drift-2026-05-28.md` | Move paired assets folder in the same batch. |
| `report/onrobot_three_stream_static_capture_20260528.md` | `report/onrobot-three-stream-static-capture-2026-05-28.md` | Move paired assets folder in the same batch. |
| `weekly_meeting/demo_01_02_weekly.md` | `weekly_meeting/demo-01-02-weekly.md` | Check deck generator references first. |

## Verification Per Batch

For each approved batch:

```bash
rg -n "OLD_PATH|OLD_BASENAME" /home/andy/ur10e_ros2_ws
python3 /home/andy/codex-private-skills/skills/file-naming-governor/scripts/audit_names.py /home/andy/ur10e_ros2_ws --format markdown --limit 80
```

Then run the local smoke command appropriate to the batch, such as a Markdown
link check for docs, a report build command for report artifacts, or a script
`--help`/dry-run command for scripts.
