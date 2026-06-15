# Experiments Index

This index describes the current physical layout and the target logical layout
for `experiments/`. It is an index only: current physical paths remain valid
until a later approved migration batch performs `git mv`.

## Current Physical Layout

- `experiments/kunwei/closed-loop-straight-line/2026-06-04/` is the current
  physical home for the active TASE contact reproduction work.
- `experiments/archive/onrobot/` contains retired OnRobot experiment evidence.
- `experiments/zero_drift/` contains UR force zero-drift evidence.
- Date-prefixed experiment folders remain stable historical evidence unless a
  specific old -> new mapping is approved.

## Target Logical Layout

The target organization is task/campaign -> step -> artifact kind ->
timestamped run. The current main campaign target is:

```text
experiments/tase-contact-reproduction/
```

Design documents:

- [Experiments step-centric reorganization](../docs/experiments-step-reorganization.md)
- [Experiments rename map](../docs/experiments-rename-map.md)

## TASE Contact Reproduction Step Map

| Target step | Current physical entry points | Notes |
|---|---|---|
| `step0-bench-io/` | `programs/step0_*`, register echo and bench IO evidence | Bench readiness, register echo, frequency checks. |
| `step1-no-contact-pipeline/` | `programs/step1_*`, `runs/bridge_step1_*` | No-contact full pipeline checks. |
| `step2-contact-baseline/` | `programs/step2*`, `runs/bridge_step2*` | Search, admittance, line, and circle baselines. |
| `step4-contact-scaffold/` | `STEP4E_FLOW.md`, `programs/step4*`, `runs/bridge_step4*` | Contact scaffold and line-entry work. |
| `step5-cycloid-rnn/` | `STEP5_FLOW.md`, `config/step5_stage_table.json`, `programs/step5/`, `runs/*step5*` | Cycloid strict-RNN route; active implementation phase is Step5d v12 liveprep. |
| `step6-eight-shape/` | `STEP6_FLOW.md`, `config/step6_stage_table.json`, `programs/step6/`, `runs/*step6*` | Paper Experiment #2 / eight-shaped reference route. |

## Physical Path Reminder

Do not rewrite references blindly. Until the migration batch is approved, use:

```text
experiments/kunwei/closed-loop-straight-line/2026-06-04/
```

as the active physical path for current Step5/v12 code, TP packages, bridge
scripts, tools, tests, configs, and run evidence.
