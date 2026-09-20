# Mechanical coefficient equivalence and tuner compatibility

This prescribed-input native check supports the structural identity-metric
claim with execution evidence. At controller steps 1, 2 and 3 ms, DSFC using
MSFC g50's six mechanical coefficients agrees with identity-metric MSFC to
4.6e-17 m/s or better over rotating forces, sustained load, a pulse and release.
MSFC structure evolves (norm about 0.315), so this is not a zero-memory reset.
Both laws' complete snapshots replay with exactly zero output difference.
This does not compare contact performance, prove equivalence of active MSFC,
or consume any formal tuning/holdout budget.

A separate read-only audit found an important incompatibility before reuse of
the old tuner. Its DSFC m/mu/g bounds already contain the MSFC g50 point. However,
its frozen DSFC coefficients are a=1.2, p=0.1, whereas the current full-task
study uses a=0.05, p=0.5. Thus merely extending mu or g bounds cannot make the
old tuner search the coefficient-matched law. A full-task tuning configuration
must bind the actual current equations and fixed parameters explicitly; do not
reuse the six-law configuration or rewrite its historical record.

The initial audit assertion that all six coefficients matched failed; the
retained tuner-bounds.json records the actual mismatches rather than asserting
compatibility. DSFC and MSFC also retain their own solver parameter schemas;
only mechanical coefficients are matched in check.py. No solver tolerances,
controller files, or shared defaults were edited.

Reproduce with the experiment .venv-contact-six Python:
- report/yield-coefficient-equivalence-v1/check.py
- report/yield-coefficient-equivalence-v1/tuner_bounds_check.py

The original 24 units still mean 24 nominal/disturbed pairs per method, split
8 initial + 12 BO + 4 repeats. A multi-scenario batch cannot be counted as one
pair. Full-task training condition, candidate selection objective, numerical
checks and independent holdout remain to be frozen before actual tuning.
