#!/usr/bin/env bash
set -euo pipefail
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)"
if [[ "${1:-}" == provision || "${1:-}" == status ]]; then
  exec "$EXPERIMENT_ROOT/scripts/contact-six.sh" "$@"
fi
interpreter="$EXPERIMENT_ROOT/.venv-contact-six/bin/python"
if [[ ! -x "$interpreter" ]]; then
  echo "Run $SCRIPT_PATH provision to install the existing locked CPU environment." >&2
  exit 69
fi
exec env -u VIRTUAL_ENV -u PYTHONHOME -u PYTHONPATH PYTHONNOUSERSITE=1 \
  OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 "$interpreter" \
  "$EXPERIMENT_ROOT/tools/run_contact_yield.py" "$@"
