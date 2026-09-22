#!/usr/bin/env bash
# One complete period through the existing sole-writer supervisor.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: figure8.sh [--method METHOD] [--duration full|r013_60|r013_60_rate400] [--run-dir RUN] [--prepared-dir PREPARED_RUN] [--control-cpu N] [--parameter-file FILE] [--resident-parameter-manifest FILE | --resident-candidate-dir DIR] [--resident-attempts N] [--video-policy required|evidence-only]'
  echo 'Adaptive resident mode reads candidate-0001.json before launch, then waits up to 120 s at verified joint Home for each next candidate.'
  echo '       figure8.sh --stop --run-dir RUN'
  echo 'Defaults: TASE_RNN_MATURE, CPU 2, one 60 s R013-compatible figure-eight, then stop/Home.'
  echo 'Use --duration full explicitly for the separate 62.831853 s full-period protocol.'
  echo 'Without --run-dir: automatically capture fresh baseline and fetch installed packages.'
  echo 'An unavailable method is rejected before controller access; no substitution.'
  exit 0
fi
method=TASE_RNN_MATURE
cpu=2
run_dir=''
parameter_file=''
resident_parameter_manifest=''
resident_candidate_dir=''
prepared_dir=''
stop_requested=0
duration='r013_60'
video_policy='evidence-only'
resident_attempts=1
offline_acceptance=0
while (($#)); do
  case "$1" in
    --method) [[ $# -ge 2 ]] || exit 64; method="$2"; shift 2 ;;
    --duration) [[ $# -ge 2 ]] || exit 64; duration="$2"; shift 2 ;;
    --control-cpu) [[ $# -ge 2 ]] || exit 64; cpu="$2"; shift 2 ;;
    --run-dir) [[ $# -ge 2 ]] || exit 64; run_dir="$2"; shift 2 ;;
    --prepared-dir) [[ $# -ge 2 ]] || exit 64; prepared_dir="$2"; shift 2 ;;
    --stop) stop_requested=1; shift ;;
    --parameter-file) [[ $# -ge 2 ]] || exit 64; parameter_file="$2"; shift 2 ;;
    --resident-parameter-manifest) [[ $# -ge 2 ]] || exit 64; resident_parameter_manifest="$2"; shift 2 ;;
    --resident-candidate-dir) [[ $# -ge 2 ]] || exit 64; resident_candidate_dir="$2"; shift 2 ;;
    --video-policy) [[ $# -ge 2 ]] || exit 64; video_policy="$2"; shift 2 ;;
    --resident-attempts) [[ $# -ge 2 ]] || exit 64; resident_attempts="$2"; shift 2 ;;
    --offline-acceptance) offline_acceptance=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 64 ;;
  esac
done
if [[ -n "$resident_candidate_dir" ]]; then
  if [[ -n "$resident_parameter_manifest" || -n "$parameter_file" ]]; then
    echo '--resident-candidate-dir is mutually exclusive with --resident-parameter-manifest and --parameter-file' >&2
    exit 64
  fi
  if [[ "$method" != TASE_RNN_MATURE || ( "$duration" != r013_60 && "$duration" != r013_60_rate400 ) ]]; then
    echo '--resident-candidate-dir requires a 60 s TASE_RNN_MATURE pilot' >&2
    exit 64
  fi
  if (( resident_attempts < 1 )); then
    echo '--resident-candidate-dir requires a positive resident attempt count' >&2
    exit 64
  fi
  if (( offline_acceptance || stop_requested )); then
    echo '--resident-candidate-dir applies only to a supervised resident pilot' >&2
    exit 64
  fi
  env -u VIRTUAL_ENV -u PYTHONHOME PYTHONNOUSERSITE=1 \
    PYTHONPATH="$ROOT/tools:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages" \
    CANDIDATE_DIR="$resident_candidate_dir" DURATION="$duration" ATTEMPTS="$resident_attempts" \
    "$ROOT/.venv-contact-six/bin/python" - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["CANDIDATE_DIR"]).expanduser()
if root.is_symlink():
    raise SystemExit("resident candidate directory cannot be a symlink")
root = root.resolve()
if not root.is_dir():
    raise SystemExit(f"resident candidate directory is invalid: {root}")
duration = os.environ["DURATION"]
attempts = int(os.environ["ATTEMPTS"])
first = root / "candidate-0001.json"
if first.is_symlink() or not first.is_file():
    raise SystemExit(f"initial resident candidate is not a regular file: {first}")
try:
    from tase_contact_provider import load_tase_outer_config

    _config, binding = load_tase_outer_config(first)
except Exception as exc:
    raise SystemExit(f"initial resident candidate is invalid: {type(exc).__name__}: {exc}")
expected_protocol = (
    "figure8_window60_r013_rate400_v1"
    if duration == "r013_60_rate400"
    else "figure8_window60_r013_compat_v1"
)
if binding.get("protocol_id") != expected_protocol or binding.get("duration_token") != duration:
    raise SystemExit(f"initial resident candidate protocol/duration differs: {first}")
for ordinal in range(2, attempts + 1):
    later = root / f"candidate-{ordinal:04d}.json"
    if later.exists() or later.is_symlink():
        raise SystemExit(f"resident candidate {ordinal} must arrive only after the prior seal")
PY
fi
if (( offline_acceptance )); then
  [[ -n "$run_dir" ]] || { echo '--offline-acceptance requires --run-dir' >&2; exit 64; }
  offline_args=(--run-dir "$run_dir" --attempts "$resident_attempts")
  if [[ "$duration" == r013_60_rate400 ]]; then
    offline_args+=(--rate400)
  fi
  exec env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONNOUSERSITE=1 \
    PYTHONPATH="$ROOT/tools:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages" \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
    "$ROOT/.venv-contact-six/bin/python" "$ROOT/tools/figure8_resident_acceptance.py" \
    "${offline_args[@]}"
fi
case "$video_policy" in
  required|evidence-only) ;;
  *) echo "Unsupported video policy: $video_policy" >&2; exit 64 ;;
esac

if (( stop_requested )); then
  [[ -n "$run_dir" ]] || { echo '--stop requires --run-dir' >&2; exit 64; }
  exec "$ROOT/scripts/contact-yield-live.sh" stop --run-dir "$run_dir"
fi
case "$duration" in
  full|full_period|period|r013_60|compat60|r013_compat_60|r013_60_rate400) ;;
  *) echo "Unsupported Figure-eight duration: $duration" >&2; exit 64 ;;
esac
if [[ -z "$parameter_file" && -z "$resident_parameter_manifest" && -z "$resident_candidate_dir" && "$method" == TASE_RNN_MATURE ]]; then
  case "$duration" in
    r013_60|compat60|r013_compat_60) parameter_file="$ROOT/config/tase_figure8_integral_0p1.json" ;;
    r013_60_rate400) parameter_file="$ROOT/config/tase_figure8_integral_0p1_rate400.json" ;;
  esac
fi

write_terminal_no_dispatch_receipt() {
  local phase="$1"
  local returncode="$2"
  RUN_DIR="$run_dir" PHASE="$phase" RETURN_CODE="$returncode" \
    "$ROOT/.venv-contact-six/bin/python" - <<'PY'
import datetime
import json
import os
from pathlib import Path

run_dir = Path(os.environ["RUN_DIR"])
run_dir.mkdir(parents=True, exist_ok=True)
forbidden = {
    "dispatch_receipt.json", "owner.json", "raw_sensor.jsonl",
    "robot_frames.jsonl", "published_packets.jsonl",
    "admission_robot_frames.jsonl", "rejected_robot_frames.jsonl",
    "supervisor-result.json",
}
payload = {
    "schema": "yield-live-entry/terminal-no-dispatch-v1",
    "terminal_no_dispatch": True,
    "attempt_dispatched": False,
    "motion_started": False,
    "dispatch_receipt_present": False,
    "writer_artifacts_absent": not any((run_dir / name).exists() for name in forbidden),
    "phase": os.environ["PHASE"],
    "returncode": int(os.environ["RETURN_CODE"]),
    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "reason": "owner preflight failed before the live writer was opened",
}
(run_dir / "terminal-no-dispatch-receipt.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
}

if [[ -n "$prepared_dir" ]]; then
  [[ -n "$run_dir" ]] || { echo '--prepared-dir requires --run-dir' >&2; exit 64; }
  if [[ ! -f "$run_dir/software_baseline_receipt.json" ]]; then
    env -u VIRTUAL_ENV -u PYTHONHOME PYTHONNOUSERSITE=1 \
      PYTHONPATH="$ROOT/tools:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages" \
      "$ROOT/.venv-contact-six/bin/python" -B "$ROOT/tools/reuse_figure8_preparation.py" \
      --source "$prepared_dir" --destination "$run_dir"
  fi
fi
export TASE_FIGURE8_STARTED_MONOTONIC="$(python3 -c 'import time; print(time.monotonic())')"
if [[ -z "$run_dir" || ! -d "$run_dir" ]]; then
  status_rc=0
  "$ROOT/scripts/contact-six.sh" status || status_rc=$?
  if [[ -z "$run_dir" ]]; then
    run_dir="$ROOT/runs/figure8-$(date -u +%Y%m%dT%H%M%S)-$$"
  fi
  if (( status_rc != 0 )); then
    mkdir -p "$run_dir"
    write_terminal_no_dispatch_receipt contact_six_status "$status_rc"
    exit "$status_rc"
  fi
  prepare_rc=0
  env -u VIRTUAL_ENV -u PYTHONHOME PYTHONNOUSERSITE=1 \
    PYTHONPATH="$ROOT/tools:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages" \
    AMENT_PREFIX_PATH=/opt/ros/humble OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
    "$ROOT/.venv-contact-six/bin/python" -B "$ROOT/tools/prepare_figure8.py" \
    --run-dir "$run_dir" --method "$method" --video-policy "$video_policy" || prepare_rc=$?
  if (( prepare_rc != 0 )); then
    write_terminal_no_dispatch_receipt prepare_figure8 "$prepare_rc"
    exit "$prepare_rc"
  fi
fi
set +e
supervise_args=(supervise --action pilot \
 --method "$method" --duration "$duration" --run-dir "$run_dir" \
 --readback-dir "$run_dir/readback" --control-cpu "$cpu")
supervise_args+=(--video-policy "$video_policy")
supervise_args+=(--resident-attempts "$resident_attempts")
if [[ -n "$resident_parameter_manifest" ]]; then
  supervise_args+=(--resident-parameter-manifest "$resident_parameter_manifest")
fi
if [[ -n "$resident_candidate_dir" ]]; then
  supervise_args+=(--resident-candidate-dir "$resident_candidate_dir")
fi
if [[ -n "$parameter_file" ]]; then
  supervise_args+=(--parameter-file "$parameter_file")
fi
if [[ -n "$prepared_dir" ]]; then
  TASE_CAMPAIGN_REUSE=1 "$ROOT/scripts/contact-yield-live.sh" "${supervise_args[@]}"
else
  "$ROOT/scripts/contact-yield-live.sh" "${supervise_args[@]}"
fi

result=$?
# Keep the previous recoverable run when admission failed before dispatch.
if [[ -f "$run_dir/dispatch_receipt.json" ]]; then
  printf '%s\n' "$run_dir" > "$ROOT/runs/bench-last-run.txt"
fi
exit "$result"
