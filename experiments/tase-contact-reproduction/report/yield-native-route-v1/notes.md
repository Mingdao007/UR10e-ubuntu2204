# Native yield route admission

Development software seam only. Historical model reconciliation with the
2026-09-17 read-only kinematics receipt. Not fresh physical qualification,
workspace accuracy, contact-model identification, transport evidence, or
source closure.

## What this route does

`prepare_control` still owns pre-ARM creation, exact instance binding,
one-time consumption, dedicated `step5d_contact_six_qp_v1`, and sole-writer
injection. For `YieldContactProvider` it now consumes a typed
`YieldNativeBinding` instead of `step5d_autotune_v4.contracts.load_contract`.
That binding is built from the current calibrated model, `QpSolverProfile`,
native law fingerprint, and the explicit NO-v3 observer file. `gate_qdot`
compares the provider's hashes with that binding, not a caller-invented dict
and not the frozen RNN xacro digest.

ContactCommandProvider and no-provider paths still call the old loader, which
correctly fails on ur_xacro SHA `618ece...`. Old RNN manifests were not
edited. Production runtime/QP deadlines remain 1.5 ms / 1 ms; `deadline_s=None`
is only used in explicit offline checks.

SFC/DSFC/MSFC parameters are the frozen-transfer protocol values (MSFC g50,
original active metric). The common observer is
`config/yield_normal_observer_v3.json`. NO-v3 has documented tradeoffs and is
not a qualification claim. Existing `YieldContactRuntime` callers without
`estimator_parameters` keep the implicit estimator.

## Limitations

Construction does not send, load, play, or stop anything. Offline synthetic
home pose/q from the preserved-home receipt were supplied explicitly; they are
not a new Home or a current robot observation. The 31,916-tick qualification
fixture was not rerun. No live writer, endpoint, or scientific campaign was
executed.
