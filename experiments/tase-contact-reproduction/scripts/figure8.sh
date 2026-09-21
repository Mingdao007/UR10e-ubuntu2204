#!/usr/bin/env bash
# One complete period through the existing sole-writer supervisor.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: figure8.sh [--method METHOD] [--run-dir PREPARED_RUN] [--control-cpu N] [--parameter-file FILE]'
  echo 'Defaults: TASE_RNN_MATURE, CPU 2, one 62.831853 s figure-eight, then stop/Home.'
  echo 'Without --run-dir: automatically capture fresh baseline and fetch installed packages.'
  echo 'An unavailable method is rejected before controller access; no substitution.'
  exit 0
fi
method=TASE_RNN_MATURE
cpu=2
run_dir=''
parameter_file=''
while (($#)); do
  case "$1" in
    --method) [[ $# -ge 2 ]] || exit 64; method="$2"; shift 2 ;;
    --control-cpu) [[ $# -ge 2 ]] || exit 64; cpu="$2"; shift 2 ;;
    --run-dir) [[ $# -ge 2 ]] || exit 64; run_dir="$2"; shift 2 ;;
    --parameter-file) [[ $# -ge 2 ]] || exit 64; parameter_file="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 64 ;;
  esac
done
if [[ -z "$run_dir" || ! -d "$run_dir" ]]; then
  "$ROOT/scripts/contact-six.sh" status
  if [[ -z "$run_dir" ]]; then
    run_dir="$ROOT/runs/figure8-$(date -u +%Y%m%dT%H%M%S)-$$"
  fi
  env -u VIRTUAL_ENV -u PYTHONHOME PYTHONNOUSERSITE=1 \
    PYTHONPATH="$ROOT/tools:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages" \
    AMENT_PREFIX_PATH=/opt/ros/humble OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
    "$ROOT/.venv-contact-six/bin/python" -B "$ROOT/tools/prepare_figure8.py" \
    --run-dir "$run_dir" --method "$method"
fi
set +e
supervise_args=(supervise --action pilot \
 --method "$method" --duration full --run-dir "$run_dir" \
 --readback-dir "$run_dir/readback" --control-cpu "$cpu")
if [[ -n "$parameter_file" ]]; then
  supervise_args+=(--parameter-file "$parameter_file")
fi
"$ROOT/scripts/contact-yield-live.sh" "${supervise_args[@]}"

result=$?
# Keep the previous recoverable run when admission failed before dispatch.
if [[ -f "$run_dir/dispatch_receipt.json" ]]; then
  printf '%s\n' "$run_dir" > "$ROOT/runs/bench-last-run.txt"
fi
exit "$result"
