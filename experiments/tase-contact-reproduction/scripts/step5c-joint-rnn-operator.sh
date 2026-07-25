#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5c/archive/step5c_joint_rnn_cycloid_v1.urp"

usage() {
  cat <<EOF
Usage:
  step5c-joint-rnn-operator.sh prep-long-checks
  STEP5C_CONFIRM='LIVE STEP5C JOINT RNN CONTACT RUN' step5c-joint-rnn-operator.sh contact-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - BLOCKED: previous step5c_joint_rnn_cycloid_v1 was DLS/IK, not strict TASE RNN.
  - This wrapper refuses all live contact bridge starts until strict RNN paper-truth gates pass.
  - The controller target is a stop-only quarantine package.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    echo "refusing Step5c joint-RNN prep: strict RNN is not implemented; contact is quarantined"
    exit 40
    ;;
  contact-bridge)
    echo "refusing live Step5c joint-RNN contact bridge: strict RNN is not implemented; contact is quarantined"
    exit 40
    ;;
  *)
    usage
    exit 2
    ;;
esac
