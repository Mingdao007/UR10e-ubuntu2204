#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

TEST_PYTHON="${UR10E_TEST_PYTHON:-${ROOT}/.venv/bin/python}"
if [[ ! -x "${TEST_PYTHON}" ]]; then
  echo "isolated test environment missing: ${TEST_PYTHON}" >&2
  echo "run: uv venv --system-site-packages --python python3 ${ROOT}/.venv && uv pip install --python ${ROOT}/.venv/bin/python -r ${ROOT}/requirements-test.txt" >&2
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

CPU_WORKERS="${UR10E_CPU_WORKERS:-auto}"
if [[ "${CPU_WORKERS}" == "auto" ]]; then
  CPU_WORKERS="$(PYTHONPATH="${ROOT}/tools:${PYTHONPATH:-}" "${TEST_PYTHON}" - <<'PY'
from ur10e_parallel import physical_core_count
print(physical_core_count())
PY
)"
fi

run_pytest() {
  if [[ "${UR10E_PARALLEL:-1}" == "0" ]]; then
    "${TEST_PYTHON}" -m pytest tests -q
  else
    PYTEST_XDIST_AUTO_NUM_WORKERS="${CPU_WORKERS}" \
      "${TEST_PYTHON}" -m pytest tests -q -p xdist.plugin -n auto --dist worksteal
  fi
}

if [[ "${UR10E_PARALLEL:-1}" == "0" ]]; then
  failed=0
  "${TEST_PYTHON}" tools/validate_tase_protocol_table.py || failed=1
  "${TEST_PYTHON}" tools/validate_cross_step_parameter_table.py || failed=1
  run_pytest || failed=1
  exit "${failed}"
else
  pids=()
  "${TEST_PYTHON}" tools/validate_tase_protocol_table.py &
  pids+=("$!")
  "${TEST_PYTHON}" tools/validate_cross_step_parameter_table.py &
  pids+=("$!")
  run_pytest &
  pids+=("$!")
  failed=0
  for pid in "${pids[@]}"; do
    if ! wait "${pid}"; then
      failed=1
    fi
  done
  exit "${failed}"
fi
