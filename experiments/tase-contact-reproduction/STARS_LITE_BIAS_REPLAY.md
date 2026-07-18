# STARS-lite offline bias replay

## Scope and safety boundary

This is a read-only, no-motion analysis path for completed Step5d-v3 CSV runs.
It does not start the bridge, connect to RTDE or Kunwei, upload a controller or
TP package, write gains, or authorize live contact. Outputs must be outside the
source run tree and existing outputs are never overwritten.

The estimator operates in the logged Kunwei channel coordinates. The physical
wrench origin is not verified, and the result is not claimed to be a rigid-tip
wrench; that claim would be invalid for the current non-rigid EOAT without an
identified local compliance model.

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
  only after the no-contact profile/mask, wrench covariance, path shape, phase
  coverage, XY, orientation, linear speed/acceleration, and signed pointwise
  10 mm Z-offset checks all pass. Stage-25 samples outside the validated phase
  interval are never endpoint-clamped or reference-subtracted.

## Commands

From this experiment directory:

```bash
python3 tools/stars_bias_replay.py inventory \
  --runs-root /path/to/read-only/runs \
  --output /path/outside/runs/dataset-manifest.json \
  --config config/ft_bias/stars_lite_v1.json

python3 tools/stars_bias_replay.py replay \
  --run-dir /path/to/read-only/runs/one-run \
  --output-dir /path/outside/runs/one-replay \
  --config config/ft_bias/stars_lite_v1.json

OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
python3 tools/stars_bias_replay.py evaluate \
  --manifest /path/outside/runs/dataset-manifest.json \
  --runs-root /path/to/read-only/runs \
  --output-dir /path/outside/runs/evaluation \
  --config config/ft_bias/stars_lite_v1.json \
  --jobs 14
```

Add `--reference-run-dir /path/to/free-space-reference` to single-run replay
only when a deliberately paired reference exists. Ordinary no-contact runs are
not automatically treated as a valid 10 mm pair.

## Acceptance interpretation

Software acceptance requires nonempty data, at least 95% causal coverage, at
least 10 valid rows, zero future-sample leakage, zero updates outside
`FREE_STATIC`, and covariance minimum eigenvalue at least `-1e-12`. Promotion
additionally requires bounded contact-force retention (95% to 105%), preserved
force direction, static RMSE limits, and a six-dimensional `FREE_STATIC` NIS
p95 no greater than the configured 99% chi-square threshold of 16.812. NIS for
frozen modes is retained as a diagnostic and is never treated as a bias update.

The frozen historical inventory contains 87 schema-eligible runs. Three are
hash-bound in `analysis_excluded` because they are empty or have no valid rows;
the remaining 84 analysis-eligible runs comprise 24 no-contact and 60
contact-capable runs, stratified 50/17/17 into train/tune/holdout. All 84 pass
the offline software checks. The train-only noise fit used 204,561 contiguous
`FREE_STATIC` differences, and the Q multiplier sweep selected 10.0. Every run
remains `not_promoted`: historical `FREE_STATIC` NIS p95 ranges from 25.56 to
1612.17, above the promotion threshold. No confirmed physical +10 mm reference
pair was present, so reference physical validation is `not_evaluated`.

These results establish offline tooling readiness only. They are not Step5d
package acceptance, live-contact acceptance, or TASE reproduction evidence.
