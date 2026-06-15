#!/usr/bin/env bash
set -euo pipefail

EXPECTED_PROGRAM="/programs/andyl/kunwei/step6/step6a_eight_no_contact_v1.urp"

cat <<EOF
Step6a no-contact 8-shaped handoff

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - No-contact fixed-Z 8-shaped rehearsal.
  - Fixed base Z is 0.029423891 m.
  - Duration is 30.0 s with 0.009 m/s command velocity cap.
  - Safe frame comes from five Step6 waypoint captures.
  - No Kunwei bridge, no force control, no contact search.
  - No zero_ftsensor(), no TCP/payload write.
  - This helper is only a handoff note; it does not connect to the robot.
EOF
