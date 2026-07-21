#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CANONICAL="${SCRIPT_DIR}/step5d-autotune-v3.sh"
MAX_COMPAT_TP_REVISION=10

refuse() {
  echo "refusing: Step5d autotune V1 launcher is retired; use step5d-autotune-v3.sh bridge" >&2
  exit 64
}

if [[ $# -eq 0 || "$1" != "bridge" ]]; then
  refuse
fi

current_revision="$({ python3 - "${ROOT}" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
pointer_path = root / "config/step5d/current.json"
pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
relative = pointer.get("manifest_path")
expected_sha = pointer.get("manifest_sha256")
if not isinstance(relative, str) or not isinstance(expected_sha, str):
    raise SystemExit("current release pointer is incomplete")
manifest_path = Path(relative)
if manifest_path.is_absolute() or ".." in manifest_path.parts:
    raise SystemExit("current release manifest path is unsafe")
encoded = (root / manifest_path).read_bytes()
if hashlib.sha256(encoded).hexdigest() != expected_sha:
    raise SystemExit("current release manifest SHA differs")
manifest = json.loads(encoded)
program_id = (manifest.get("identity") or {}).get("program_id", "")
match = re.fullmatch(r"step5d_strict_rnn_autotune_v3_r(\d{3})", program_id)
if match is None:
    raise SystemExit("current release TP program id differs")
print(int(match.group(1)))
PY
  } 2>/dev/null)" || refuse

if (( current_revision > MAX_COMPAT_TP_REVISION )); then
  echo "refusing: Step5d compatibility adapter cutoff passed at r010; use step5d-autotune-v3.sh bridge" >&2
  exit 64
fi

exec "${CANONICAL}" "$@"
