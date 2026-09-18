# Contact benchmark freshness contract

The six-controller contact benchmark uses the SFC-compatible freshness policy:

| observation age | band | runtime behavior |
| --- | --- | --- |
| `0 <= age < 20 ms` | `fresh` | use the observation normally |
| `20 ms <= age < 80 ms` | `held` | use the latest value with zero-order hold and record the delay |
| `age >= 80 ms` or a future/non-finite timestamp | `stale` | fail closed, stop/zero the command, and censor the trial |

The 20 ms boundary is a freshness diagnostic. It is not a requirement that
every sample arrive within 20 ms. The 80 ms boundary is the runtime freshness
stop. The contact outer loop also keeps its independent geometric latency
budget; a command can therefore be rejected before 80 ms when the predicted
position uncertainty exceeds that budget. Such a rejection is recorded as
`geometric_latency_reject`, not as a freshness failure.

The metric accumulator's `max_gap_s` remains a separate coverage rule for
time-weighted force/path metrics. It does not redefine sensor age and it does
not interpolate missing samples.

Controller comparisons use the same capture/session and raw sensor stream for
all laws. Held observations remain eligible when no stale stop occurs. A
trial with a stale stop is retained as failure evidence but is excluded from
the primary performance ranking.

Use `tools/run_contact_freshness_sensitivity.py` with a CSV or JSONL trace that
contains `controller` and `observation_age_s`. Optional columns are
`force_error_n`, `path_error_m`, and `vibration_metric`. The replay reports
the 20/40/60/80 ms cutoffs and only emits a complete ranking when every row for
each controller survives the selected cutoff.
