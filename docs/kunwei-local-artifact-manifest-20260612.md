# Kunwei Local Artifact Manifest - 2026-06-12

This manifest records local artifacts intentionally kept out of Git after the
UR/Kunwei Step5c current-stage cleanup. The repository tracks small source,
summary, report, and metadata files; raw captures and controller recovery
bundles remain local.

## Current Pointer Evidence

- Current stage: `step5c_joint_rnn_cycloid_v1`.
- Current pointer: `experiments/kunwei/closed-loop-straight-line/2026-06-04/config/current_stage.json`.
- Numeric sanity artifact, local only:
  `experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/step5c_numeric_sanity_20260612_234356/step5c_numeric_sanity.json`.
- Numeric sanity result: overall pass.
- Dry-run case: max `|qdot| = 0.005994541828760564 rad/s`, limit `0.10 rad/s`, no clipping/projection.
- Contact case: max `|qdot| = 0.07485740629384106 rad/s`, limit `0.15 rad/s`, no clipping/projection.
- Controller read-back verified, dry-run:
  `runs/controller_readback_step5c_speedj_dryrun_v1_20260612_234401`.
- Controller read-back verified, contact current:
  `runs/controller_readback_step5c_joint_rnn_cycloid_v1_20260612_234410`.
- Both read-back manifests state: file deploy/read-back only; no URScript send,
  no program load, no program start, no live bridge, and no robot motion.

## Local Raw Data Policy

Ignored in Git:

- `controller_backups/`
- `ft_sensor/kunwei/kwr75b/measurements/**/*.csv`
- `ft_sensor/kunwei/kwr75b/measurements/**/*.bin`
- `ft_sensor/kunwei/kwr75b/measurements/**/*.log`
- `ft_sensor/kunwei/kwr75b/measurements/**/capture.pid`
- `ft_sensor/kunwei/kwr75b/measurements/**/logs/`
- `ft_sensor/kunwei/kwr75b/software/`

Tracked intentionally:

- Kunwei device notes and current state under `ft_sensor/kunwei/kwr75b/`.
- Kunwei evidence README files.
- Kunwei capture tools.
- Small measurement metadata, summaries, and reports.
- Derived report assets under `report/assets/`.

## Local Artifact Sizes

- `ft_sensor/kunwei/kwr75b/measurements`: about `20G`.
- `ft_sensor/kunwei/kwr75b/software`: about `323M`.
- `controller_backups`: about `56M`.

These paths are local recovery/evidence stores. Do not add them to Git unless a
future Git LFS or external archive policy is explicitly selected.
