#!/usr/bin/env bash
# Return through the existing relief-then-Home owner, with fresh read-back.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: home.sh [--source-run PATH] [--check]'
  echo 'Default source: the most recent run selected by the bench operator.'
  echo 'Uses the existing observed relief/Home sequence; --check never moves.'
  exit 0
fi
source_run=''
execute=(--execute)
while (($#)); do
  case "$1" in
    --source-run) [[ $# -ge 2 ]] || exit 64; source_run="$2"; shift 2 ;;
    --check) execute=(); shift ;;
    *) echo "Unknown option: $1" >&2; exit 64 ;;
  esac
done
if [[ -z "$source_run" ]]; then
  pointer="$ROOT/runs/bench-last-run.txt"
  # Compatibility with the current explicitly selected bench run. Do not
  # guess a source by scanning unrelated historical experiments.
  [[ -f "$pointer" ]] || pointer=/tmp/yield-current-contact-run.txt
  [[ -f "$pointer" ]] || { echo 'No selected run. Use --source-run PATH.' >&2; exit 66; }
  source_run="$(cat -- "$pointer")"
fi
[[ -f "$source_run/dispatch_receipt.json" && -f "$source_run/software_baseline_receipt.json" ]] || {
  echo 'Selected run lacks stop/baseline evidence; Home was not started.' >&2; exit 66;
}
"$ROOT/scripts/contact-six.sh" status
out="$ROOT/runs/home-$(date -u +%Y%m%dT%H%M%S)-$$"
exec env -u VIRTUAL_ENV -u PYTHONHOME PYTHONNOUSERSITE=1 \
 PYTHONPATH="$ROOT/tools:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages" \
 AMENT_PREFIX_PATH=/opt/ros/humble OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
 "$ROOT/.venv-contact-six/bin/python" -B "$ROOT/tools/run_contact_recovery.py" \
 --source-run "$source_run" --output "$out" "${execute[@]}"
