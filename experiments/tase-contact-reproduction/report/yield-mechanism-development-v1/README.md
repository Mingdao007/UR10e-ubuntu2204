# Yield mechanism development diagnostic v1

Bounded **development** plumbing. Not reserved validation, not a training
debit, and not a freeze. Main owns integration, Git, hardware, and any later
full-period execution.

The question is the one in
`report/yield-training-report-final-v1/main-interpretation.md`: isolate active
memory and geometry on the already observed stiff_low_mu surface using shared
coefficients, NOv3, the training outer loop, and paired nominal references.
This tool only binds that case list and runs one diagnostic slot at a time.

## Files

- `tools/yield_mechanism_development.py` — `create` / `run-next` / `status` / `report`
- `tests/test_yield_mechanism_development.py`
- `report/yield-mechanism-development-v1/README.md`
- `report/yield-mechanism-development-v1/writer-receipt.json`

The runner calls existing `contact_yield_runner.run_closed_loop`
(`campaign_kind=diagnostic_seed`, `record_fullstate=True`, `require_ur10e=True`,
`timeline=full_cycle`, duration `2*pi/0.1`). Metrics reuse `summarize_trial`,
`compare_pair`, and the training full-state / J helpers. Arm coefficient routing
reuses `arm_law_parameters` for identity and radial. No training freeze is
loaded or manufactured.

## Fixed case list

60 fresh diagnostic trials, zero training/validation budget, no tuning.

| Axis | Binding |
| --- | --- |
| Surface | existing stiff_low_mu, `kappa_xx=0.8`, `kappa_yy=0.4` |
| Prior / observer / prep | approach, NOv3, cold |
| Outer loop | training `Kp=120`, `Kz=0` |
| Duration | full unwrapped period `2*pi/0.1` |
| Triples | YieldContactTuner initial units **0** and **4**, same `(m, mu, g)` on every arm |
| Arms | SFC, SFC_RADIAL, DSFC, MSFC, MSFC_IDENTITY |
| Identity | actual method MSFC, frozen MSFC other parameters, `minimum_metric_eigenvalue=1` |
| Radial | actual method `SFC_RADIAL`, SFC mechanical coefficients |
| Resolutions | `dt=0.002/sub8`, `dt=0.001/sub4`, `dt=0.002/sub16` |
| Pair | `nominal` and `sustained_release_oblique` |

### Data-selection disclosure

Unit 0 is the first shared initial triple. Unit 4 is included because memory
adversity was observed on that shared initial unit in the training development
table. It is **not** a holdout, not an incumbent freeze, and not an independent
validation cell. Task tolerances and winner selection are unchanged. Existing
reserved validation cells remain unexecuted.

## Commands

```
python tools/yield_mechanism_development.py create --output NEW_DIRECTORY \
    [--qp-library PATH] [--native-laws-root PATH]
python tools/yield_mechanism_development.py run-next --output DIRECTORY
python tools/yield_mechanism_development.py status --output DIRECTORY
python tools/yield_mechanism_development.py report --output DIRECTORY
```

`create` writes an immutable 60-slot manifest, binds its digest, and binds
source, observer, protocol, campaign/tuner config bytes, training execution
identities, and native identities before any run. The stored protocol digest
and a rebuilt tuner/arm/resolution schedule are rechecked on status/run-next;
id, order, method, parameters, scenario, dt, and pair association changes are
rejected, not merely count. Main may point at existing production binaries with
explicit paths; those paths are hashed and rechecked. Missing or unbound
binaries fail create and fail `run-next` before an attempt is registered.
`run-next` records the slot as begun, executes exactly one member with frozen
outer `YieldSettings` (`Kp=120`, `Kz=0`) on the real `run_closed_loop`
settings API, writes the full-state artifact, and seals the digest. Existing
or inflight slots are refused; there is no silent overwrite. Failures stay on
disk.

`status` and `report` are read-only and do not launch trials. Import and
`--help` do not run a diagnostic.

Successful-but-out-of-band complete members are retained and still receive the
other resolutions. Failed members are retained; the pair is not ranked.
Absolute descriptors plus own-nominal paired recovery/J are descriptive only.
Report names matched arm comparisons and dt differences. No cross-resolution
pooling, no confidence intervals, no causal or winner inference.

## Tests

Focused falsifiers with a fake runner. They do not spend the 60-trial budget.

- shared `(m, mu, g)` versus coefficient mismatch
- MSFC identity / SFC_RADIAL routing, including relabel refusal
- 60-slot schedule completeness
- mutated manifest `g` / ids / order / method / scenario / dt rejected by public `status` and `run-next`
- stored protocol digest drift rejected by public `status` and `run-next`
- overwrite and inflight refusal
- pair association to the matching nominal
- failed-member retention without skipping remaining slots
- out-of-band complete pairs not skipped
- frozen outer settings supplied through the real `run_closed_loop` settings API
- missing/unbound binaries fail before inflight; create refuses unbound defaults
- create / status / report do not launch

Interpreter:
`/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/.venv-contact-six/bin/python`
with `PYTHONPATH=tools:tests`, `PYTHONNOUSERSITE=1`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` (unset `PYTHONHOME` / `VIRTUAL_ENV`).

Result: **15 passed in 0.48s**.

## Limits

This delivery does not execute the 60 trials, does not touch reserved
validation, and does not select a winner. Full-state checks validate snapshot
schema, not exact forward replay. Main still owns launch after review.
