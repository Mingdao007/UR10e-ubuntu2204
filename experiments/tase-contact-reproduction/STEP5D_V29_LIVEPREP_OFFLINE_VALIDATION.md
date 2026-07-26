# Step5d v29 offline live-prep validation

Date: 2026-07-10 HKT

Host: `andy7`, PREEMPT_RT kernel `5.15.0-1110-realtime`

Boundary: offline computation only; no RTDE, bridge, controller write, TP Play,
`zero_ftsensor()`, or robot motion.

The canonical artifact is
`runs/step5d_v29_liveprep_offline_20260710_0317/liveprep_readiness.json`
(SHA-256 `cf91a400882dd1a377c5fc27e25d598c69ade3390511c61ef46902b811ee8707`).
The same folder retains `before_optimization_readiness.json`
(SHA-256 `f178138512b6deead09614640546f9badea59675e02aaa305e6d5ae8901214e9`).

## Result

`liveprep_blocked` is the only valid state. Package hashes, controller
readback, DLS shadow, solver microbenchmark, and zero-qdot safe hold pass. The
60 s / 500 Hz paced synthetic tick still has deadline misses, and the
three-lane milestone review is intentionally not accepted while that blocker
remains.

| metric | before allocation/marshaling optimization | after optimization |
|---|---:|---:|
| solver first post-warm | 1.289 ms | 1.175 ms |
| solver p99 / max | 1.381 / 1.408 ms | 1.304 / 1.337 ms |
| solver deadline misses (10,000) | 0 | 0 |
| synthetic p50 / p99 | 1.381 / 1.744 ms | 1.266 / 1.503 ms |
| synthetic compute misses (30,000) | 11 | 1 |
| synthetic schedule overruns (30,000) | 98 | 36 |
| synthetic total deadline failures | 109 | 37 |
| safe-hold deadline misses (10,000) | 0 | 0 |

## Optimization boundary

The strict solver retains CuPy, 1024 inner iterations, epsilon 0.010,
finite-time exponent `r=0.8`, and the 0.05 rad/s qdot cap. The change only
synchronizes lifecycle-boundary host-to-device state copies and reuses fixed
host/device staging buffers with one contiguous result readback. Thresholds
were not relaxed and no scheduling-priority override was used.
