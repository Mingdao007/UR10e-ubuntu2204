# Yield-fair campaign plumbing v1

Software plumbing only. No formal training or holdout unit was spent at delivery.
Main subsequently runs a separately labeled full-cycle diagnostic pair; see
`../yield-fair-campaign-native-smoke-v1/`. This is a prospective development-model
comparison path, not a precise-contact, safety, or physical qualification
claim.

## Files

- `config/yield_fair_campaign_v1.json`
- `tools/yield_fair_selection.py`
- `tools/yield_fair_campaign.py`
- `tools/yield_contact_ledger.py` (pair-feasibility adapter and inflight helper only)
- `tests/test_yield_fair_selection.py`
- `tests/test_yield_fair_campaign.py`

The runner reuses `YieldContactTuner`, `YieldContactLedger`, and
`run_closed_loop`. Pair evaluation is a pure function. Campaign commands are
explicit `create` / `status` / `run-next` / `reconcile` / `freeze`. Import,
help, create, and status do not start trials.

## Tests

Focused falsifiers for mismatched pair/candidate/frame/grid, incomplete and
NaN evidence, progress-freezing disturbed vs true nominal, analytic final-cell
weights, fail debit, restart before and after artifact, repeated reconcile,
config drift, no autorun, stop-before-EI, and a synthetic 24-pair freeze
through the existing tuner. One short native diagnostic smoke is labeled
`diagnostic_seed` and does not debit the training ledger.

## Limitations

Holdout is refused and not implemented. Freeze is parameter selection on a
complete 24-pair schedule, not scientific acceptance. Development feasibility
bands are not user precision requirements or physical safety guarantees.
`.05 rad` is a declared objective normalization, not a 1 s observer correction
claim. Full-state checks validate persistent controller/plant snapshot schema,
clocks, and initial/formal/final identity; they do not perform exact forward
replay. `objective_eligible` means a complete uncensored sealed result;
`pair_feasible` is the selection/EI gate. Main owns integration, Git, hardware,
and any full verification. No formal budget until accepted.
