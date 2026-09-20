# Historical contact mechanism audit

This is an offline evidence audit built from selected structured reports and
receipts. It does not read raw logs, access hardware, replay a controller, or
claim a new physical result.

Run from the repository root:

```bash
python3 experiments/tase-contact-reproduction/tools/audit_historical_contact_evidence.py \
  --output experiments/tase-contact-reproduction/report/historical-contact-mechanism-audit-v1/evidence.json
```

## Evidence table

| Evidence | Exact source | Reproducible result | Boundary |
| --- | --- | --- | --- |
| Real live attempt `r006` | `runs/yield-sfc-full-20260920T151241Z/dispatch_receipt.json` | First qualification tick rejected; one source-reported sample, filtered normal `2.5483 N`, force norm `2.5595 N`, zero command packets and no PATH. Relief/Home recovery succeeded and remained failed as a trial. | Real attempt, not contact-law evidence. |
| Real stopped preflight | `runs/yield-sfc-full-20260920T151241Z/summary.json` | Separate stopped transport window; raw force norm max `9.0059 N`, max receive gap `13.867 ms`, zero command packets. | Not causal evidence for the later live tick. |
| Real SFC shear | `report/yield-live-transition-v1/scheduler-closeout-contact-result.json`; `qualification-force-walk.json` | `1119` state-21 samples over `2.235969314 s`; XY walk `2.444 mm`; active raw peak `18.7830 N`, corrected peak `20.2460 N`; final signed normal/compensated `16.1545 N`; later stopped raw peak `25.4292 N`. | Source-reported windows; causal wording remains a hypothesis, not a counterfactual isolation. |
| Recorded-input replay | `report/contact-yield-recovery-20260920/open-loop-replay.json` | Parent, exact `1f4c44b4`, and current candidate are compared on identical inputs. | Open-loop controller-law comparison, not a physical-force prediction. |
| Synthetic/model evidence | `report/contact-six-qp-20260917/offline-campaign.json`; `report/yield-normal-v2/forceoff-summary.json` | LAC offline holdout has `25` scheduled/completed rows; campaign has `48` completed LAC trials. | Explicit synthetic contact-plant/model replay; no physical acceptance. |
| Simulation | `report/yield-fair-training-v1/initial-descriptors.json` | SFC/DSFC/MSFC nominal/disturbed force, progress, saturation, path and recovery descriptors are available. | `claim_scope: simulation only`; not physical ranking. |

## Coverage and hypothesis

The selected source scope has real SFC evidence, but no paired physical DSFC,
MSFC, or LAC result was found in the selected source scope. LAC is present in
the offline campaign/holdout index only; this is not a claim that older
physical artifacts do not exist elsewhere.
The requested Aug-20 `100 physical / 78 accepted` pointer was not found in the
bounded review index, ledger, and report paths listed in `evidence.json`; this
is a scoped missing pointer, not proof that the historical dataset does not
exist.

The one bounded candidate is that, after contact, the baseline full residual
can turn tangent shear into tangent command. The existing `1f4c44b4`
baseline-only normal projection and the current qualification zero-tangent
intervention are acknowledged existing repairs, not contributions of this
audit. Shared geometry/normal estimation, task feasibility, and command
admission are not isolated by these rows.

The discriminating paired experiment is parent SFC versus the existing
`1f4c44b4` baseline projection, with the same UR10e/Kunwei platform, Home/task
basis, 5 N figure-eight, entry/PATH, limits, cadence, and recovery policy.
Record first-tick readiness, state-21 duration, raw/corrected force peaks, XY
drift, phase reached, and recovery. If both arms fail readiness, classify the
result as execution/admission and do not rank laws; only a readiness-passing
reduction in drift/force supports the bounded law hypothesis.
