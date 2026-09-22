# TASE, tangential SFC, and joint-space SFC feasibility

Evidence scope: offline source inspection, a sealed rate400 data reduction, and
a deterministic common-input solver characterization. This is not a new
physical acceptance or a live joint-space controller.

## Contact task and control authority

The current physical task is 5 N end-effector contact on the Figure-eight
surface. Its live provider is `mature_local_tase_rnn`; the contact-six TP
filename does not change that solver identity. The existing offline fusion
module assigns normal force and orientation to TASE and only the tangent plane
to SFC during PATH:

\[
P_n=nn^\top,\qquad P_t=I-P_n,\qquad
v_{task}=P_n v_{TASE}+P_t v_{SFC}.
\]

The contact normal is expressed in the measured task frame. A fixed world XYZ
split would change authority when the surface normal tilts. The offline
bounded QP is one optional Cartesian-to-joint realization of this task; it is
not the current live TASE solver.

## What the whole-body manuscript contributes

The unpublished *Whole-Body Shear-Thickening Fluid Control for Enhancing
Collision Safety and Grasp Stability* manuscript is at
`/home/andy/Zotero/storage/WTV6QDRK/T_ASE2026_Collaboration_Safety.pdf`.
Equations (17)--(18) map external joint torque to joint acceleration/velocity,
so the JSFC interaction is admittance-like. Equation (19) subsequently forms a
joint torque command, and Eq. (26) is a QP for velocity bounds and reduced
end-effector displacement. Its dual-arm internal-force regulation is a
separate task, with dual-arm evidence in simulation and single-arm hardware
evidence on Unitree G1. The manuscript's torque/QP derivation should not be
treated as mathematical closure for a UR10e implementation.

An elbow push requires an independently supported external joint-torque or
link-force estimate and a contact-location reference. The current Kunwei
sensor measures the wrench at the tool; it does not by itself identify a force
on an intermediate link. The existing GMO code is observer-only and requires
calibration, dynamics, and timestamp lineage. Those inputs and a live command
route have not been qualified by this study.

At the recorded approved Home configuration, the calibrated UR10e TCP
Jacobian has singular values approximately
`[1.922, 1.462, 1.020, 0.458, 0.292, 0.228]`: rank six, nullity zero.
Therefore exact six-dimensional end-effector tracking leaves no local joint
nullspace for independent elbow yielding at that pose. Prioritizing one normal
translation plus three orientation axes leaves at most two task directions,
which the two tangent directions may use. This is a local kinematic result,
not a claim about every robot pose.

## Matched-input solver check

`tase_rnn_qp_shared_input_20260923.json` reuses the existing calibrated
registry characterization with identical outer-loop inputs and joint bounds.
In the feasible nominal case at 2 ms, mature-minus RNN and QP each accepted
100/100 prescribed steps with no active joint bound; the shared outer task
matched to numerical precision. In the orientation-stress case the
unconstrained task was infeasible for all 100 steps: QP rejected the first
step, while RNN continued with active bounds on 99 steps. These prescribed
inputs show solver behavior under feasibility and conflict. They cannot rank
closed-loop force tracking or justify a live QP switch.

First-stage use of whole-body ideas is thus limited to offline analysis of
joint torque observability, task rank, and the normal/tangent authority split.
Any elbow-contact trial must have its own perturbation location, target,
observer calibration, guards, and evidence identity rather than being scored
as another end-effector 5 N attempt.
