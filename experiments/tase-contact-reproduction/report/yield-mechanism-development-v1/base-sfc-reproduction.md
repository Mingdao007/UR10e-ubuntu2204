# Base SFC reproduction check

Both completed unit-0 base-resolution members reproduce training exactly in rows, full step records, initial/formal-initial/final controller and plant snapshots, and numerical metrics. Each raw artifact was SHA-256 verified before parsing.

The initial strict comparison found one nominal metrics difference: `campaign_kind` is `diagnostic_seed` in the development diagnostic and `training` in training. The final check explicitly excludes only that metadata field from metrics and records both labels; no metric definitions, values, execution sources or outcome gates were changed. The same check then passed for both nominal and disturbed members. Runtime timing and campaign provenance are not asserted identical.

This verifies reproduction only for this completed SFC pair. It does not establish deterministic behavior of the remaining methods, numerical convergence, physical evidence or formal validation. See `base-sfc-reproduction.json` and `verify-base-reproduction.py` for exact fields, digests and reproduction.
