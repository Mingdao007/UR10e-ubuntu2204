# TASE Registry Characterization

This is a bounded offline comparison through the common method registry. It
uses the calibrated UR10e FK/Jacobian, identical Step5d outer configuration,
prescribed force/reference traces, and identical `+/-0.15 rad/s` bounds for
`TASE_RNN` (printed Eq.23 plus), `TASE_RNN_MATURE_MINUS`, and `TASE_QP`.
It performs a 2 s run at 2 ms and 1 ms, with private method state per run.

## Command

From the repository root:

```bash
PYTHONPATH=experiments/tase-contact-reproduction/tools \
python3 experiments/tase-contact-reproduction/tools/characterize_tase_registry.py \
  --output experiments/tase-contact-reproduction/report/tase-registry-characterization-v1/characterization.json \
  --duration-s 2.0 --dt-s 0.002 \
  --build-qp-dir /tmp/tase-registry-characterization-qp
```

The command builds the existing offline QP library and writes the reproducible
summary to `characterization.json`. It performs no robot, endpoint, bridge, or
motion operation.

## Frozen Cases

- `aligned_nominal`: normal is near `-R_current[:,2]` with a small changing
  tilt. The q trajectory has analytic `qdot`; measured linear/angular twist is
  `J(q) * qdot`, and the position-reference velocity is its analytic
  derivative.
- `orientation_task_stress`: retains the original world-Z-near normal against
  the arbitrary `BASE_Q` orientation. It is intentionally kept to expose task
  infeasibility. The QP status-3 boundary is not a solver disadvantage.

Each case freezes the outer task before solver comparison and records desired/
current orientation angle, `xdot_c`, Jacobian singular values, unconstrained
`solve(J, xdot_c)`, and bound feasibility. No pseudoinverse, clipping, or
fallback is used.

## Observed Offline Result

In the aligned case, all three methods execute both time steps. The printed
plus RNN remains within the registry bounds but its equality residual grows to
about `0.7096` and reaches active bounds frequently. Mature-minus ends near
`0.0011`; QP remains near `1e-9`. These are prescribed solver residuals, not
force or physical-contact results and do not establish superiority.

In the stress case, the first frozen task has orientation error about
`0.81503 rad` and unconstrained bound violation about `1.82828 rad/s`; all
frozen tasks are infeasible under the unchanged bounds. QP reports status 3 at
the first tick, while the RNN variants continue with saturated bounded output
and nonzero task residual. This separates exact-task infeasibility from RNN
residual behavior.

The invalid freshness input (`state_age_s=0.08`) is rejected by the common
registry for every method, with state unchanged after the rejection.

## Equation Boundary

The local PDF audit places Eq.23 on PDF page 6. The printed mapping is
`P_Omega(J.T @ lambda_state)` with
`epsilon * lambda_dot = J @ theta_dot_state - xdot_c`; the local audit records
the Eq.23 lambda state as opposite in sign to the standard Eq.21 multiplier.
The registry therefore keeps printed-plus and mature-minus as explicit names.
The continuous paper equations/proof do not establish this explicit-Euler,
bounded, changing-J UR10e implementation as a full original-TASE reproduction.

Source evidence is the JSON artifact beside this file and
`report/tase-offline-baselines-v1/pdf-equation-parameter-audit.json`.
