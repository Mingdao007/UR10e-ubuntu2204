# Validation scope

Main integrated only the three authorized writer files after terminal exit 0.
The writer receipt records native session 01a0bc08-c534-78d0-b9d4-26f2a3f3aa78,
model grok-4.6, Homepool endpoint and high launch/route effort, with no fallback.
The provider/effort fields are route attestation, not an independent claim about
server-side reasoning implementation. The isolated writer worktree is retained.

Main focused validation: 17 tests passed (NO-v3, NO-v2 and independent prior),
with the existing xacro DeprecationWarning. The writer separately reported its
nine new tests passing. In addition, main loaded the pre-change estimator
source from Git HEAD 2d2cef5c and compared 500 seeded random updates each for
legacy and NO-v2 modes (NumPy generator seed 301): parameters, every diagnostic
and final snapshots matched exactly. This guards against comparing two aliases
of the same new code and calling that backward compatibility.

The CP-only ideal test includes substantial normal velocity, whereas the
combined along-slide correction test uses tangential velocity. This distinction
is intentional: the motion residual n dot v is contaminated by normal motion.
The transverse external-force test explicitly demonstrates CP contamination.
No noise immunity, material-damage bound or physical qualification is claimed.

The study saves full controller/simulator state and hashes its source before
execution; changing source during a run invalidates it. The analysis preserves
failed/shortened runs and null tail values when the tail was not observed.

Full owner regression completed: 211 passed, one existing xacro warning in
115.58 s. Exact output is retained in regression.txt. The run included all
`tests/test_contact*.py` plus evidence-ledger and motion-profile tests.

Full-period fresh-instance replay passed all 31,916 records for the combined
across-prior case. Seven comparisons also passed mechanical parameters,
reference hashes, physical initial state and controller initial-state checks
apart from the intended estimator change (absolute tolerance 1e-12). See
verification.json. This is same-implementation replay, not independent physics
validation. The figure was rendered and visually inspected.

Both frozen 1 ms follow-up trials completed with no failure/contact loss;
raw SHA-256 values were independently checked after the study. The combined
normal RMS benefit persists, but step sensitivity is reported explicitly in
refinement.md/json, including QP intervention counts. No tolerance-based
numerical qualification was declared.
