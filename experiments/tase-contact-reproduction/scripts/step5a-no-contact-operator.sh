#!/usr/bin/env bash
set -euo pipefail

EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5a/step5a_cycloid_no_contact_v3.urp"

cat <<EOF
Step5a no-contact cycloid handoff

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - No-contact fixed-Z cycloid rehearsal.
  - Fixed base Z is 0.029423891 m.
  - Duration is 22.0 s with 0.009 m/s command velocity cap.
  - v3 physical path gate matches shifted drag-teach start/mid/end.
  - No Kunwei bridge, no force control, no contact search.
  - No zero_ftsensor(), no TCP/payload write.
  - Do not press Play until the operator has explicitly accepted the run.

This helper is only a handoff note; it does not connect to the robot.
EOF
