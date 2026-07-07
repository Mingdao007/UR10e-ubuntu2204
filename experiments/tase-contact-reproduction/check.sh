#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

python3 tools/validate_tase_protocol_table.py
python3 tools/validate_cross_step_parameter_table.py
python3 -m pytest tests -q -p no:anyio
