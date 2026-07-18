# STARS-lite offline bias replay

## Scope and safety boundary

This is a read-only, no-motion analysis path for completed Step5d-v3 CSV runs.
It does not start the bridge, connect to RTDE or Kunwei, upload a controller or
TP package, write gains, or authorize live contact. Outputs must be outside the
source run tree and existing outputs are never overwritten.

The estimator preserves the logged force-sensor frame and declared sensor/TCP
origin. It does not claim that the corrected wrench is a rigid-tip wrench; that
claim would be invalid for the current non-rigid EOAT without an identified
local compliance model.

## Components

- `tools/contact_mode_estimator.py` converts existing v3 bridge and sensor facts
  into fail-closed modes: `INVALID`, `UNKNOWN`, `FREE_STATIC`,
  `FREE_REFERENCE`, `SEARCH`, `IMPACT`, `CONTACT_TRACK`, `LIFT_OFF`, and
  `REBASELINE`.
- Only high-confidence `FREE_STATIC` rows can update bias. Search, impact,
  contact, lift-off, rebaseline, stale data, epoch mismatch, and dynamic
  no-contact reference rows are prediction/freeze only.
- `tools/stars_bias_replay.py` uses a causal join: the latest Kunwei sample at
  or before each bridge timestamp. It emits the logged residual wrench, gated
  EMA result, 12-state bias/rate Kalman prediction and posterior, innovation,
  NIS, covariance diagnostics, and the decision that gated each update.
- An optional `--reference-run-dir` subtracts a phase-matched free-space trace
  only after path shape, phase coverage, XY, orientation rotvec, linear speed,
  and nominal 10 mm Z-offset checks all pass.

## Commands

From this experiment directory:

```bash
python3 tools/stars_bias_replay.py inventory \
  --runs-root /path/to/read-only/runs \
  --output /path/outside/runs/dataset-manifest.json

python3 tools/stars_bias_replay.py replay \
  --run-dir /path/to/read-only/runs/one-run \
  --output-dir /path/outside/runs/one-replay \
  --config config/ft_bias/stars_lite_v1.json

OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
python3 tools/stars_bias_replay.py evaluate \
  --manifest /path/outside/runs/dataset-manifest.json \
  --output-dir /path/outside/runs/evaluation \
  --config config/ft_bias/stars_lite_v1.json \
  --jobs 14
```

Add `--reference-run-dir /path/to/free-space-reference` to single-run replay
only when a deliberately paired reference exists. Ordinary no-contact runs are
not automatically treated as a valid 10 mm pair.

## Acceptance interpretation

Software acceptance requires zero future-sample leakage, zero updates outside
`FREE_STATIC`, and covariance minimum eigenvalue at least `-1e-12`.
Synthetic promotion additionally requires contact-force retention at least
95%, static force RMSE at most 0.10 N, and static torque RMSE at most 0.01 Nm.

The frozen historical inventory contains 87 eligible runs: 26 no-contact and
61 contact-capable, stratified 60/20/20 into train/tune/holdout. All 87 pass the
offline software checks. The Q multiplier sweep selected 10.0, but only 3 of 17
holdout runs met every per-run promotion condition; the historical result is
therefore `kalman_not_promoted`. No confirmed physical +10 mm reference pair
was present, so reference physical validation is `not_evaluated`.

These results establish offline tooling readiness only. They are not Step5d
package acceptance, live-contact acceptance, or TASE reproduction evidence.
