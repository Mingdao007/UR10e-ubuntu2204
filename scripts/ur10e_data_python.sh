#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws"
PYTHON="${ROOT}/.conda/ur10e-data/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  cat >&2 <<'EOF'
Missing UR10e data-analysis conda environment.

Create it with:
  /home/andy/miniconda3/bin/conda env create \
    -p /home/andy/ur10e_ros2_ws/.conda/ur10e-data \
    -f /home/andy/ur10e_ros2_ws/environment-ur10e-data.yml
EOF
  exit 2
fi

unset PYTHONPATH
export PYTHONNOUSERSITE=1
export MPLBACKEND=Agg

exec "${PYTHON}" "$@"
