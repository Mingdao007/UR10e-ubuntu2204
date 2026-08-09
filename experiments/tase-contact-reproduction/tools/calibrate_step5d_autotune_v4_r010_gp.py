#!/usr/bin/env python3
"""Build the canonical R010 GP calibration artifact from Phase5 evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from step5d_autotune_v4_r010.calibration_runner import run_cross_validation
from step5d_autotune_v4_r010.gp_calibration import (
    admit_phase5_ledger,
    build_calibration_artifact,
    canonical_bytes,
    sha256_file,
    validate_calibration_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config/step5d/autotune_v4_r010_gp_calibration.json"
KERNEL_PATH = ROOT / "tools/step5d_autotune_v4_r010/kernel.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-label", default="phase5_r006_observations_historical_read_only")
    parser.add_argument("--maxiter", type=int, default=200)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.maxiter < 1:
        raise SystemExit("--maxiter must be positive")
    admission = admit_phase5_ledger(args.ledger)
    results = run_cross_validation(admission.rows, maxiter=args.maxiter)
    artifact = build_calibration_artifact(
        admission,
        results,
        kernel_implementation_sha256=sha256_file(KERNEL_PATH),
        source_path_label=args.source_label,
    )
    validate_calibration_artifact(artifact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # This builder is the explicit persist boundary for the immutable artifact.
    args.output.write_bytes(canonical_bytes(artifact) + b"\n")
    cold = json.loads(args.output.read_text(encoding="utf-8"))
    validate_calibration_artifact(cold)
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output),
                "calibration_sha256": artifact["calibration_sha256"],
                "selected_noise_floor_n2": artifact["noise"]["selected_floor_n2"],
                "admitted_rows": artifact["source"]["admitted_rows"],
                "controller_upload": False,
                "controller_readback": False,
                "live_evidence": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
