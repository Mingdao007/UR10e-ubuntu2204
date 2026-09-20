# Yield training-result report v1

Read-only reporter for one frozen yield-fair training campaign. It does not
launch training, reserved validation, simulation, or hardware. Main owns
integration, Git, the live campaign run, and scientific conclusions.

Interpretation is governed by
`report/yield-round8-main-review-v1/main-adjudication.md`. This tool records
partial and terminal outcomes honestly. It does not manufacture an equal-budget
three-method freeze or a proposal-superiority claim.

## Command

```
python tools/yield_training_report.py --campaign CAMPAIGN_ROOT --output NEW_DIRECTORY
```

`--output` must not exist. The tool never writes inside the campaign root.
Import and `--help` do not run a report.

The ledger is opened once with SQLite `mode=ro` inside a single transaction.
The connection is closed before any raw artifact is hashed or parsed. A writer
ledger is never instantiated. Running members are never read. Complete members
must match `artifact_sha256` before parse; missing or mismatched complete
evidence fails. One raw artifact is loaded at a time.

## Outputs

- `report.json` / `report.md`
- `slots.csv` (72 ordinal pair slots)
- `members.csv`
- `initial_triples.csv`
- `best_so_far.csv`
- `artifact_digest_manifest.csv`
- `plots/best_so_far_j.{png,pdf}`
- `plots/initial_triples_nominal.{png,pdf}`
- `plots/initial_triples_disturbed.{png,pdf}`

## What is reported

Every method has 24 ordinal slots. States are completed feasible, completed
nominal-infeasible, completed disturbed-guard-infeasible, both-infeasible,
failed, incomplete running or pending, not run, and stopped-before-EI. A finite
ledger J is not a feasible incumbent. Failed or interrupted units consume
budget. Missing data stay empty; they are not replaced by zero.

Parameters and `initial` / `bayesian_ei` / `repeat_incumbent` labels come from
registered units and the published 8+12+4 schedule. Units 0–7 are shared-triple
comparisons only when the stored mechanical `(m, mu, g)` values actually match.
EI rows are method-specific training trajectories, not coefficient-matched
pairs. Literal repeats are labeled and are not independent confidence samples.

Descriptors reuse `summarize_trial` and `compare_pair`. Low-load duration is
`true_normal_load_n < 1 N` on the declared grid, not geometric contact loss.
Ledger J components are printed beside absolute descriptors and are not asserted
to be optimized precision or safety. Best-so-far J uses only feasible completed
pairs and stops at observed complete or failed ordinals. Incomplete, running,
or pending units remain in the 72-slot table but do not extend the curve.
Infeasible complete J is marked, not selected. A complete pair whose
`pair_feasible` flag contradicts `nominal_feasible` and `disturbed_guards_ok`,
or whose checks contradict those flags, fails closed. Unspent SFC ordinals
after an initial-feasibility stop are `stopped_before_ei`, not a carried line.

Plots and tables are training-only development snapshots. They are not
independent validation or physical results. No CI, winner, p-value, or pooled
validation is computed.

## Tests

Focused synthetic fixtures in temporary directories. They do not read the live
campaign. See `tests/test_yield_training_report.py`.

Interpreter: `.venv-contact-six` Python with
`PYTHONPATH=/usr/lib/python3/dist-packages:tools:tests` so system Matplotlib
and NumPy are used without modifying that environment.

## Limits

Main must run this tool against the real campaign after review. This delivery
does not inspect `runs/yield-fair-training-v1/` in other worktrees, does not
freeze the campaign, and does not spend training or validation budget.
PNG/PDF require Matplotlib. If Matplotlib or NumPy cannot be imported, the
tool fails before creating the output directory. There is no custom image
renderer fallback.
