# SFC initial feasibility stop

Observed UTC: 2026-09-20T04:28:23.873225+00:00

The frozen campaign completed all eight initial SFC nominal/disturbed pairs. Two pairs satisfy both nominal bands and disturbed guards; the registered minimum is three. SFC therefore stops before EI. No failed execution, retry, refunded unit, changed threshold, or changed objective is involved.

| Unit (zero-based) | J (retained) | Failed nominal checks | Failed disturbed checks |
| --- | ---: | --- | --- |
| 0 | 77.453194253 | none | none |
| 1 | 65.647534709 | none | none |
| 2 | 68.051160463 | attitude_rms_ok, path_rms_ok, progress_ok | none |
| 3 | 96.749204503 | attitude_rms_ok, load_mae_ok, load_peak_ok, path_rms_ok | load_min_ok |
| 4 | 102.252015138 | load_mae_ok, load_peak_ok | none |
| 5 | 66.887801052 | load_mae_ok, load_peak_ok, path_rms_ok | none |
| 6 | 152.541288471 | load_mae_ok, load_peak_ok, progress_ok | none |
| 7 | 180.910205272 | load_mae_ok, load_peak_ok, path_rms_ok | progress_ok |

All objective values remain in the ledger. A finite objective does not make an infeasible pair eligible for incumbent selection. Unit 1 is the best feasible initial SFC observation, not a completed 24-pair tuned baseline.

DSFC and MSFC continue under the unchanged registered per-method budget. This asymmetric completion is a campaign outcome, not evidence of proposal superiority. The production validation runner requires the complete three-method training freeze; that freeze cannot be issued for this campaign. Reserved validation cells remain unobserved.

After the remaining training finishes, examine the initial common parameter triples and the empirical feasibility bands before choosing a separately named development protocol. Do not spend a new SFC-only search and combine it with this campaign as an equal-budget comparison. Any new comparison must preserve this campaign and its negative outcome.

Source: progress.json is a transactionally consistent read-only ledger snapshot. Raw artifacts and their hashes remain bound in runs/yield-fair-training-v1/campaign.sqlite. No controller, configuration, hardware, or validation cell was changed by this report.
