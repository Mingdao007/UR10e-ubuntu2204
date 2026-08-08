#!/usr/bin/env python3
"""Build the content-addressed r008 grey-box identification artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.greybox.identify import identify_run  # noqa: E402
from step5d_autotune_v4_r008.greybox.receipt import build_receipt  # noqa: E402
from step5d_autotune_v4_r008.greybox.reconstruct import GreyboxError  # noqa: E402


DEFAULT_RUN = ROOT / "runs" / "step5d_autotune_v4_r006" / "live_20260802_2258_wire_gate"
DEFAULT_OUT = ROOT / "runs" / "step5d_autotune_v4_r008" / "greybox_identification"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--rho-init", type=float, default=0.5)
    args = parser.parse_args(argv)

    try:
        result = identify_run(args.run_root, rho_init=float(args.rho_init))
        receipt = build_receipt(
            result,
            run_root=args.run_root,
            repo_root=args.repo_root,
        )
        path = receipt.write(args.output_dir)
    except GreyboxError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2

    summary = {
        "ok": True,
        "artifact": str(path),
        "artifact_sha256": receipt.digest,
        "stiffness_n_per_m": receipt.document["identified"]["stiffness_n_per_m"],
        "rho": receipt.document["identified"]["rho"],
        "kinematic_c_v_m_s": receipt.document["identified"]["kinematic_c_v_m_s"],
        "gates": receipt.document["gates"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
