#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_PYTHON="${UR10E_TEST_PYTHON:-${ROOT}/.venv/bin/python}"

if [[ ! -x "${TEST_PYTHON}" ]]; then
  echo "isolated test environment missing: ${TEST_PYTHON}" >&2
  echo "run: cd ${ROOT} && uv sync --frozen --only-group test-hermetic" >&2
  exit 2
fi

"${TEST_PYTHON}" - <<'PY'
from importlib.metadata import version

expected = {"pytest": "8.4.1"}
actual = {name: version(name) for name in expected}
if actual != expected:
    raise SystemExit(
        f"isolated test dependency mismatch: expected={expected} actual={actual}"
    )
PY

export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

cd "${ROOT}"
exec "${TEST_PYTHON}" -m pytest -q \
  tests/test_step5d_remote_control_entrypoint.py \
  tests/test_step5d_remote_minimal_core.py \
  tests/test_step5d_remote_minimal_engine.py \
  tests/test_step5d_remote_minimal_live.py \
  tests/test_step5d_remote_minimal_runtime.py \
  tests/test_step5d_paper_outer_loop.py \
  tests/test_step5d_v30_control_contract.py \
  tests/test_tase_protocol_table.py
