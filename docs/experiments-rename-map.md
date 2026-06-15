# Experiments Rename Map

This is the migration map for completed root moves and deferred deep
reorganizations. Completed rows record old -> new physical moves; deferred rows
describe later step-level splits that still require a separate approved batch.

Columns:
`old_path | proposed_path | object_type | lifecycle | move_policy | risk | reference_impact | batch | notes`

## Current main campaign

| old_path | proposed_path | object_type | lifecycle | move_policy | risk | reference_impact | batch | notes |
|---|---|---|---|---|---|---|---|---|
| `experiments/kunwei/closed-loop-straight-line/2026-06-04/` | `experiments/tase-contact-reproduction/` | campaign root | active | completed | high | high | 2026-06-15-physical-migration | Root move only; internal layout preserved. |
| `experiments/kunwei/closed-loop-straight-line/2026-06-04/README.md` | `experiments/tase-contact-reproduction/README.md` | campaign index | active | completed | medium | medium | 2026-06-15-physical-migration | Campaign landing page moved with root. |
| `experiments/tase-contact-reproduction/STEP4E_FLOW.md` | `experiments/tase-contact-reproduction/steps/step4-contact-scaffold/STEP4E_FLOW.md` | step doc | active-evidence | deferred | medium | high | future-step-split | Step4 contact scaffold entry point. |
| `experiments/tase-contact-reproduction/STEP5_FLOW.md` | `experiments/tase-contact-reproduction/steps/step5-cycloid-rnn/STEP5_FLOW.md` | step doc | active | deferred | medium | high | future-step-split | Step5d v12 liveprep is implementation phase, not directory name. |
| `experiments/tase-contact-reproduction/STEP6_FLOW.md` | `experiments/tase-contact-reproduction/steps/step6-eight-shape/STEP6_FLOW.md` | step doc | active-planned | deferred | medium | high | future-step-split | Eight-shape reference route. |
| `experiments/tase-contact-reproduction/config/current_stage.json` | `experiments/tase-contact-reproduction/shared/config/current_stage.json` | active config | active | deferred | high | high | future-step-split | Preserve content exactly until import/caller audit. |
| `experiments/tase-contact-reproduction/config/step5_stage_table.json` | `experiments/tase-contact-reproduction/steps/step5-cycloid-rnn/config/step5_stage_table.json` | step config | active | deferred | high | high | future-step-split | Preserve content exactly. |
| `experiments/tase-contact-reproduction/config/step6_stage_table.json` | `experiments/tase-contact-reproduction/steps/step6-eight-shape/config/step6_stage_table.json` | step config | active-planned | deferred | medium | medium | future-step-split | Preserve content exactly. |
| `experiments/tase-contact-reproduction/programs/` | `experiments/tase-contact-reproduction/steps/<matching-step>/programs/` | TP packages | active-evidence | deferred | high | high | future-step-split | Split by filename step prefix only after reference audit. |
| `experiments/tase-contact-reproduction/scripts/` | `experiments/tase-contact-reproduction/shared/scripts/` | experiment scripts | active | deferred | high | high | future-step-split | Move only after import/caller audit. |
| `experiments/tase-contact-reproduction/tools/` | `experiments/tase-contact-reproduction/shared/tools/` | experiment tools | active | deferred | high | high | future-step-split | Move only after import/caller audit. |
| `experiments/tase-contact-reproduction/tests/` | `experiments/tase-contact-reproduction/shared/tests/` | experiment tests | active | deferred | medium | medium | future-step-split | Keep test expectations stable before moving. |
| `experiments/tase-contact-reproduction/runs/controller_readback_*` | `experiments/tase-contact-reproduction/runs/<step>/controller_readback_*` | controller readback evidence | evidence | deferred | high | high | future-step-split | Do not rewrite readback payloads; classify by embedded step token later. |
| `experiments/tase-contact-reproduction/runs/bridge_*` | `experiments/tase-contact-reproduction/runs/<step>/bridge_*` | bridge run evidence | evidence | deferred | high | high | future-step-split | Preserve raw run basenames; classify by embedded step token later. |

## Sensor integration

| old_path | proposed_path | object_type | lifecycle | move_policy | risk | reference_impact | batch | notes |
|---|---|---|---|---|---|---|---|---|
| `ft_sensor/kunwei/kwr75b/` | `experiments/sensor-integration/kunwei-kwr75b/` | sensor integration root | active-evidence | completed | high | high | 2026-06-15-physical-migration | `ft_sensor/` is a deprecated root evidence zone. |
| `ft_sensor/archive/onrobot/` | `experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/` | retired sensor evidence | historical-evidence | completed | medium | high | 2026-06-15-physical-migration | Exact workspace navigation has been remapped; raw provenance may retain old strings. |
| `ft_sensor/archive/robotiq/` | `experiments/sensor-integration/archive/robotiq-ft300s-borrowed-ur10e/` | borrowed sensor evidence | historical-evidence | completed | medium | medium | 2026-06-15-physical-migration | Preserves borrowed UR10e context in target basename. |

## Archived/historical experiment material

| old_path | proposed_path | object_type | lifecycle | move_policy | risk | reference_impact | batch | notes |
|---|---|---|---|---|---|---|---|---|
| `experiments/archive/onrobot/` | `experiments/archive/onrobot/` | retired experiment archive | historical-evidence | do-not-move | low | low | none | Already archived by OnRobot archive migration. |
| `experiments/zero_drift/` | `experiments/zero_drift/` | UR force evidence | historical-evidence | index-only | medium | medium | future-review | Leave stable until a separate zero-drift mapping exists. |
| `experiments/20260523_tase_finite_time_ur10e_mujoco_reproduction/` | `experiments/tase-contact-reproduction/archive/mujoco-reproduction-20260523/` | simulation reproduction evidence | historical-evidence | approval-required | medium | medium | later-git-mv | Candidate archive under the campaign after review. |

## Preserve / index-only evidence

| old_path | proposed_path | object_type | lifecycle | move_policy | risk | reference_impact | batch | notes |
|---|---|---|---|---|---|---|---|---|
| `experiments/20260525_tase_sim_readable_state_backup/` | `experiments/20260525_tase_sim_readable_state_backup/` | mixed support evidence | historical-evidence | index-only | medium | medium | none | Mixed TASE/OnRobot-era evidence; do not move in this campaign batch. |
| `experiments/20260527_ur10e_demo_1_1_local_planar_patch/` | `experiments/20260527_ur10e_demo_1_1_local_planar_patch/` | demo evidence | historical-evidence | index-only | medium | medium | none | Keep demo history stable. |
| `experiments/20260520_ur10e_builtin_force_maxfreq_60s/` | `experiments/20260520_ur10e_builtin_force_maxfreq_60s/` | UR force capture | historical-evidence | index-only | low | low | none | Not part of current TASE step migration. |
| `experiments/20260525_rotation_plane_visualization/` | `experiments/20260525_rotation_plane_visualization/` | visualization evidence | historical-evidence | index-only | low | low | none | Not part of current TASE step migration. |

## Questions for later step split approval

- Confirm whether later physical migration should move the campaign root in
  one commit or split docs/config/programs/runs into separate commits.
- Confirm whether `programs/step3_*` should live under
  `step4-contact-scaffold/` as scaffold evidence or under campaign `archive/`.
- Confirm whether `scripts/`, `tools/`, and `tests/` should remain shared or
  be split by step after import/caller audit.
- Confirm whether generated bridge/controller readbacks should be physically
  moved at all, or only indexed from the new campaign docs.
