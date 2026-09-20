# TASE printed RNN sign audit

Source: Xu et al., IEEE TASE 23 (2026), DOI 10.1109/TASE.2025.3637540,
printed pp. 568-569, Eqs. (21)-(23). The supplied local PDF was read directly;
its SHA256 is recorded in source.json. This is an equation audit, not a robot
experiment, reproduction success, or comparison result.

## The two signs must be identified together

Writing v for the joint-velocity state and u for the commanded task twist,
the printed Eq. (21) is L = v^T v / 2 + lambda^T (Jv - u). Its derivative with
respect to v is v + J^T lambda. Consequently, Eq. (22a) would project
-J^T lambda. Printed Eq. (23a) instead simplifies to a projection of
+J^T lambda, while printed Eq. (23b) retains lambda_dot = (Jv - u)/epsilon.

This difference is material. Replacing lambda with -lambda transforms BOTH
J^T lambda and lambda_dot. Changing only the dual update sign while retaining
the positive projection argument is not an algebraically equivalent rewrite
of the printed Eq. (23) pair. It is a distinct sign-consistent implementation
variant, equivalent to a negative multiplier convention for Eq. (21).

## Scalar counterexample for the printed pair

Consider a constant nonsingular scalar task J=1, bounds Omega=[-b,b],
0 < u < b, epsilon > 0, 0 < r <= 1, and initial states v=lambda=0.
The printed Eq. (23) pair reduces to

    epsilon * v_dot = -sigr(v - clip(lambda, -b, b), r)
    epsilon * lambda_dot = v - u.

The region -b <= v <= 0, lambda <= 0 is forward invariant:

- At v=0 and lambda<=0, v_dot<=0.
- At v=-b and lambda<=0, v_dot>=0 because clip(lambda,-b,b)>=-b.
- At lambda=0 and v<=0, lambda_dot=(v-u)/epsilon<0.

The initial state lies in that region and immediately has lambda_dot<0.
Thus v cannot approach the feasible positive solution u. In fact the task
residual v-u remains <= -u. The same scalar case embeds in a diagonal
six-dimensional task. This is a counterexample to convergence of this
specific printed differential-equation pair under these conditions; it does
not establish what equations or software produced the paper's experiments.

For r=1 in the unsaturated region, the linear state matrix is
[[-1,1],[1,0]]/epsilon, with eigenvalues (-1 +/- sqrt(5))/(2*epsilon).
The positive eigenvalue independently confirms the local sign problem.

## Required comparison labels

Keep an explicit printed-sign variant, with its initialization, discretization
and observed failure recorded. Keep the existing mature-minus implementation
under an adaptation label. TASE-QP must use the same selected outer-loop
mapping, force processing, parameters and constraints as its paired RNN case.
A printed-sign failure is not evidence that a proposal outperforms a successful
TASE reproduction. The outer-loop frame/angle ambiguities remain separate.

The current draft also limits each discrete velocity-state increment to avoid
overshooting the projected target. That is an implementation choice beyond the
continuous printed differential equation and must remain visible in the source
map. This audit does not resolve its discrete-time accuracy or tune it away.
