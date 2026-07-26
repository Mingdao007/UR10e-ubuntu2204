#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"${PYTHON_BIN}" -m ur10e_vic.cli validate-lock --lock config/dbil_upstream_lock.json >/dev/null
"${PYTHON_BIN}" -m unittest discover -s tests -p 'test_*.py' -v
