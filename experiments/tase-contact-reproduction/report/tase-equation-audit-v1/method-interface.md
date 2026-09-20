# Shared offline method interface

`tools/contact_method_registry.py` is independent of the discontinued hardware
entry. `MethodRegistry.register(MethodSpec(...), factory)` admits additional
controllers without editing a three-method enumeration. Registration does not
prove that a backend is installed or verified; initialization raises if its
source is unavailable.

Built-in names are SFC, SFC_RADIAL, DSFC, MSFC, TASE_RNN,
TASE_RNN_MATURE_MINUS and TASE_QP. TASE_RNN explicitly selects the printed
Eq.23 sign variant (`J.T @ lambda_state` projection and `lambda_state +=`
update) with the identified UR10e outer-loop adaptation. It is not labeled a
fully literal or successful paper reproduction. TASE_RNN_MATURE_MINUS
explicitly selects the existing `lambda_state -=` adaptation. TASE_QP changes
only the solver on the same TASE outer configuration. All three are now
integrated as transport-free offline adapters; the yield live-entry registry
continues to mark TASE_RNN and TASE_QP unavailable and no live eligibility is
promoted.

Each initialized handle offers step(observation, reference, dt), snapshot(),
restore(state), stop() and close(). Snapshots include method identity, adapter
source digest, lifecycle/sample-clock state and the backend's full controller
snapshot. Failed computations or invalid output restore the pre-step state.
Stopped handles refuse further steps unless an explicit saved state is restored.
No handle opens a device or writes a robot command.

The common observation supplies time_s, state_age_s, base position_m, rotation,
joint_position_rad, calibrated jacobian, raw_force_base_n, raw_torque_base_nm,
linear_velocity_base_m_s, angular_velocity_base_rad_s and joint velocity bounds.
The raw-force field is the controller input after any documented shared upstream
compensation; this adapter neither estimates a baseline nor invents sensor data.
The TASE adapter rotates this shared wrench back into TCP coordinates so its
configured force filter remains active in both RNN and QP modes. Its own wrench-
direction normal remains distinct from the proposal's estimator.

The common reference supplies position_m, velocity_m_s, reference_force_n and
phase/path_time_s for the native dynamics. In particular, reference_force_n is
mapped to the native force_n field instead of silently accepting its default.
Evaluator surface truth is rejected. Nonzero software injection must be composed
explicitly upstream; it is not silently ignored for a subset of methods.

Validation at this checkpoint: the focused registry integration tests cover
offline discovery, initialization of all three TASE names, printed-versus-
mature sign metadata, independent private state, full snapshot/replay and
stop behavior using a calibrated non-identity UR10e Jacobian. The existing
native registry tests remain green. These are numerical/interface checks, not
closed-loop contact or live results.

## Subsequent review checkpoint

The common handle now rejects finite but out-of-bound qdot without clipping,
and restores its pre-step state. Eight registry tests pass, including a custom
extension that attempts .06 rad/s under a .05 rad/s limit.

Main independently ran the Astra draft's 40 baseline/dependency tests using the
fixed contact Python environment. An additional malformed-snapshot probe found
that missing qp_state is rejected only after tick changes from 0 to 19. The
exact source digest and result are retained in restore-atomicity-finding.json.
This is an unresolved draft restore bug, not a passing atomicity claim.

The main registry was also exercised against the isolated draft for both RNN
signs and native QP, with a shared aligned frame and a 1 mm/s tangential task.
All three passed one-step save/replay, and their outer commanded twists matched.
Results are in draft-registry-review.json. The RNN first output remains zero
from its zero initial state; the QP output approximately matches the task twist.
This limited interface check does not establish RNN convergence, a controller
ranking, or main-worktree integration. No hardware was accessed.
