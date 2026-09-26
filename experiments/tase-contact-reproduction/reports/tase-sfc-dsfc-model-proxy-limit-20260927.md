# TASE + SFC/DSFC offline model screen — 2026-09-27

## Decision

The 30-attempt model screen is complete and its traces are intact. It does **not** rank SFC versus DSFC for force tracking: all six method/scenario combinations hit the proxy joint-speed boundary on 99.59% of ticks. The predicted force MAE values are retained as model outputs only; neither law is promoted to live use from this run.

## Run and trace checks

- Identity: `TASE_RNN_MATURE` A baseline, `figure8_window60_r013_rate460_v1`, `r013_60_rate460`, 5 N, 60 s.
- Five matched blocks × three scenarios (`plane`, `tangent_pulse`, `normal_pulse`) × two laws = **30 planned, 30 completed, 0 model execution failures**.
- Each SFC/DSFC pair shared its block/scenario noise seed. Feedback was closed-loop only inside the simplified model; candidate states diverged after commands changed the model plant.
- All 30 compressed traces passed SHA-256 verification and contain exactly 30,000 rows. The sealed run is [here](../runs/tase-sfc-dsfc-model-proxy-r013-a-20260927/summary.json), with the generated [model report](../runs/tase-sfc-dsfc-model-proxy-r013-a-20260927/report.md) and traces alongside it.

## Retained proxy outputs

| Scenario | SFC model MAE | DSFC model MAE | DSFC − SFC | SFC / DSFC path RMS | qdot-bound ticks, both |
|---|---:|---:|---:|---:|---:|
| Plane | 0.000165 N | 0.000168 N | +0.0000028 N | 0.133 / 0.027 mm | 99.59% |
| Tangential pulse | 0.001411 N | 0.001731 N | +0.000320 N | 2.487 / 2.494 mm | 99.59% |
| Normal pulse | 0.101922 N | 0.109936 N | +0.008014 N | 16.095 / 15.379 mm | 99.59% |

Positive `DSFC − SFC` means DSFC's proxy MAE is higher. These differences are descriptive, not a physical or statistical superiority claim. The normal pulse is a software disturbance in the model, not a measured push.

## Why the model is not a force-accuracy selector

The simulator uses an identity Jacobian (`J = I6`) and applies a ±0.05 rad/s joint-velocity bound. That proxy maps the same numerical bound directly onto Cartesian velocity, although it is not a calibrated UR10e Jacobian. Its `saturation_ticks` flag means at least one realized proxy `qdot` component is at that bound; it does not mean a measured robot motor saturated. Both methods therefore spend nearly the whole simulated path at this artificial boundary, and their low predicted force errors cannot be compared with KWR75 feedback or the formal 550-bin score.

The model also uses the existing offline adapter's 120 N/m path-restoring term. That value is not approved as a live SFC+TASE setting. The sealed source-trace replay remains useful only for checking the candidate's fixed-Home posture and command feasibility; it has no counterfactual force MAE.

## Next step

Do not dispatch either law based on these MAEs. Continue with the real sealed R31 traces to establish the SFC/DSFC command feasibility limits and isolate force-error episodes. A future closed-loop proxy needs a calibrated, configuration-varying Jacobian and a separately identified contact model; until those inputs exist, use model runs to test software contracts, not force performance. Any live comparison still needs a fresh UR10e/KWR75 gate, 460 Hz timing, complete 60 s PATH, and verified return to the same joint Home.
