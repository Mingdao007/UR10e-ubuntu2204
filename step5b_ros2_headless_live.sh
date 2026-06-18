#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
EXPERIMENT="${ROOT}/experiments/tase-contact-reproduction"
LEDGER="${EXPERIMENT}/config/step5b_authorization_state.json"
AUTH_STATUS="${EXPERIMENT}/tools/step5b_authorization_status.py"
RUNNER="step5b_contact_live_runner"
FINAL_TRIGGER_TEXT="RUN STEP5B ROS2 HEADLESS LIVE"

usage() {
  cat <<EOF
Usage:
  step5b_ros2_headless_live.sh status
  step5b_ros2_headless_live.sh dry-run
  STEP5B_FINAL_TRIGGER='${FINAL_TRIGGER_TEXT}' step5b_ros2_headless_live.sh

Boundary:
  - Current route only: ROS2 Remote Control/headless.
  - Uses the locked Step5b specification defaults from the runner/stage table.
  - Does not override target force, path speed, path parameters, force source, or zero policy.
  - No TP/bridge fallback, no URScript send, no zero_ftsensor(), no Kunwei tare/config.
  - Live run requires the exact STEP5B_FINAL_TRIGGER value above.
  - The per-run operator_final_trigger_received gate is set true only during this script run and restored on exit.
EOF
}

source_ros() {
  set +u
  # shellcheck disable=SC1091
  source /opt/ros/humble/setup.bash
  # shellcheck disable=SC1091
  source "${ROOT}/install/setup.bash"
  set -u
}

run_authorization_status() {
  (cd "${EXPERIMENT}" && python3 "${AUTH_STATUS}" "$@")
}

make_run_dir() {
  local stamp
  stamp="$(date +%Y%m%d_%H%M%S)"
  printf '%s\n' "${EXPERIMENT}/runs/step5b_ros2_headless_live_${stamp}"
}

write_operator_trigger() {
  local value="$1"
  python3 - "${LEDGER}" "${value}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = sys.argv[2].lower() == "true"
payload = json.loads(path.read_text(encoding="utf-8"))
payload.setdefault("open_gates", {})["operator_final_trigger_received"] = value
path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
PY
}

read_operator_trigger() {
  python3 - "${LEDGER}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("true" if payload.get("open_gates", {}).get("operator_final_trigger_received") else "false")
PY
}

require_only_operator_trigger_blocking() {
  local status_json="$1"
  python3 - "${status_json}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
blocking = payload.get("blocking_reasons", [])
allowed = ["operator_final_trigger_received:not_set"]
if payload.get("authorized") or blocking == allowed:
    raise SystemExit(0)
print("Refusing live run; authorization blockers are not limited to operator_final_trigger_received:", file=sys.stderr)
for reason in blocking:
    print(f"  - {reason}", file=sys.stderr)
raise SystemExit(42)
PY
}

require_authorized_after_trigger() {
  local status_json="$1"
  python3 - "${status_json}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("authorized"):
    raise SystemExit(0)
print("Refusing live run; authorization is still false after final trigger:", file=sys.stderr)
for reason in payload.get("blocking_reasons", []):
    print(f"  - {reason}", file=sys.stderr)
raise SystemExit(43)
PY
}

mode="${1:-run}"
case "${mode}" in
  -h|--help|help)
    usage
    exit 0
    ;;
  status)
    run_authorization_status
    ;;
  dry-run)
    source_ros
    ros2 run ur10e_example_controllers "${RUNNER}"
    ;;
  run)
    if [[ "${STEP5B_FINAL_TRIGGER:-}" != "${FINAL_TRIGGER_TEXT}" ]]; then
      usage
      echo
      echo "Refusing live Step5b run: STEP5B_FINAL_TRIGGER is not exact."
      exit 40
    fi

    run_dir="$(make_run_dir)"
    mkdir -p "${run_dir}"
    before_status="${run_dir}/authorization_before_trigger.json"
    after_status="${run_dir}/authorization_after_trigger.json"
    previous_trigger="$(read_operator_trigger)"

    restore_trigger() {
      write_operator_trigger "${previous_trigger}"
    }
    trap restore_trigger EXIT

    set +e
    run_authorization_status --json >"${before_status}"
    auth_rc=$?
    set -e
    if [[ ${auth_rc} -ne 0 && ${auth_rc} -ne 2 ]]; then
      echo "Authorization status command failed unexpectedly: rc=${auth_rc}" >&2
      exit "${auth_rc}"
    fi
    require_only_operator_trigger_blocking "${before_status}"

    write_operator_trigger true
    run_authorization_status --json >"${after_status}"
    require_authorized_after_trigger "${after_status}"

    source_ros
    ros2 run ur10e_example_controllers "${RUNNER}" \
      --execute-live-contact \
      --run-dir "${run_dir}"
    echo
    echo "Step5b ROS2 headless live artifacts: ${run_dir}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
