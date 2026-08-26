#!/usr/bin/env bash
set -euo pipefail

# This is the user-facing entrypoint in the main workspace.  The V4 R013
# implementation itself remains in its isolated worktree so the current V5
# files are not mixed into the demo.
DEFAULT_R013_EXPERIMENT_ROOT="/home/andy/.codex-worktrees/step5d-r013-offline-20260813/experiments/tase-contact-reproduction"
R013_EXPERIMENT_ROOT="${R013_DEMO_EXPERIMENT_ROOT:-${DEFAULT_R013_EXPERIMENT_ROOT}}"
DEMO_PY="${R013_EXPERIMENT_ROOT}/tools/run_step5d_autotune_v4_r013_demo.py"

if [[ ! -f "${DEMO_PY}" ]]; then
  echo "refusing: V4 R013 demo script was not found:" >&2
  echo "  ${DEMO_PY}" >&2
  echo "set R013_DEMO_EXPERIMENT_ROOT to the V4 R013 experiment root" >&2
  exit 66
fi

if [[ $# -eq 0 ]]; then
  echo "usage:" >&2
  echo "  $0 profile --feedforward on|off" >&2
  echo "  $0 home --feedforward on|off --run-dir <fresh-run> --launch-profile <profile.json>" >&2
  echo "  $0 start --feedforward on|off --run-dir <fresh-run> --launch-profile <profile.json>" >&2
  exit 64
fi

R013_WORKTREE_ROOT="$(cd "${R013_EXPERIMENT_ROOT}/../.." && pwd)"
DEFAULT_R013_PYTHON="/home/andy/.local/share/step5d-autotune-v3/runtimes/7d2349bf202911570b0cb36e7f849112089fe7710e61c2b9018f7344a16019e1/optimizer/bin/python3"
if [[ -n "${R013_DEMO_PYTHON:-}" ]]; then
  R013_PYTHON="${R013_DEMO_PYTHON}"
elif [[ -x "${DEFAULT_R013_PYTHON}" ]]; then
  R013_PYTHON="${DEFAULT_R013_PYTHON}"
else
  R013_PYTHON="$(command -v python3)"
fi

R013_PYTHONPATH="${R013_EXPERIMENT_ROOT}/tools:${R013_WORKTREE_ROOT}/src/ur10e_experiment_runtime:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages"
if [[ -n "${PYTHONPATH:-}" ]]; then
  R013_PYTHONPATH="${R013_PYTHONPATH}:${PYTHONPATH}"
fi

exec env PYTHONPATH="${R013_PYTHONPATH}" "${R013_PYTHON}" "${DEMO_PY}" "$@"
