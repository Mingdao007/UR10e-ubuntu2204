#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5c_speedj_dryrun_v1.urp"

usage() {
  cat <<EOF
Usage:
  step5c-speedj-dryrun-operator.sh prep-long-checks
  STEP5C_CONFIRM='LIVE STEP5C SPEEDJ DRY RUN' step5c-speedj-dryrun-operator.sh joint-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - BLOCKED: 2026-06-13 live dry-run showed wrong XY/Z motion from DLS/Jacobian mapping.
  - This wrapper refuses live dry-run bridge starts until the joint mapping is fixed offline.
  - The controller target is a stop-only quarantine package.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    echo "refusing Step5c speedj dry-run prep: DLS/Jacobian mapping is quarantined after wrong XY/Z live motion"
    exit 40
    ;;
  joint-bridge)
    echo "refusing live Step5c speedj dry-run bridge: DLS/Jacobian mapping is quarantined after wrong XY/Z live motion"
    exit 40
    ;;
  *)
    usage
    exit 2
    ;;
esac
