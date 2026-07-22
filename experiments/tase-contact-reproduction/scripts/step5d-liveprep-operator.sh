#!/usr/bin/env bash
set -euo pipefail

echo "refusing: duplicate Step5d liveprep entrypoint is retired; use step5d-autotune-v3.sh status --json or step5d-autotune-v3.sh bridge" >&2
exit 64
