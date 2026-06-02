# UR10e Experiments

This folder is the Obsidian-friendly experiment log for the UR10e bench.

Rules for existing experiment folders:

- Every new experiment starts on a new git branch.
- Every experiment gets an English-named subfolder.
- Every experiment folder must contain a `report.md`.
- Raw CSV and plots should live under `data/` and `plots/`.
- ROS build artifacts stay out of git through the repository `.gitignore`.

Future new experiment folders should use a shallow domain layout:

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
experiments/ur10e/poweron-readonly/2026-05-20/
```

Historical experiment folders may keep their existing names until a specific
batch migration is approved. Do not move raw runs only to make names prettier.

Current experiment:

- `zero_drift/20260518_initial_s00_s01`
