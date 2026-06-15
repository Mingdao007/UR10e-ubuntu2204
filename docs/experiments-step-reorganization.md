# Experiments Step-Centric Reorganization

This document defines the target information architecture for `experiments/`.
It is a planning, index, and mapping document only. It does not authorize
physical moves, `git mv`, code edits, controller uploads, bridge execution, or
Teach Pendant changes.

## Rationale

`experiments/` should be organized by experiment task and step because the
workspace is now driven by a TASE contact reproduction campaign. The most
useful navigation axis is:

```text
experiment task / campaign -> step -> artifact kind -> timestamped run
```

Sensor names, dates, people, and hardware vendors are context or metadata.
They should not define the top-level experiment information architecture.
`kunwei/` is therefore deprecated as a top-level experiment axis: Kunwei is the
current hardware and data-source context, not the experiment task.

`ft_sensor/` is a deprecated root evidence zone in the target taxonomy. Sensor
integration material target-maps into `experiments/sensor-integration/`, while
the current physical `ft_sensor/` paths remain in place until a later approved
migration batch.

## Target Tree

```text
experiments/
  README.md
  index.md
  tase-contact-reproduction/
    README.md
    index.md
    shared/
      config/
      hardware/
        kunwei/
        onrobot/
      scripts/
      tools/
    steps/
      step0-bench-io/
      step1-no-contact-pipeline/
      step2-contact-baseline/
      step4-contact-scaffold/
      step5-cycloid-rnn/
      step6-eight-shape/
    runs/
      step0/
      step1/
      step2/
      step4/
      step5/
      step6/
    archive/
  sensor-integration/
    kunwei-kwr75b/
    archive/
      onrobot-hex-e-v2-3010007655/
      robotiq-ft300s-borrowed-ur10e/
  archive/
    onrobot/
```

## Step Names

| Step path | Meaning |
|---|---|
| `step0-bench-io/` | Register echo, RTDE/Kunwei frequency, no-motion IO validation, bench readiness. |
| `step1-no-contact-pipeline/` | No-contact/dry-run full pipeline checks without binding to a specific path shape. |
| `step2-contact-baseline/` | Contact/search/admittance/line/circle baseline exploration. |
| `step4-contact-scaffold/` | Contact search, normal latch, lift, attitude correction, and line-entry scaffolding. |
| `step5-cycloid-rnn/` | Cycloid task and strict-RNN reproduction route; liveprep is an implementation phase, not the directory name. |
| `step6-eight-shape/` | Paper Experiment #2 / eight-shaped reference route. |

## Migration Rules

- New experiment planning should use task/campaign -> step -> artifact kind ->
  timestamped run.
- `experiments/tase-contact-reproduction/` is the target campaign root for the
  current Step0/1/2/4/5/6 line.
- The current physical path
  `experiments/kunwei/closed-loop-straight-line/2026-06-04/` remains the live
  source of truth until an approved migration batch moves it.
- Step5 uses `step5-cycloid-rnn/`; the active implementation phase is Step5d
  v12 liveprep, and v1-v12 history remains evidence rather than taxonomy.
- Raw CSV/JSON, bridge runs, controller readbacks, and generated reports are
  preserve-first evidence unless a later mapping explicitly approves a move.
- Any future physical migration must stage only intended paths and must not
  rewrite historical run payloads.
