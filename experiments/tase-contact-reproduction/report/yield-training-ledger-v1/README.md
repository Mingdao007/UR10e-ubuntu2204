# Three-method paired training ledger

The yield adapter reuses the mature SQLite transaction and attempt lifecycle.
Default six-controller callers retain their scope and completion semantics;
the new explicit scope is SFC, DSFC and MSFC. A controller-scope binding is
persisted, and a legacy database cannot be reinterpreted as the new scope.

A training ledger binds the campaign protocol, tuner config, training cell and
selection contract. An attempt is registered before execution, so restart,
failure, censoring or interruption cannot refund a pair. One attempt remains
inflight at a time. Both nominal and disturbed members must be sealed before
training observations are returned. Holdout or differently bound evidence is
rejected; repeated identical sealing is idempotent and conflicting evidence
cannot replace a terminal record.

At freeze, all three methods must have exactly 24 pairs. The adapter recomputes
the deterministic proposer schedule from the sealed history and rejects any
candidate sequence that differs, including incorrect initial/EI/repeat choices.
The selected candidate must equal the discovery incumbent frozen before the
four repeat units. Freeze is parameter selection, not scientific qualification.
Failed methods without a qualified incumbent are not silently given a seed.

Scope: metadata/evidence-digest ledger only, no simulator/device/campaign entry.
The future runner must produce and verify the referenced full-task artifacts;
caller-supplied metadata alone is not physical evidence. Holdout needs its own
separate execution/recording flow and cannot use these training records.

Validation: 11 focused tests pass, including mature ledger/proposer regressions,
restart with an inflight attempt, failed budget consumption, idempotent sealing,
holdout/contract rejection, immutable binding, all-three-method 24-pair freeze,
and rejection of an off-schedule history. Tests use temporary SQLite files and
synthetic objectives only. Actual formal tuning and holdout budgets remain zero.
The initial old-test error-message regression and its fix are retained.
