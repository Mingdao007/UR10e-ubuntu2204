# Compute budget v1: full-period qualification lower bound

Development-only, transport-free execution. No physical acceptance, no formal timing qualification and no controller winner claimed. Existing route remains inactive.

`measure.py` reuses the complete native qualification seam fixture (500 entry + 31416 formal ticks) for each of SFC, DSFC and MSFC. `perf_counter_ns` surrounds the actual `CanonicalQualificationControl.step`, including provider rollback snapshots and post-command gates. The robot observation is stationary, the baseline-success state is stubbed, deadlines are disabled in this measurement fixture only, and sample clocks advance synthetically. Device IO, full writer scheduling, evidence collection and disk logging are excluded. Trials are sequential and unpaced on the ordinary host, without CPU isolation. Thus this is a compute lower bound, not proof of 500 Hz performance. Method timing differences are not a scientific ranking.

## Baseline and identified bottleneck

Baseline is 2b8e9597; raw per-tick nanoseconds, summaries and an independently instrumented MSFC profile are retained. All three formal-path p95 values were about 2.85-2.87 ms, with roughly 44% exceeding the 2 ms period. A separate cProfile run attributes 121.4 of 171.0 instrumented seconds to freshness summary generation; three history sorts per tick generated over 1.5 billion conversion iterations. Profile durations are not used as latency measurements.

The baseline process imported modules before the source edit. Its original freshness source is retained. No running process was restarted on an observation timeout. The optimized timing process started only after the baseline/profiling process exited.

## Exact-statistics repair

`SensorFreshnessTracker` now retains an ordered index alongside the unchanged acquisition-order age list. Each new finite age is inserted exactly, and the same linear-interpolation percentile definition is evaluated from that index. Age-band boundaries, stale stops, geometry rejects, fractions, extrema, all original samples and output schema are preserved. No approximation, decimation, rolling window, timestamp refresh, or gate relaxation was introduced. List insertion still has O(n) pointer movement in the worst case and storage remains O(n); this is not a hard realtime data structure.

Prefix equivalence tests cover duplicate and out-of-order ages, all three quantiles, maxima and invalid inputs. Optimized results are stored separately under `exact-order-statistics/`; baseline receipts are not overwritten.

## Remaining limitations

The fixture's all-zero ages are favorable for insertion order. Mixed-age statistics were checked separately below. Exact equivalence on prefixes validates numerical semantics, not worst-case latency. Full execution still requires real receive clocks, transport, measured logging and scheduling evidence. The existing runtime deadline checkpoint also precedes result-dictionary/freshness serialization; its `runtime_wall_s` is not the whole provider/qualification duration. The outer timer here includes that cost. Deadline scope and complete writer timing remain explicit follow-up work before pilot.

Do not infer a need to port controller mathematics from the baseline failure alone: the dominant measured bottleneck was telemetry computation. Native law and QP execution are already present. Reassess the remaining hot path after the exact repair and the complete writer-chain measurement.

## Observed before/after

| Method | Baseline PATH p95 ms | Repaired PATH p95 ms | Repaired PATH p99 ms | Repaired PATH >2ms / 31416 |
|---|---:|---:|---:|---:|
| SFC | 2.863 | 0.731 | 0.753 | 1 |
| DSFC | 2.848 | 0.733 | 0.758 | 1 |
| MSFC | 2.873 | 0.743 | 0.765 | 0 |

Both SFC and DSFC still had a roughly 22-26 ms wall-clock compute-interval outlier (not isolated CPU time). Its cause is not identified; do not suppress it or call the chain realtime-qualified. MSFC had no >2 ms sample in this single fixture run. No inference of superior MSFC latency is warranted.

Isolated exact-statistics tests retain 32000 raw ages each for random, descending and coalesced patterns:

- random: p99 6.742 us, max 42.830 us; final quantiles exactly match historical calculation.
- descending: p99 7.334 us, max 40.475 us; final quantiles exactly match historical calculation.
- coalesced: p99 6.513 us, max 34.965 us; final quantiles exactly match historical calculation.

The descending stream exercises full list insertion shifts; these small empirical costs do not prove a worst-case bound. All three streams preserve the complete acquisition-order samples.

Reproduce with the managed Python environment and a **new** output directory: `measure.py --output /tmp/new-yield-budget --skip-profile` and `check_age_streams.py --output /tmp/new-age-budget`. The scripts refuse to overwrite retained receipts.

## Validation and next diagnostic

`regression-tests.txt`: 189 passed across contact tests, evidence ledger and motion profiles. `freshness-tests.txt`: 11 targeted tests passed. Reproduction scripts compile and diff whitespace checks pass.

The largest SFC outlier occurs at tick 1412 in both baseline and repaired runs; DSFC at tick 14613 in both. These locations are retained in `outlier-locations.json`. The repeatability motivates tracing Python garbage collection and per-thread CPU versus wall time; it does not yet establish the cause. Do not disable collection or suppress outliers to obtain a pass.
