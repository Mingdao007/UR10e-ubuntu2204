# TASE recovery lessons, 2026-09-21

This record preserves the observed trigger, root cause, repair, replay status,
and stop condition. It does not convert a repaired software path into physical
acceptance.

| Attempt | Trigger and evidence | Root cause / repair | Status |
|---|---|---|---|
| 01 | Provider output envelope violation near 6.45 N; automatic Home completed | The provider did not enforce the mandatory host envelope before return. Added bounded same-direction projection as an explicit opt-in and named the violated fields. | Focused replay PASS; original attempt remains failed. |
| 02 | Provider joint delta reached the strict host slew boundary near 8.91 N; Home completed | Strict `>` comparison rejected an equal-to-boundary value at floating-point ulp. Tightened only the internal numeric margin; host gate unchanged. | Focused replay PASS; original attempt remains failed. |
| 03 | Force norm near 19.99 N while solved `Jqdot` still pointed inward; Home completed | Force-rise warm-start episode did not rearm a pressing-direction correction. Added a direction-checked same-tick re-solve; no force or safety limit was widened. | Focused replay PASS; original attempt remains failed. |
| 04 | TP resident stopped with reason 48 after the post-latch timeout; force-norm peak about 14.28 N | Stopped pose was 10.83 mrad from Home. Existing relief/Home scripts reject more than 10 mrad, so Home could not be verified without changing a protective corridor. | HARD BLOCKED; do not widen the gate or retry unchanged. |
| 05 | Qualification replay at packet `188318` reported a raw-sensor guard rejection; native force norm was `18.2603 N`, while baseline-corrected control wrench reached `20.3002 N` | `TaseContactProvider._observe` passed the baseline-corrected wrench into the raw guard because `SensorPacket` did not retain the native sample. Added a typed `raw_wrench` field; the unchanged 20 N/2 Nm guard now uses native Kunwei data while the corrected wrench remains the control/diagnostic signal. | Offline replay and focused provider test PASS; original attempt remains failed. |
| 06 | Fresh read-only preflight on the current contact-six route passed package/read-back, Dashboard, RTDE, Kunwei, network, realtime, and single-writer checks; current Home pose remains `10.72 mrad` from the approved target | Added a route-specific read-only binding check for `contact-readback-selection-v2` and corrected the canonical realsetup skill path. No live gate was relaxed; the existing Home owner still rejects `>10 mrad`. | Admission preflight PASS; Home remains BLOCKED and no ARM/Play issued. |
| 07 | Fresh preflight replay gives position error `12.43 mm` and SO(3) error `10.724 mrad` against the approved Home | Existing `plan_home_recovery` rejects the pose before commandability; no approved recovery geometry currently covers this state. | Durable blocker receipt recorded; do not retry unchanged or widen the corridor. |
| 08 | Offline replay of the failed run's 14,918 BASELINE ticks found 14,226 (`95.36%`) with realized `Jqdot` above the `2e-6 m/s` tangential or `2e-6 rad/s` angular residual tolerances; peaks were `0.566 mm/s` and `1.796 mrad/s` | The TASE provider branch did not apply the canonical baseline residual hold to solved commands. Added a baseline-only zero-command hold after the force-preempt direction retry, retained residual telemetry, and kept the solver state advancing; no envelope or safety limit was widened. | Offline replay and focused tests PASS; original attempt remains failed and the repair requires a fresh qualification/path attempt. |

The fourth attempt is the current hardware stop condition. All live writers,
bridge processes, and control ownership were stopped. Offline work may proceed;
the next physical action requires fresh evidence for the Home corridor and a
new diagnostic, not a repeated attempt.
