#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
CLI="${EXPERIMENT_ROOT}/tools/run_step5d_remote_control.py"

usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3-remote.sh <command> [options]

Remote offline-only CLI entrypoint.

Commands:
  status
  validate-release
  prepare-trial
  replay
  check
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 64
fi

command="$1"
case "${command}" in
  status|validate-release|prepare-trial|replay|check)
    ;;
  *)
    echo "unsupported command: ${command}" >&2
    usage >&2
    exit 64
    ;;
esac

if [[ ! -f "${CLI}" ]]; then
  echo "remote operator CLI missing: ${CLI}" >&2
  exit 66
fi

exec /usr/bin/env python3 "${CLI}" "$@"
