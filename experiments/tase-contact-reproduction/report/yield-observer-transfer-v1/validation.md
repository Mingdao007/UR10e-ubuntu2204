# OT-v1 validation and execution scope

Six new fixed-parameter trials ran to the complete 62.831853 s PATH duration,
with full controller/simulator state, at 2 ms/eight plant substeps. Three DSFC
NO-v3 receipts and nine legacy-observer receipts were reused after SHA-256
verification. All nine within-method observer comparisons passed mechanical,
settings, initial-state and reference checks at absolute tolerance 1e-12.
No parameters, freshness gates or controller source changed during these runs.

OC-v1 separately reconstructs the exact law-input difference at every paired
sample from four vector terms, with maximum component error below 1e-11 N.
The analysis is an identity on recorded observations, not an independent
closed-loop intervention. Its source hashes and results are retained.

The unchanged runtime/controller had 211 passing tests in fd6be9a1. During
this turn, inspection found a distinct CLI issue: `run_contact_yield.py refine`
did not inherit estimator parameters or custom surface curvature, so it could
compare different systems. Main made a narrow local correction, also carrying
QP library and kinematics requirement. Fifteen focused tests passed, including
a fresh Python CLI subprocess with active NO-v3, tilted prior, non-default
curvature and the fixture QP library. Existing xacro deprecation warning only.
No additional writer was needed for this local correction. Previously reported
NO-v3 step checks used their own explicit-parameter scripts and are unaffected.

The SFC normal-contact-loss case is separately replayed and refined at 1 ms
with full state recorded, preserving all source experiment parameters. Those
results are retained separately; failure or contact loss is never discarded.
Plots were rendered and inspected. None of these checks qualifies real
transport, controller timing, contact, human intervention or physical safety.

The SFC negative case's 31,916-record replay passed. Its 1 ms full-state receipt
hash was independently verified. Both step sizes lose contact (0.434/0.985 s),
but 1.005 N maximum force difference and changed saturation indicate unresolved
numerical sensitivity, not step convergence. A finer grid/implementation audit
is required before any quantitative method ranking based on this cell. The
refinement has no matched nominal, so it supplies no recovery-time comparison.
