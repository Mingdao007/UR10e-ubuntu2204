#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"

export NO_CONTACT_LIVE_MOTION=true
export KUNWEI_FORCE_GATE_REQUIRED=true
exec "${ROOT}/no_contact_test.sh"
