#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
SOURCE_RUN_DIR="${1:-}"

if [[ -z "${SOURCE_RUN_DIR}" ]]; then
  while IFS= read -r candidate; do
    if [[ -f "${candidate}/step5a_cartesian_cycloid_motion.json" ]]; then
      SOURCE_RUN_DIR="${candidate}"
      break
    fi
  done < <(find "${RUN_ROOT}" -maxdepth 1 -type d -name "no_contact_test_*" | sort -r)
fi

if [[ -z "${SOURCE_RUN_DIR}" ]]; then
  echo "No no_contact_test_* run with step5a_cartesian_cycloid_motion.json was found under ${RUN_ROOT}" >&2
  exit 66
fi

if [[ ! -f "${SOURCE_RUN_DIR}/step5a_cartesian_cycloid_motion.json" ]]; then
  echo "Selected run is missing step5a_cartesian_cycloid_motion.json: ${SOURCE_RUN_DIR}" >&2
  exit 66
fi

echo "source_run_dir=${SOURCE_RUN_DIR}"
exec "${ROOT}/step5a_live_return_to_anchor.sh" "${SOURCE_RUN_DIR}"
