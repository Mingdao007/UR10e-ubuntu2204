#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"

export STEP4E_VERSION="${STEP4E_VERSION:-v31}"

exec "${SCRIPT_DIR}/step4e-line-v1-operator.sh" "$@"
