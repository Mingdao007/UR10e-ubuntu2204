# Human-present contact task: joint and end-effector response

Status: problem formulation and offline implementation only. No robot motion,
apparatus-contact qualification, workpiece-protection claim, or human trial is
established by this report.

## Prior work and the question

The direct comparison is Jiang et al., *Multi-hierarchy interaction control of
a redundant robot using impedance learning*, Mechatronics 67 (2020), 102348,
DOI 10.1016/j.mechatronics.2020.102348, Mac Zotero item `A92546NZ`.
[Confirmed publisher abstract](https://www.sciencedirect.com/science/article/pii/S0957415820300283)
describes a 7-DoF Sawyer robot with end-effector stiffness and damping
learning and low-stiffness null-space compliance for body contact. Thus neither
"joint plus end-effector impedance" nor "variable damping" alone is a novelty
claim. The separate Yiming Jiang coauthored minimally invasive surgery paper
is outside the direct task comparator set.

The proposed task is human-present, 5 N contact motion on the existing board.
Intentional 5 N contact leaves a visible trace. The workpiece endpoint is
therefore **additional mark outside the intended trace corridor**, including
marks made while the arm swings, yields, or recovers after a person touches a
link or the tool. Human safety, existing hard guards, and verified recovery
take precedence over the workpiece endpoint; workpiece protection takes
precedence over completing the tangent trajectory.

Let `q` be the six UR10e joints, `x(q)` the tool pose, `n` the measured surface
normal, and `Pn = n n^T`, `Pt = I - Pn`. In nominal PATH the existing TASE outer
loop owns normal force and posture while tangential SFC is a candidate path
response. A qualified link-contact observation can request virtual
joint-space admittance; the robot's velocity interface is not torque-level
impedance control. During collision, tangent tracking yields first. If loaded
tool displacement leaves the qualified mark corridor, a guard trips, or the
observation is stale, the policy requests the single recovery owner. It does
not compute an independent live motion command.

At a full-rank six-dimensional tool task there is no separate joint null space
on this six-axis arm. The offline policy retains one normal translation and
three posture axes and projects a joint-yield preference into their remaining
directions. This deliberately allows tangential task degradation and is not a
copy of Sawyer's 7-DoF null-space controller. A final bounded velocity
realization and live safety proof remain separate work. The image corridor is
in pixels; using it as an online lateral-error bound also requires a verified
camera-to-board/tool geometric calibration. No such calibration is claimed
here. The virtual inertia, stiffness, and damping are explicit offline inputs,
not selected live gains or a qualified variable-damping law.

## Testable hypotheses and endpoints

1. For matched, instrumented link perturbations, a dual-space candidate can
   reduce the apparatus force peak/impulse and loaded tangent slip relative to
   TASE-only while retaining the existing hard limits. These are apparatus
   proxies, not evidence of human injury risk.
2. For matched link and tool perturbations, the dual-space candidate can
   reduce new off-corridor mark area and maximum mark distance without hiding
   normal-force error, posture error, contact loss, guard events, or Home
   recovery time. A candidate may instead need to stop/retreat; preserving
   the original 60 s PATH is not required after a safety event.
3. Any gain over end-effector-only and joint-only ablations must be shown
   against matched perturbations and the same final velocity bounds. QP and
   RNN are realization alternatives, not hypotheses about force quality.

Primary image measures are newly visible off-corridor area and maximum
distance from the corridor. Use fixed camera pose, lighting, scale/fiducials,
and before/after registration. Ten unperturbed 5 N runs establish normal mark
variability and image registration error; each requires a distinct run ID,
capture time window, and image pair. If existing marks saturate the
region or image registration cannot resolve new marks, mark evidence is
unqualified and **no no-damage claim is allowed**; force, slip, and pose remain
proxies only. The historical five resident sessions have no available RTSP
video, so they cannot establish this endpoint. A read-only 2026-09-23 probe of
`rtsp://127.0.0.1:8554/arm` returned 404: the relay was present but its
publisher was unavailable. No instrumented pusher configuration was found in
the scoped experiment configuration/report files.

The collision protocol is separate from the legacy 60 s, 5--60 s bin-mean
force MAE. It scores event-aligned impact `[0,0.5) s`, swing `[0.5,5) s`, and
recovery `[5,10) s` windows. Report pusher peak/impulse, workpiece normal
force peak/MAE, load-weighted tangent slip, lateral/posture error, contact
loss, mark evidence, guard trips, and verified Home. The old 48 sealed
attempts remain a no-disturbance reference: 47 complete PATH, 45 eligible,
and seven with RTDE below 460 Hz. A new trial is comparison-eligible only
with aligned clocks, complete event windows, visual detectability, single
writer, Home verification, and measured RTDE/TP path and event rates of at
least 460 Hz; safety incidents remain visible even if performance is
ineligible. No hardware frequency guarantee is inferred.

## Implemented offline seams

- `tools/tase_collision_policy.py` stages nominal, link-yield, tool-hold,
  and recovery-request intent. Link-yield requires fresh, admitted joint
  observer lineage and never emits hardware commands. Only fresh verified
  joint Home can clear the recovery state.
- `tools/tase_collision_trial.py` accepts a distinct apparatus trial identity,
  scores event windows, retains failed safety outcomes, and compares at least
  ten waveform-matched baseline/candidate pairs with deterministic bootstrap
  intervals. It will not label apparatus evidence as a human trial or merge it
  into the old 5--60 s MAE.
- `tools/measure_contact_board_marks.py` is the offline image endpoint. Its
  registration and detectability receipt must qualify before any surface
  claim is attached to a trial.

These modules are offline analysis and decision interfaces. They are not
connected to `figure8.sh`, a TP package, RTDE, Kunwei transport, or the live
single writer.

The offline CLI sequence is: `admit_tase_board_corridor.py --manifest ...
--output ...` for ten independent no-disturbance image pairs;
`measure_contact_board_marks.py --before ... --after ... --corridor-mask ...
--output ...` for one candidate pair; then `tase_collision_trial.py --trial
... --output ...` for event-aligned scoring. `tase_collision_trial.py
--results ... --candidate-arm dual --output ...` compares an array of scored,
matched apparatus results. The trial binds the same mask digest, a corridor
admission made before its start, a calibrated positive apparatus trigger
threshold that the impact window must actually cross, and measured rates. A qualified zero image
finding means no *visible* off-corridor change above this detector's
sensitivity; it cannot prove absence of microscopic or subsurface damage.

## Physical-stage sequence and gates

1. Restore and verify an actual camera publisher and obtain ten paired
   before/after no-disturbance views on the existing board. Freeze image
   alignment, nominal corridor, and detectability criteria before viewing
   candidate collision outcomes.
2. Calibrate motor-current-to-torque and dynamics/timestamp lineage; compare
   the existing observer-only GMO against an independent instrumented pusher
   at the link and tool. Sign, false alarms, detection latency, and contact
   location ambiguity are admission evidence. Until qualified, the link
   estimate cannot drive the policy.
3. Freeze a bounded, independently measured perturbation waveform after
   no-workpiece graded checks. Test link and tool sites in tangent and inward
   normal directions without workpiece contact first. Only the existing
   recovery owner handles guards, timeouts, stop, and Protective Stop through
   the approved relief/clearance/Home ladder.
4. On the board, randomize matched TASE baseline and candidate runs for each
   site/direction, with at least ten valid pairs per comparison. Compare
   end-effector-only, joint-only, and dual-space candidates under the same
   waveform and limits. Report every excluded trial and every incident.
   A workpiece-protection claim needs qualified image evidence, zero newly
   detected off-corridor marks in eligible candidate trials, and a favorable
   paired confidence interval against the baseline. If the original board
   cannot resolve new marks, stop this claim and revisit the workpiece choice.
5. A later human-contact protocol requires a separate applicable safety
   review and fresh device qualification after apparatus results. No human
   contact or live robot motion is authorized by this offline work.
