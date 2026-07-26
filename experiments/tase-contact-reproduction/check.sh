#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

TEST_PYTHON="${UR10E_TEST_PYTHON:-${ROOT}/.venv/bin/python}"
if [[ ! -x "${TEST_PYTHON}" ]]; then
  echo "isolated test environment missing: ${TEST_PYTHON}" >&2
  echo "run: cd ${ROOT} && uv sync --frozen --only-group test-hermetic" >&2
  exit 2
fi

"${TEST_PYTHON}" - <<'PY'
from importlib.metadata import version

expected = {"pytest": "8.4.1", "pytest-xdist": "3.8.0"}
actual = {name: version(name) for name in expected}
if actual != expected:
    raise SystemExit(f"isolated test dependency mismatch: expected={expected} actual={actual}")
PY

export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

# ROS Python packages are installed outside the isolated venv on bench hosts.
for ros_python in /opt/ros/*/lib/python3.*/site-packages /opt/ros/*/local/lib/python3.*/dist-packages; do
  if [[ -d "${ros_python}" ]]; then
    export PYTHONPATH="${ros_python}:${PYTHONPATH:-}"
  fi
done

CPU_WORKERS="${UR10E_CPU_WORKERS:-auto}"
if [[ "${CPU_WORKERS}" == "auto" ]]; then
  CPU_WORKERS="$(PYTHONPATH="${ROOT}/tools:${PYTHONPATH:-}" "${TEST_PYTHON}" - <<'PY'
from ur10e_parallel import physical_core_count
print(physical_core_count())
PY
)"
fi

pytest_workers=$((CPU_WORKERS - 2))
(( pytest_workers > 14 )) && pytest_workers=14
(( pytest_workers < 1 )) && pytest_workers=1

execution="dag"
if [[ "${UR10E_PARALLEL:-1}" == "0" ]]; then
  execution="serial"
fi

stamp="$(date +%Y%m%d_%H%M%S)_$$_${execution}"
output_dir="${UR10E_TEST_EVIDENCE_DIR:-${ROOT}/runs/validation/${stamp}}"
args=(
  --root "${ROOT}"
  --python "${TEST_PYTHON}"
  --output-dir "${output_dir}"
  --execution "${execution}"
  --workers "${pytest_workers}"
)
if [[ -n "${UR10E_TEST_BASE_REF:-}" ]]; then
  args+=(--base-ref "${UR10E_TEST_BASE_REF}")
fi
if [[ -n "${UR10E_CHANGED_PATHS_FILE:-}" ]]; then
  args+=(--changed-paths-file "${UR10E_CHANGED_PATHS_FILE}")
fi
if [[ "${UR10E_FULL_SUITE:-0}" == "1" ]]; then
  args+=(--full-suite)
fi
if [[ "${UR10E_TEST_REUSE:-1}" == "0" ]]; then
  args+=(--no-reuse)
fi

exec "${TEST_PYTHON}" tools/run_ur10e_impacted_tests.py "${args[@]}"
