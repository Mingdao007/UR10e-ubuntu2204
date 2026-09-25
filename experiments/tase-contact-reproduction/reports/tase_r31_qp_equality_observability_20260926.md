# R31 TASE-QP hard-equality observability

Generated 2026-09-26 00:39 HKT from sealed R31 PATH command timelines.

## Result

Every recorded six-by-six Jacobian was full column rank in both frozen QP attempts. The singular-value rank test used float64 SVD and `s_i / s_max > 1e-10`; the analyzed row counts matched the sealed PATH row counts and both command-timeline hashes matched their seals.

| Attempt | PATH rows | Rank-6 rows | Minimum raw singular value | Median raw condition number | Existing qd gate |
|---|---:|---:|---:|---:|---:|
| QP BO-10/B | 29,995 | 29,995 | 0.211754 | 8.50673 | 0.840132, failed |
| QP matched R31 A | 29,956 | 29,956 | 0.211763 | 8.50669 | 0.848607, failed |

Full quantiles, exact source paths, and hashes are in [the machine-readable result](tase_r31_qp_equality_observability_20260926.json).

## What this means for the QP objective

The live QP imposes the six exact equalities `J qdot = twist`. For a full-rank square Jacobian, those equalities already determine a unique `qdot` whenever the box bounds permit a solution. Replacing the identity Hessian with another positive-definite weight, or penalizing change from the previous `qdot`, cannot smooth that exact feasible solution: there is no remaining nullspace for the objective to choose from. If the exact task equality conflicts with a bound, objective weights alone cannot make the problem feasible.

This matches the existing replay evidence: adding the recorded per-joint slew limits to the hard-equality QP made the first PATH tick infeasible, while applying a scalar slew after the solve introduced a Cartesian equality residual. A smoother live QP therefore requires a task-priority or explicit relaxation design, with new, reviewable slack/dynamic parameters. None are selected here, and no frozen profile, safety bound, tolerance, or motion gate changed.

The singular values establish numerical rank for the logged Jacobians, not a physical root cause for poor `actual_qd` tracking. Raw condition numbers mix translational and rotational units and are descriptive only. Both live QP attempts remain failed at the unchanged 0.900 actual-velocity gate; their force MAEs remain diagnostic.

The robot did not move during this analysis. The fresh Dashboard/RTDE preflight remains recorded separately; the current QP is not eligible for an unchanged live retry.
