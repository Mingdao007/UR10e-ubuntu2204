# GM-v1 mechanism hypothesis (not a stability certificate)

Scope: development data. No new hardware result or final holdout is used.
The SFC baseline and DSFC/MSFC proposal comparison remains unchanged. These
four MSFC-labelled cells are a bounded mechanism ablation, not four new
competitors or extra formal tuning allocations.

The internal native state is `w`, and command velocity is `g*w`. At fixed
memory metric `A`, native MSFC uses damping

```
D(w) = a*||w||^(p-1)*w + mu*(w^T*A*w)^((n-1)/2)*A*w.
```

In a one-direction restriction along an eigenvector of A, with eigenvalue
lambda and nonzero scalar w, its incremental damping is

```
d = a*p*abs(w)^(p-1) + n*mu*lambda^((n+1)/2)*abs(w)^(n-1).
```

Thus the native memory can reduce the superlinear incremental damping, even
though its metric remains positive definite. Positive damping alone does not
certify a delayed sampled contact loop. This is the specific reason to test
memory coupling and command gain separately rather than attribute all observed
sensitivity to the presence of memory.

For an illustrative scalar contact model, let x increase into the surface,
delta_f = -K*x be restoring force, use an ideal velocity servo x_dot=g*w,
freeze memory, and retain only the common force-filter time constant tau.
Linearization gives the characteristic polynomial

```
tau*m*s^3 + (m+tau*d)*s^2 + d*s + g*K = 0.
```

For positive coefficients, the remaining cubic Routh condition is

```
(m + tau*d)*d > tau*m*g*K.
```

This predicts that increasing output gain or contact stiffness can overwhelm
incremental damping in the delayed loop. It does NOT include the actual robot
servo, sensor lag, moving normal, tangential coupling, memory evolution,
contact damping, discrete native solver, QP interventions or saturations.
It is a falsifiable mechanism hypothesis, not a bound qualifying this platform.
The native coefficient and output gain must not be conflated by replacing w
with robot command velocity in the original equation.

GM-v1 therefore holds the common outer loop, initial state protocol, scene,
controller interval and constraints fixed, and crosses:

- output gain: original selected value versus half;
- mechanical memory coupling: original beta versus beta=1, so A=I exactly;
- contact integration: four versus eight plant substeps at fixed 2 ms control.

With beta=1 the h and S states still evolve and are recorded. A focused native
test compares its command to DSFC with matched m,g,p,a,n,mu over forcing and
release. Therefore any difference between the on/off cells is not introduced
by a state reset or an unrelated damping parameter change.

Report actual contact load, path and orientation errors, progress, saturation,
QP interventions, matched release recovery and full-state records for every
cell. Alongside unaligned pointwise refinement errors, report final-window
force standard deviation and sampled dominant frequency. Similar aggregate
metrics or a plausible phase shift do not erase the unaligned discrepancy or
certify trajectory convergence. Any phase diagnostic is explanatory only.

All inputs in this study have been inspected or used for design. They cannot
later be relabelled independent final validation. No winning method is selected
by this ablation; any retained candidate still requires a matched common-task
comparison and new independent evaluation.
