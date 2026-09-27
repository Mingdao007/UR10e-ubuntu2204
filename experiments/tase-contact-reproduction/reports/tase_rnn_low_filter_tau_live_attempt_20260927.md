# TASE-RNN lower control-filter tau: live screening result

## Protocol and identity

One complete `TASE_RNN_MATURE` attempt ran on `r013_60_rate460`: 5 N target,
60.0 s Figure-eight PATH, approved Autotuner joint Home, incumbent
`Md=9.565272137974492`, `Bd=693.6559295653944`, and legacy integral limit
`1.0 N·s`. The single changed control parameter was the PATH normal-filter
time constant, `0.24748737341529164 s`. Qualification and official scoring
remain fixed at `0.35 s`.

The official `[5,60)` score is a 550-bin MAE on the filtered Kunwei KWR75
normal-load channel. The KWR75 is the experiment's feedback sensor, not an
independent contact-force reference.

## Result

| Signal and window | MAE | RMSE | Signed bias | Tail p95 | Coverage |
|---|---:|---:|---:|---:|---:|
| Official filtered KWR75, `[5,60)` 100 ms bins | **1.4143 N** | 1.7311 N | −1.3966 N | 3.1769 N | 550/550 |
| Filtered KWR75, `[0,60)` 100 ms bins | 1.4226 N | 1.7318 N | −1.4052 N | 3.1763 N | 600/600 |
| Filtered KWR75, `[5,60)` per sample | 1.4170 N | 1.7448 N | — | 3.2405 N | 27,478 samples |
| Filtered KWR75, `[0,60)` per sample | 1.4255 N | 1.7455 N | — | 3.2349 N | 29,983 samples |
| Raw corrected KWR75, `[5,60)` per sample | 2.6737 N | 3.2428 N | — | 5.3822 N | 27,478 samples |
| Raw corrected KWR75, `[5,60)` 100 ms bins | 2.4589 N | 3.0345 N | −1.3892 N | 4.9903 N | 550/550 |
| Raw corrected KWR75, `[0,60)` per sample | 2.6876 N | 3.2494 N | — | 5.3660 N | 29,978 samples |
| Raw corrected KWR75, `[0,60)` 100 ms bins | 2.4712 N | 3.0418 N | −1.4035 N | 4.9891 N | 600/600 |

The raw per-sample MAE exceeds the raw 100 ms-bin MAE by 0.2148 N in the
formal window because within-bin variation cancels when samples are averaged.
The PATH control-input signal had a 1.4841 N formal bin MAE and 1.6401 N
per-sample MAE; this is an internal KWR75-derived feedback signal, not another
force reference. The sealed timeline contains all 27,478 formal rows with
control tau `0.24748737341529164 s` and score tau `0.35 s`.
In the raw corrected KWR75 normal channel, 29.22% of formal samples were below
1 N, with a longest below-1-N run of 0.848 s; this is a contact-loss proxy,
not visual proof.

## Live acceptance and recovery

- PATH duration was 60.012 s; the attempt completed with 550/550 formal bins.
- Host successful PATH publication was 499.67 Hz, raw RTDE frames 499.63 Hz,
  Kunwei distinct samples 999.78 Hz, and TP formal consumed echoes 480.59 Hz.
  Every measured layer exceeded the 460 Hz gate. RTDE p99/max interval was
  2/10 ms; TP echo p99/max interval was 4/10 ms.
- There were 1,142 formal host reference sequences without a matching TP echo
  inside the formal window; this count is retained separately from the accepted
  synchronized rate, which passed. It does not alter the 550-bin coverage.
- Automatic joint Home passed: maximum joint error `2.77×10⁻⁵ rad`, final
  joint velocity zero, Safety NORMAL. Dashboard confirmed the program STOPPED.
- Video was captured successfully. No `actual_TCP_force` or
  `get_tcp_force()` appears in the TASE attempt traces; force feedback and
  metrics are from the Kunwei sensor.
- Full start-to-seal time was 85.59 s for this one cold attempt. This is not a
  reused-cycle median.

## Decision

This was a complete, eligible screening unit, not a paired confirmation. Its
official MAE is 0.437 N above the prior eligible A–E screening mean (0.977 N)
and 0.373 N above the A arm mean in the five parameter-confirmation pairs
(1.041 N). Those are historical, nonpaired references, so these differences
do not establish a causal effect or confidence interval. They provide no
positive signal for advancing this lower-tau candidate to five-pair
confirmation. Keep the attempt in the denominator and do not promote the
candidate.

The candidate changes only the PATH controller input filter; the official
score and readiness filter stayed at their fixed 0.35 s definition. The
comparison therefore did not improve by changing the measurement definition.

## Accepted-sample reducer correction — 2026-09-27

The per-sample values above were reduced from all writer ticks. The official
550-bin score uses unique synchronized RTDE/TP consumed samples. Re-reducing
the sealed timeline after matching it to RTDE frames gives **26,432 accepted
formal samples** and **28,837 full-course samples**; both 550/550 and 600/600
bin coverage are complete. The receipt's formal MAE remains **1.414262 N** and
is reproduced exactly by the accepted-sample 550-bin calculation.

On that matching sample set, `[5,60)` filtered sample MAE/RMSE/p95 is
**1.416802/1.744672/3.240785 N**; raw corrected KWR75 sample MAE/RMSE/p95 is
**2.674464/3.243580/5.380885 N**, and its 550-bin MAE is **2.460507 N**.
For `[0,60)`, filtered sample MAE and 600-bin MAE are **1.425256 N** and
**1.422607 N**; raw sample MAE and 600-bin MAE are **2.688146 N** and
**2.473228 N**. These remain KWR75 feedback diagnostics, not an independent
force reference. The matched data and exact score cross-check are in
[accepted-sample reducer alignment](tase_rnn_filter_tau_reducer_alignment_20260927.json).

## Evidence

- Full run: `runs/tase-rnn-low-control-filter-rate460-20260927-002902/`
- Sealed attempt: `runs/tase-rnn-low-control-filter-rate460-20260927-002902/attempts/0001/`
- Seal-verified reducer output: `reports/tase_rnn_low_control_filter_tau_attempt_20260927.json`
- Candidate parameters: `config/tase_figure8_control_filter_tau_low_rate460.json`
