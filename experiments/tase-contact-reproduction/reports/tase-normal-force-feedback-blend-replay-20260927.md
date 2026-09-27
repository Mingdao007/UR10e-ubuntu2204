# TASE normal-force feedback blend replay — 2026-09-27

## Result

No signal-blend candidate is ready for live use. On five sealed, eligible rate460 A attempts, replacing the mature controller's current normal-force input

```text
control = max(filtered KWR75 normal, measured KWR75 normal)
```

with `filtered + alpha * (control - filtered)` materially changes the outer-loop normal command and integral state. The replay can compare commands on fixed observations; it cannot estimate a candidate force MAE or closed-loop response. KWR75 is the force feedback source, not independent task-force truth.

## Evidence

The five source attempts are the A sides of the existing five randomized confirmation pairs. Each is sealed and eligible under `figure8_window60_r013_rate460_v1`, with 550/550 formal bins and 600/600 diagnostic bins. Their sealed formal MAEs range from 0.942 to 1.196 N. No physical attempt was repeated.

The fused outer-loop baseline reproduced the sealed `[5,60)` twists exactly for all five attempts. A second RNN/qdot replay reproduced the sealed baseline qdot to below `9e-19 rad/s` maximum error. The compact trace omits some previous-velocity state before PATH origin, so `[0,5)` command reconstruction has up to `0.00134 m/s` error and is diagnostic only.

Across the five A traces, the fixed-observation outer-loop replay gave:

| Input to outer loop | Median absolute normal-command change vs A | Median per-tick P95 command change | Median absolute integral-state change | Meaning |
|---|---:|---:|---:|---|
| Filtered only (`alpha=0`) | 1.628 mm/s | 5.133 mm/s | 0.481 N·s | Large change; 5,657–6,719 recomputed retry decisions differed from recorded decisions |
| 25% raw excess (`alpha=0.25`) | 1.441 mm/s | 4.452 mm/s | 0.474 N·s | Still a substantial normal-command change; final qdot was not replayed |
| 50% raw excess (`alpha=0.5`) | 1.142 mm/s | 3.103 mm/s | 0.447 N·s | Smaller command delta, but not a small perturbation |

Each table entry is the median across the five attempts of that attempt-level statistic: mean absolute command change, per-tick P95 absolute command change, or mean absolute integral-state change. It is not a pooled per-tick median.

For `alpha=0.5`, the mature RNN and final qdot gates were replayed across all five attempts. The baseline qdot again matched sealed output; the candidate had zero joint-bound, post-gate task-cap, and post-gate slew violations. Final gate projection occurred on 3–9 formal rows per attempt. The replayed candidate's force-preempt retry decisions differed from the recorded transitions on 5,113–6,093 rows; because candidate replay recomputes transitions while baseline reproduction restores recorded transitions, this count is diagnostic and cannot be attributed to `alpha` alone.

Both candidate files explicitly record `force_mae_recomputed=false`, `closed_loop=false`, and `physical_candidate_executed=false`. Passing the command constraints therefore does not establish better tracking, 460 Hz live timing, or live readiness.

## Decision

Do not dispatch filtered-only or the tested partial blends. They alter the normal command and stored integral state substantially, while the fixed-observation replay supplies no force-response evidence. A live test would consume a full 60 s attempt without a demonstrated reason to expect improvement. Keep the current A input rule unchanged while investigating the retry-transition mismatch and other force-error causes.

## Reproducible artifacts

- Five-A full command replay: `runs/tase-rnn-control-signal-replay-five-a-20260927/report.json`
- Partial outer-loop blend replay (`alpha=0.25, 0.5`): `runs/tase-rnn-control-signal-partial-blend-replay-20260927/report.json`
- Partial `alpha=0.5` RNN/qdot replay: `runs/tase-rnn-control-signal-partial-blend-replay-20260927/qdot-alpha-0p5.json`

The JSON files retain source attempt identities, timeline digests, both time windows, command/integral summaries, constraint checks, and the limits above. They do not modify the source receipts.
