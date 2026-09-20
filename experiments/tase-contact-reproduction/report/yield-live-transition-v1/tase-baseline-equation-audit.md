# TASE baseline equation audit

Source: the local original PDF, printed pp. 566-569, visually checked Eq. (23) on PDF page 6 (printed p. 569). This is an implementation-fidelity audit, not a claim that our proposals outperform the paper.

## Confirmed discrepancies

- Eq. (23a) simplifies to `epsilon * s_dot = -sig_r(s - clip(J.T @ lambda))`.
- Eq. (23b) prints `epsilon * lambda_dot = J @ s - xdot_c`.
- For the allowed case `r=1`, constant scalar `J=1`, constant feasible `xdot_c`, and inactive bounds, the error dynamics have matrix `A=[[-1,1],[1,0]]/epsilon`. One eigenvalue is `(sqrt(5)-1)/(2*epsilon)>0`. Thus this literal inner solver is not asymptotically convergent even in that interior example. This does not by itself characterize the full robot/environment closed loop.
- `tools/step5c_strict_rnn.py` instead updates `lambda -= dt/epsilon * (J@s-xdot_c)`. This is a sign-corrected convention consistent with a positive `J.T@lambda` projection. It is not a literal transcription of Eq. (23b).
- The existing `config/step5c_tase_paper_truth.json` explicitly remains disabled and records unresolved fields. Its old name is not evidence of a verified live baseline.

## Outer-loop conventions requiring explicit mapping

- Eq. (7) prints `Phi_O=R_d.T@Phi_E`, without a right-hand `R_d`; its coordinate mapping must be specified before treating it as a base-frame orthogonal projector.
- Eq. (10) describes a unit force-direction vector `u`, then writes `sin(u)` and `cos(u)` in a Rodrigues expression. A rotation angle/axis convention is missing from that expression as printed. A force-direction-to-tool-attitude construction on UR10e must be recorded as an interpretation.
- The desired-force sentence uses `Phi_O@[0,0,fd]`, although the preceding motion selection `Phi_E=diag(1,1,0)` would annihilate that input. The force selection convention must be disambiguated explicitly.
- Eq. (13) uses desired-inverse times current orientation and Eq. (14) adds the resulting orientation error. Quaternion order, angular-velocity frame, and error sign must be tested on a one-axis rotation; they cannot be inferred from method naming.
- Eq. (16)/(17) retain mass/damping and force-error integration; TASE-QP must use exactly the same interpreted outer loop and constraint problem as the TASE RNN version. It must not silently switch to the proposal normal estimator.

## Implementation decision

Keep the literal Eq. (23) counterexample as a reproducible numerical diagnostic. Do not enable an unstable literal sign combination on hardware. Implement the interpretable primal-dual convention with a visible `dual_sign_correction` adaptation record, while retaining the paper's finite-time nonlinearity and separate state. Both TASE variants must share force-direction attitude generation, force/motion decomposition, force integral, filters and bounds. Remaining missing parameter choices must be recorded; current SFC timing repair continues independently.

The registry remains unavailable until this full mapping, state interfaces and the paired solver implementation are validated. No baseline success or proposal win is inferred from this audit.
