#!/usr/bin/env bash
# Canonical launcher for ANY contact-experiment Python (tools, probes, samples).
# System python3 only runs the stdlib resolver; experiment code always runs on
# the .venv-contact-six interpreter with the ROS Humble paths (pinocchio).
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
RESOLVER="$ROOT/tools/experiment_runtime.py"
case "${1:-}" in
  -h|--help|'')
    cat <<'USAGE'
Usage: contact-python.sh --check | --resolve | SCRIPT.py [ARGS] | -m MODULE [ARGS] | -c CODE
  --check    No-device import smoke: interpreter + ROS paths + pinocchio.
  --resolve  Print the interpreter that will be used.
A wrong interpreter or missing pinocchio exits 69 before the target starts.
Override the interpreter with TASE_CONTACT_PYTHON; never pip install globally.
USAGE
    [[ -n "${1:-}" ]] && exit 0 || exit 64 ;;
  --check) exec /usr/bin/python3 -I -B "$RESOLVER" launch -- "$RESOLVER" check ;;
  --resolve) exec /usr/bin/python3 -I -B "$RESOLVER" resolve ;;
esac
exec /usr/bin/python3 -I -B "$RESOLVER" launch -- "$@"
