# Step5b TP Local Bayesian Autotune Flow

This is an isolated experimental flow. It does not replace the current Step5d
route and does not modify `config/current_stage.json`, `STEP5_FLOW.md`,
`config/step5_stage_table.json`, or `tools/kunwei_rtde_bridge.py` while P0v9 and
Step5d v30 are active.

Its isolated row lives in `config/step5b_autotune_stage_table.json`. That file
is deliberately inactive and blocked; it is not a second current-stage pointer
and cannot authorize a live run.

## Fixed physical path

The Teach Pendant package is generated from
`step5b_contact_cycloid_baseline_v2`, which remains the executable
specification: entry, first contact search, first-contact normal latch, the
conditional 4 degree lift/attitude skip, otherwise the 20 mm lift plus Stage
25.2 attitude correction and second search, Stage 25.3 entry gate, the 60 s
Step5 cycloid, retract, and return home.

The force/frame convention is unchanged: `reaction_normal` defines positive
load and `approach_normal` defines posture and press direction. Force and
torque guards remain 50 N raw normal, 60 N total force, and 3 Nm total torque.
No UR `zero_ftsensor()`, Kunwei tare/configuration, payload write, or TCP write
is part of this flow.

## Session loop

The operator opens the controller-read-back-verified
`step5b_contact_cycloid_bayes_loop_v1.urp` in Local Control and presses Play
once. The TP then waits at the captured home pose. The Ubuntu supervisor arms
one candidate through RTDE integer registers, starts exactly one existing
`kunwei_rtde_bridge.py` child, observes the TP state, and stops that child only
after the TP reports a verified home return.

Postprocessing starts after the bridge child has closed its run artifact. A
trial that violates a tunable constraint is recorded as infeasible, with no
fabricated objective value. Fatal sensor, RTDE, controller, protective-stop,
e-stop, TP-pause, or home-verification failures end the session. Two
consecutive recoverable constraint violations latch a pause until the operator
uses `resume`.

## Search and acceptance

Target-force contexts rotate through 10 N, 12 N, and 15 N. Tier 1 tunes force
P gain, damping, and normal-filter alpha on bounded discrete grids. Tier 2 may
also tune force I gain only after every context has five feasible full trials,
repeatability is within 15%, and the latest six trials contain no failure.

The optimizer uses contextual Bayesian optimization with q=1
`qLogNoisyExpectedImprovement`, an explicit 0.95 feasibility threshold, a
one-grid-step trust region around each context incumbent, and an incumbent
replicate every fourth visit. Live candidate selection requires CUDA and runs
the GP, feasibility model, and batched acquisition evaluation on `cuda:0`; it
does not silently fall back to CPU. Independent history scans and completed-run
postprocessing use up to 16 CPU workers, and CPU diagnostics may overlap GPU
selection after the immutable capture marker. All heavy workers join before
the next live writer starts. The loss combines force tracking, p99 force error,
XY tracking, command smoothness, and near-limit dwell.

Offline implementation acceptance does not authorize contact. Before an
infinite session, merge this isolated stage into the canonical Step5 table
after the P0v9/v30 work is clear, freeze the composite fingerprint, run Review
v3 1+1, and pass serialized 2 s, 10 s, and 60 s canaries. Autonomous operation
requires two consecutive full 60 s trials at different target contexts with a
verified home return.
