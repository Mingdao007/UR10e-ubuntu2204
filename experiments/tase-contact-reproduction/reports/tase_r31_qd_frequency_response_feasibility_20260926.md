# R31 QP target-to-actual frequency diagnostic feasibility

This is an offline sealed-trace diagnostic. The question was whether a conventional Welch cross-spectrum could characterize RTDE `target_qd` to `actual_qd` response on the exact consumed-packet rows. The official published-qdot/actual-qdot gate remains unchanged.

The join retains one RTDE frame per exact consumed PATH packet and requires strictly increasing controller timestamps. It does not interpolate missing rows. A candidate Welch segment needs 1,024 consecutive samples, or 2.048 s at the measured nominal 500 Hz period.

| Attempt | Joined rows | Continuous spans | Median span | Longest span | RTDE gaps | Welch result |
|---|---:|---:|---:|---:|---:|---|
| R31 A1 | 28,564 | 1,397 | 25 rows | 40 rows (80 ms) | 4.89% | Ineligible |
| R31 A2 | 28,845 | 1,159 | 26 rows | 53 rows (106 ms) | 4.01% | Ineligible |
| R31 QP BO-10 | 28,871 | 1,135 | 26 rows | 45 rows (90 ms) | 3.93% | Ineligible |
| R31 QP matched-A | 28,809 | 1,190 | 26 rows | 43 rows (86 ms) | 4.13% | Ineligible |

Every attempt contains frequent intervals longer than 2.5 ms, while the required contiguous Welch window is 2.048 s. The analyzer therefore records `ineligible_no_contiguous_span_long_enough` for all four attempts and emits no frequency-response estimate. Filling the gaps would invent samples and is not justified here.

This rules out the ordinary evenly sampled Welch method for these captures; it does not establish whether QP output changes are filtered by the robot servo. Existing exact-sequence time-domain evidence remains: the QP attempts fail the frozen 0.900 motion gate at 0.840132 and 0.848607, the ±24-row timing scan does not find a corrective lag, and QP J2/J4 setpoint changes are much larger than the RNN A repeats. These associations remain noncausal. A timestamp-aware irregular-sample estimator would be a separate diagnostic and must preserve the official gate and attempt classification.

Machine-readable rows and sealed source hashes: [`tase_r31_qd_frequency_response_20260926.json`](tase_r31_qd_frequency_response_20260926.json). The analysis used no robot communication or motion command.
