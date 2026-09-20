# Reserved simulator validation runner/evaluator v1

Software plumbing only. No validation, holdout, or formal training unit was spent
at delivery. Main owns integration, Git, hardware, and any later launch after a
complete three-method training freeze exists.

## Files

- `tools/yield_validation_selection.py` — pure holdout evaluator
- `tools/yield_validation_runner.py` — `create` / `status` / `run-next` / `reconcile` / `report`
- `tests/test_yield_validation_selection.py`
- `tests/test_yield_validation_runner.py`

The runner reuses `run_closed_loop` (`campaign_kind=holdout`, `record_fullstate=True`,
`require_ur10e=True`, `timeline=full_cycle`, full `PERIOD`). Pair evaluation reuses
training full-state parsers, `cell_weight` / objective helpers, `summarize_trial`,
and original `compare_pair`. It does not call or relabel through the three-method
training evaluator.

## Bindings

Create requires a digest-bound `ur10e.yield-fair-campaign-freeze-v1` export sitting
with its training `campaign.json` and `campaign.sqlite`. Source, protocol, tuner,
cell and selection identities must match that export. The ledger is read-only and
must contain 24 sealed nominal/disturbed units per method whose recorded candidates
replay the deterministic tuner schedule; the selected incumbent must be the discovery
candidate. A boolean flag or a self-hashed selected-candidate payload without that
training export is not sufficient. There is no create argument that skips the ledger.
The reserved six cells, five arms, three settings and 180-trial ceiling are copied
from `report/yield-validation-reservation-v2/protocol.json`. Identity executes
MSFC with frozen MSFC coefficients except `minimum_metric_eigenvalue=1`. Radial
executes `SFC_RADIAL` with frozen SFC coefficients. Raw artifact method is never
relabeled. Prior `n0` is rotated from calibrated home approach and
`Task.reference(0)` tangent axes into `estimator initial_inward_normal_base`.

## Tests

Focused falsifiers for wrong freeze, a self-hashed freeze without a training
ledger, missing campaign export/sqlite, illegal status, missing pair, mismatched
evidence, arm coefficients, prior axis, scenario, dt, refinement identity, caller
flags hiding failed vs out-of-band complete base, null/missing/partial/NaN
evidence, nonzero-prior warm and radial full-state schema, source drift,
interrupted restart without duplicate execution, artifact tamper, exact scenario
windows and final cell weights, MSFC identity / radial label truthfulness, finite
180-slot schedule, and no auto-launch. One ≤0.02 s PATH diagnostic native smoke
is labeled `diagnostic_seed` and does not debit validation.

## Limitations

No formal validation evidence is claimed. Freeze is a training parameter export,
not scientific acceptance. Training bands are flags only; out-of-band complete
pairs are retained and still refined. Failed/incomplete base pairs skip both
refinements without retry. Unresolved resolution sensitivity is not equivalence;
lower J with adverse absolute descriptors is mixed. Full-state checks validate
snapshot schema, not exact forward replay. Main still owns launch after
integration and frozen training.
