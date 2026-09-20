#!/usr/bin/env bash
# One complete period through the existing sole-writer supervisor.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: figure8.sh [--method METHOD] --run-dir PREPARED_RUN [--control-cpu N]'
  echo 'Defaults: TASE_RNN, CPU 2, one 62.831853 s figure-eight, then stop/Home.'
  echo 'PREPARED_RUN must contain fresh baseline and read-back evidence.'
  echo 'An unavailable method is rejected before controller access; no substitution.'
  exit 0
fi
method=TASE_RNN
cpu=2
run_dir=''
while (($#)); do
  case "$1" in
    --method) [[ $# -ge 2 ]] || exit 64; method="$2"; shift 2 ;;
    --control-cpu) [[ $# -ge 2 ]] || exit 64; cpu="$2"; shift 2 ;;
    --run-dir) [[ $# -ge 2 ]] || exit 64; run_dir="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 64 ;;
  esac
done
if [[ -z "$run_dir" ]]; then
  echo 'No fresh prepared run selected. Figure-eight has not started.' >&2
  echo 'Automatic fresh preparation is not yet connected; use --run-dir PREPARED_RUN.' >&2
  exit 66
fi
exec "$ROOT/scripts/contact-yield-live.sh" supervise --action pilot \
 --method "$method" --duration full --run-dir "$run_dir" \
 --readback-dir "$run_dir/readback" --control-cpu "$cpu"
