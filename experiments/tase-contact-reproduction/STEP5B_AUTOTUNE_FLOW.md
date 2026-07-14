# Step5b TP Local Bayesian Autotune Flow

This is an isolated experimental flow. It does not replace the current Step5d
route and does not modify `config/current_stage.json` or
`tools/kunwei_rtde_bridge.py` while P0v9 and Step5d v35 are active. Its status
rows in `STEP5_FLOW.md` and `config/step5_stage_table.json` remain inactive and
current-pointer-independent.

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
`step5b_contact_cycloid_bayes_loop_v2.urp` in Local Control and presses Play
once. The TP then waits at the captured home pose. The Ubuntu supervisor arms
one candidate through RTDE integer registers, starts exactly one existing
`kunwei_rtde_bridge.py` child, observes the TP state, and stops that child only
after the TP reports a verified home return.

Postprocessing starts after the bridge child has closed its run artifact. A
trial that violates a tunable constraint or returns through an abort reason is
recorded as infeasible, with no fabricated objective value; it may continue to
the next candidate unless the session STOP file or a fatal condition is set.
Fatal sensor, RTDE, controller, protective-stop,
e-stop, TP-pause, or home-verification failures end the session. Two
consecutive recoverable constraint violations latch a pause until the operator
uses `resume`.

The v2 triplet reached `controller read-back verified` on 2026-07-14. The
fresh fetched-back files and URP internal gate are recorded under
`runs/controller_readback_step5b_contact_cycloid_bayes_loop_v2_20260714T202201HKT`;
the controller path is
`/programs/andyl/kunwei/step5/autotune/step5b_contact_cycloid_bayes_loop_v2.urp`.
This file-delivery result does not authorize loading the program, starting the
bridge, pressing TP Play, or moving/contacting the robot.

## Search and acceptance

The target force is fixed at 12 N so the selected outer-loop parameters map
directly to the shared Step5/Step5d profile. The first 60 s trial uses the
retained baseline (`Kp=0.001`, `Ki=1e-5`, damping `7.0`, filter alpha `0.55`).
Tier 1 tunes force P gain, damping, and normal-filter alpha on bounded discrete
grids. Tier 2 may also tune force I gain after five feasible full trials,
repeatability is within 15%, and the latest six trials contain no failure.

The optimizer uses single-context constrained Bayesian optimization with q=1
`qLogNoisyExpectedImprovement`, an explicit 0.95 feasibility threshold, a
one-grid-step trust region around each context incumbent, and an incumbent
replicate every fourth visit. Live candidate selection requires CUDA and runs
the GP, feasibility model, and batched acquisition evaluation on `cuda:0`; it
does not silently fall back to CPU. Model fitting is serial by default; the
two-stream path is available only through an explicit `verified_parallel`
attestation after that exact environment has passed its regression. Independent history scans and completed-run
postprocessing use up to 16 CPU workers, and CPU diagnostics may overlap GPU
selection after the immutable capture marker. All heavy workers join before
the next live writer starts. The only scalar objective is the 60 s signed-load
force MAE in newtons. A full trial requires at least 59.5 s of strictly
monotonic Stage25 timestamps, path progress at least 59.9 s, no sample gap over
0.1 s, sensor-ok throughout, and fresh token-bound RUN/HOME/HOLD plus reaped
process-group closure. Source and config content are fingerprinted before and
after every trial and must remain identical. Each history row carries the
backend identity, the verified fingerprint, a full-width `trial_uid` derived
from session/candidate identity, and a separate `physical_capture_uid` derived
from the raw bridge-capture SHA256; `run_dir` and mutable runtime fields never
define identity. An atomic pre-motion `trial_spec.json` is repeated through
runtime and closure evidence. Cross-directory copies are detected through
either identity before history admission, and promotion rehashes raw capture
bytes plus every non-empty provenance artifact before use, then revalidates
the repeated trial, capture, and fingerprint identities.
Malformed or duplicate evidence is durably quarantined and cannot reach model
fitting or promotion; inability to write and verify that quarantine is fatal.
Only an explicit parameter guard/constraint event with verified safe closure
is admitted as a negative feasibility observation. Short/cadence, sensor,
required-column, fatal-session, fingerprint, and closure failures are
platform/integrity evidence, never parameter evidence, and stop selection of
a new candidate. TP reason 4 (operator/stop), reasons 8/10/12 (search or
infrastructure), and reason 13 (host/TP command-contract defect) likewise
never enter BO. Force RMSE/p99, XY tracking, command smoothness, and
near-limit dwell remain diagnostics.

After every completed trial the supervisor writes a fingerprinted Step5b to
Step5d promotion status. A candidate becomes promotable only after the same
parameter tuple completes two distinct feasible full 60 s trials whose force
MAE differs by no more than 15%. Raw trial artifacts, the TP/read-back package,
bridge, evaluator, optimizer, supervisor, promotion code, and Step5d consumer
are hash-bound into the promotion. The generated JSON and `.env` overlay map the four outer
loop parameters to `STEP5D_FORCE_P_GAIN`, `STEP5D_FORCE_I_GAIN`,
`STEP5D_FORCE_DAMPING`, and `STEP5D_NORMAL_FILTER_ALPHA`. The overlay never
changes Step5d state and never authorizes a live run.

Offline implementation acceptance does not authorize contact. Before an
infinite session, activate the dedicated owner-policy exception, freeze the
composite fingerprint, and run Review v3 1+1. Per the user's explicit choice,
the physical gate starts with one operator-supervised 60 s baseline rather
than 2 s and 10 s duration clones. Every subsequent trial remains serialized,
returns HOME, and keeps the same live/contact authorization boundary.
