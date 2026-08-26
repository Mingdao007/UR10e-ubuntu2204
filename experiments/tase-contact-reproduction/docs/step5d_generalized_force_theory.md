# Generalized force outer loop: offline design and proof contract

This note defines the proposed extension of the original scalar normal-force
outer loop.  It is an offline design contract; it does not change the live
bridge, TP program, or the existing paper-form controller.

## 1. Controller structure

Let `n` be the reaction normal expressed in the base frame, `f` the measured
force vector, and `F_d` the desired normal load.  The scalar measurement and
error are

\[
  F_n = n^\top f, \qquad e = F_d-F_n.
\]

The bounded integral state is updated at sample time `h` by

\[
  z^+ = \operatorname{sat}_{[-z_{\max},z_{\max}]}(z+h e),
\]

with conditional anti-windup when the velocity command is already saturated
in the direction of the error.  The normal velocity command is

\[
  v_n^+ = \operatorname{sat}_{[-v_{\max},v_{\max}]}
  \left(v_n+h\left[k_p e+\phi(z^+)-b v_n-k_r\psi(r)\right]\right),
\]

where `r` is a filtered force-rate estimate.  Tangential motion remains the
original projected admittance/position command; the new terms act only in the
scalar normal channel.

The nonlinear integral map used by the offline primitive is

\[
  \phi(z)=k_i z\left(1+\rho\frac{|z|}{\tau+|z|}\right),
  \qquad \rho\ge 0,\;\tau>0.
\]

It is odd and sector bounded:

\[
  k_i z^2\le z\phi(z)\le k_i(1+\rho)z^2.
\]

Thus `rho=0` exactly recovers the linear I term.  An even term such as `z²`
is intentionally not used: it would lose oddness and can apply the same
velocity sign for positive and negative force error.

The rate shape is data-gated and bounded:

\[
  \psi(r)=\operatorname{sgn}(r)
  \tanh\left(\left(\frac{(|r|-\delta)_+}{\sigma}\right)^p\right),
  \qquad p\ge1.
\]

It satisfies `|psi| <= 1` and `r psi(r) >= 0`; `k_r` therefore has the same
acceleration-authority units as the other additive terms.  The deadband
`delta` is chosen from an identified low-rate/noise band.  If the held-out
study does not show a benefit, `k_r=0` is the default, so the added term
disappears.

The implementation is the pure offline primitive
[`step5d_generalized_force_outer_loop.py`](../tools/step5d_generalized_force_outer_loop.py).
It also contains a measured-only normal estimator with unit normalization,
hemisphere continuity, spherical low-pass filtering, and an angular-rate
limit.  The estimator is optional; the existing provided-normal path remains
the parity baseline.

## 2. Continuous-time proof model

For a local contact patch, use the deliberately explicit assumptions

\[
  F_n=k_e x+d_F, \qquad \dot x=v_n,
  \qquad 0<k_e\in[k_{e,-},k_{e,+}],
\]

where `d_F` contains model mismatch, sensor noise, curvature variation, and
the force-rate/filter residual.  In the ideal unsaturated case, with
`z_dot=e`, define

\[
  X=[e,\;v_n,\;z]^\top.
\]

For a sector slope `mu` satisfying
`mu in [k_i, k_i(1+rho)]`, the nominal linear parameter-varying model is

\[
  \dot X=A(k_e,\mu)X+B w,
  \quad
  A=\begin{bmatrix}
    0&-k_e&0\\
    k_p&-b&\mu\\
    1&0&0
  \end{bmatrix}.
\]

Here `w` collects bounded force disturbance, normal projection error, inner
velocity realization error, and any deliberately omitted rate/filter term.
The force-rate term can instead be absorbed into `w` conservatively because
`|k_r psi(r)| <= k_r`; treating it as damping requires the additional
assumption that the rate estimate has a known positive-gain relation to
`v_n`.

## 3. Common-Lyapunov certificate

For an uncertainty box in `(k_e,mu)`, check the four endpoint matrices.  A
common certificate is a symmetric matrix `P` and `Q` satisfying

\[
  P\succ0,\qquad
  A_j^\top P+P A_j\preceq-Q,\qquad Q\succ0,
  \quad j=1,\ldots,4.
\]

The inequality is affine in the two uncertain parameters, so checking the
box vertices is sufficient for this affine model.  With
`V=X^T P X`, bounded `|w| <= w_bar`, and `g=||P B||`,

\[
  \dot V\le-\lambda_{\min}(Q)||X||^2
       +2g\,||X||\,\bar w.
\]

Consequently the nominal unsaturated model is exponentially stable, while
the disturbed model is input-to-state stable and ultimately bounded.  One
explicit conservative state-radius estimate is

\[
  ||X||\le
  \frac{2g}{\lambda_{\min}(Q)}
  \sqrt{\frac{\lambda_{\max}(P)}{\lambda_{\min}(P)}}\,\bar w
\]

outside the transient region.  The helper
[`step5d_generalized_force_stability.py`](../tools/step5d_generalized_force_stability.py)
recomputes all eigenvalue margins from the returned `P`; a failed search is
reported as infeasible rather than converted into a stability claim.

For forward Euler with period `h`, the discrete check is

\[
  P-(I+hA_j)^\top P(I+hA_j)\succ0.
\]

A conservative sufficient bound returned by the helper is

\[
  h < \frac{\lambda_{\min}(Q)}
  {\max_j\|A_j^\top P A_j\|_2}.
\]

This bound is intentionally conservative; the exact discrete inequality is
the acceptance test for a selected sample period.

## 4. Saturation, normal estimation, and inner-loop boundary

The LMI proof is for the unsaturated local model.  The implementation's
velocity clip and conditional integral freeze make the command and integral
state bounded, but they do not imply zero steady-state force error when the
required contact velocity is outside the limit.  A paper theorem should state
the feasible-contact region explicitly and use UUB/ISS wording under bounded
disturbance, rather than claim global asymptotic convergence through
saturation.

For the measured-only normal estimator, assume only

\[
  \|\hat n-n\|\le\bar\epsilon_n, \qquad \|f\|\le f_{\max}.
\]

Then the projection error is bounded by
`|hat n^T f-n^T f| <= f_max epsilon_bar` and enters `w`.  No normal-estimator
parameter-convergence theorem is claimed: the unknown-surface trace does not
provide the observability conditions needed for one.

If the robot/RNN inner layer realizes
`v_actual=v_command+eta` with an ISS bound on `eta`, the outer ISS result gives
an ISS cascade/UUB corollary.  This is the intended robotics-community proof
boundary.  It is not a full six-degree-of-freedom robot stability theorem and
does not reuse an inner KKT/RNN solver theorem as an outer force-loop proof.

## 5. What the current offline evidence says

The trace audit is generated by
[`build_step5d_generalized_force_offline_study.py`](../tools/build_step5d_generalized_force_offline_study.py).
It excludes an entire attempt if any row has runtime phase strategy enabled or
a non-zero `phase_target_correction_n`, before identification or tuning.  The
phase-profile path is therefore treated as a separate ILC-like target-force
feedforward reference, not as evidence for this generalized feedback law.

For the leakage-free 120-attempt `stride50b` trace, the raw filtered-force
finite differences have approximately median `2.03 N/s`, 95th percentile
`10.75 N/s`, and the saturation flags are true on about `58.0%` of rows.  The
trace has no commanded normal displacement or authoritative contact model, so
`k_e`, contact damping, and transport delay are explicitly *not identified*.
The current offline candidate therefore uses a `2.0 N/s` rate deadband and a
bounded `dF/dt` scale of `2.0 N/s`; these are study parameters, not live
acceptance constants.

The synthetic study uses a bounded Kelvin--Voigt family only as an offline
stress test.  Its held-out nominal `0.1 N` threshold is a simulation target,
not physical acceptance.  In the current deterministic sweep, the gated
force-rate candidate is selected by robust hold-out MAE with an additional
bounded 0.7 s disturbance component; the standalone and combined nonlinear-I
candidates are not promoted merely because they are mathematically available.
The generated certificate is conditional on the declared nominal range
`k_e in [90,110] N/m` and the sector range
`mu in [1/12,1.25/12]`; it is not a claim about the unknown physical surface.

The exact raw metric convention remains

\[
  \operatorname{MAE}_{[5,60)}=
  \frac1{550}\sum_{j=50}^{599}
  \left|\frac1{|B_j|}\sum_{t_i\in B_j}F_i-5\right|,
  \quad B_j=[0.1j,0.1(j+1)),
\]

with every bin required and no gap interpolation.  This is the same semantic
boundary as the sealed R013 metric; a trace-only relative-monotonic replay is
labelled non-authoritative unless the path-time field is present.

## 6. References and positioning

The passivity/force-control framing is consistent with classical robot force
control literature such as [Siciliano and Villani (1996)](https://www.sciencedirect.com/science/article/pii/0005109895001735)
and recent unified force-interaction control work such as [Haddadin and
Shahriari (2024)](https://journals.sagepub.com/doi/full/10.1177/02783649241249194).
The proposed contribution is a general, model-free scalar normal-loop
extension with explicit sector/LMI/ISS conditions; geometry is used only for
motivation and offline stress scenarios, not as a runtime CAD prior.
