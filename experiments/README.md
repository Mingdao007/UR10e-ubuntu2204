# UR10e Experiments

This folder is the experiment bundle and bench evidence zone for the UR10e
execution workspace. It is not a cosmetic filename cleanup area.

## Rules

- Every new experiment starts on a new git branch when it changes tracked
  workspace files.
- Every experiment gets an English-named leaf or family path.
- Every experiment leaf should have a `report.md` or a local README/dashboard
  that explains the evidence.
- Raw runs are stable by default. Do not move raw CSV, logs, plots, controller
  packages, or readbacks only to make names prettier.
- Dense leaves should first get a local README or dashboard before file moves
  are proposed.
- ROS build artifacts stay out of git through the repository `.gitignore`.

## Leaf Layout

New experiment leaves should use folders to carry context:

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

Use `programs/` inside an experiment leaf for controller packages that belong
to that experiment. Use `/home/andy/ur10e_ros2_ws/controller_backups/` for
controller snapshots and read-back evidence whose role is preserving controller
state.

Examples:

```text
experiments/onrobot/three-stream/coldstart-drift/2026-05-30/
experiments/ur10e/poweron-readonly/2026-05-20/
experiments/kunwei/closed-loop-straight-line/2026-06-04/
```

## Lifecycle Notes

- OnRobot material is historical evidence.
- Retired OnRobot experiment bundles live under `archive/onrobot/`.
- Kunwei and zero_ftsensor are lifecycle contexts for active or recent work,
  not global workspace taxonomy.
- Dates, people, and sensor names belong at leaves or in metadata, not as the
  main navigation structure.

Historical experiment folders may keep their existing names until a specific
`old -> new` mapping is approved. Do not move raw runs only to normalize names.
