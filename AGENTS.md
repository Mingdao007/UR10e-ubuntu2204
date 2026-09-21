# Contact-yield recovery contract

This worktree owns the existing contact recovery path. Every runtime fault,
guard trip, timeout, stale sensor, controller stop, Dashboard stop, or
Protective Stop must enter the recovery owner and automatically attempt the
approved Home.

- Ordinary faults: quiesce, use the approved relief/clearance route, then
  dispatch monitored Home and verify the final pose.
- Protective Stop: collect fresh stationary Dashboard/RTDE/force/torque and
  video evidence, perform the governed one-shot unlock when safe, then use the
  same relief/clearance/Home route. Do not wait for a human to press Home.
- A direct Home planner rejection (including SO(3), position, or corridor
  bounds) is an intermediate result. Continue with the approved staged relief,
  vertical clearance, alternate verified IK, bounded replan, and retry path.
  Do not silently finish away from Home and do not widen a safety bound blindly.
- `home_commandable=false` alone is never terminal. Report `BLOCKED` only after
  every approved recovery stage and retry has been attempted and recorded with
  the missing physical predicate, last pose, safety state, and next action.
  The expected terminal state is `Home verified`.

Keep recovery single-owner and preserve all raw receipts. Offline evidence or a
package check cannot substitute for final physical Home verification.
