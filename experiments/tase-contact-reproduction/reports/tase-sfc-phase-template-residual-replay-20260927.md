# TASE+SFC phase-template residual replay — 2026-09-27

## Finding

Direct SFC driven by the measured tangential KWR75 signal opposes the Figure-eight path in the existing A traces. A leave-one-A-out phase template explains a repeatable part of that signal. Feeding only the residual to frozen SFC substantially improves the **command's path direction** in fixed-observation replay; it does not establish better force tracking.

The A traces are five already completed, eligible `figure8_window60_r013_rate460_v1` runs. No attempt was repeated. Every source has 550/550 formal bins and 600/600 diagnostic bins, with 60 s PATH and effective rate above 460 Hz.

## Phase-template check

For each held-out attempt, the template is the equal-weight 100 ms mean of the other four attempts' baseline-corrected KWR75 tangent force, transformed TCP-to-base and projected onto the sample tangent plane. Leave-one-out prediction explains **65.6%–78.6%** of held-out tangent-force vector energy (median **72.3%**). The median per-attempt tangent-force norm falls from **0.516 N** to **0.185 N** after subtracting the template; negative alignment with the reference path falls from **93.6%** to **53.5%**.

This supports a repeatable *path-phase-associated* component. It does not identify friction as the cause; sensor bias and other repeatable dynamics may contribute. There were no human pushes, so the residual has not been shown to preserve or identify an external push.

## Frozen SFC+TASE command replay

The same leave-one-out templates drove the existing additive `TASE_RNN_MATURE+SFC` command replay, using the frozen SFC parameters from `config/yield_native_route_v1.json`. The normal force feedback, TASE path command, observation trace, single final TASE qdot realization, and existing limits remained unchanged. The template is learned from the other four A runs and applied over `[0,60)`; each replay covers both 600-bin diagnostic and 550-bin formal windows.

| Measure across five attempts | Median | Range |
|---|---:|---:|
| Total task command cosine with reference path | 0.780 | 0.621–0.900 |
| Total task command pointing along reference path | 93.5% | 86.8%–100% |
| SFC component cosine with reference path | −0.005 | −0.197–0.458 |
| Mean applied SFC tangent speed | 1.82 mm/s | 1.75–2.48 mm/s |
| Mean absolute normal component change vs A qdot | 0.017 mm/s | 0.015–0.020 mm/s |
| Candidate joint-bound violations | 0 | 0–0 |

The SFC component itself is not consistently aligned with the path. The favorable total-command direction comes from retaining TASE's path command and adding a weaker, template-residual SFC component. All five baseline RNN qdot/Jacobian replays match their sealed sources. Candidate SFC caps were not hit; recorded host slew limiting affected 4–9 rows per attempt. This remains an offline feasibility observation, not live timing or physical motion evidence.

The leave-one-out transform is now available in the existing sealed-command replay entry through `--phase-template-file`. All five held-out A traces reproduce their sealed RNN qdot/Jacobian baselines; candidate joint-bound violations remain zero, with 550/550 formal and 600/600 diagnostic bins. Their sealed trace rates are 469.81–481.02 Hz and each has 29,557–30,003 PATH rows. The replay configuration is bound to the shared short-EOAT identity, controller read-back/program/runtime identity, and canonical Autotuner Home profile. All five output reports explicitly say the source trace contains no UR `actual_TCP_force`; force input is Kunwei KWR75 only. The first held-out replay reproduces the earlier exploratory command summary within `2e-15`. This verifies the reusable offline replay path, not live registry availability.

## Decision and evidence boundary

This is a promising **offline path-command** candidate for a later TASE+SFC study. It is not a force-accuracy result: the robot observations were fixed to A, candidate force MAE is unavailable, KWR75 is not independent task-force truth, and no live candidate ran. Do not promote the template or SFC method to live based on this replay alone. A live attempt would need an integrated, source-bound template runtime, fresh package/read-back, a current live gate, one writer, and a full 60 s / 460 Hz / joint-Home attempt; any claim of force improvement requires a paired A comparison.

## Reproducible artifacts

- Five-fold template fit: `runs/tase-r32a-tangent-force-phase-template-cv-20260927.json`
- Five held-out SFC+TASE command replays: `runs/tase-r32a-sfc-phase-template-runtime-replay-bound-20260927/summary.json` and per-fold `report.json` / `candidate_command_trace.jsonl.gz`
- Reusable five-A phase template: `config/tase_sfc_phase_template_a_v1.json`; builder: `tools/build_tase_sfc_phase_template.py`; sealed command replay accepts it with `--phase-template-file`.

The source attempt paths and timeline digests remain in the JSON receipts. The original A receipts and traces were not modified.
