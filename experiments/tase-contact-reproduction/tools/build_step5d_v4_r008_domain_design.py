#!/usr/bin/env python3
"""Build config/step5d/autotune_v4_r008_domain.json from a sealed greybox artifact."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.design import (  # noqa: E402
    DesignError,
    build_domain_document,
    default_anchor,
    default_box,
    plant_from_receipt,
    scan_point,
)
from step5d_autotune_v4_r008.greybox.receipt import canonical_bytes, sha256_bytes  # noqa: E402
from step5d_autotune_v4_r008.greybox.reconstruct import load_run_traces  # noqa: E402
from step5d_autotune_v4_r008.lattice import R008Point, scrambled_sobol  # noqa: E402


DEFAULT_GREYBOX_DIR = ROOT / "runs" / "step5d_autotune_v4_r008" / "greybox_identification"
DEFAULT_RUN = ROOT / "runs" / "step5d_autotune_v4_r006" / "live_20260802_2258_wire_gate"
DEFAULT_OUT = ROOT / "config" / "step5d" / "autotune_v4_r008_domain.json"


def _latest_greybox(directory: Path) -> Path:
    files = sorted(Path(directory).glob("*.json"), key=lambda path: path.stat().st_mtime)
    if not files:
        raise DesignError(f"no greybox artifacts in {directory}")
    return files[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--greybox", type=Path, default=None)
    parser.add_argument("--greybox-dir", type=Path, default=DEFAULT_GREYBOX_DIR)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--scan-count", type=int, default=64)
    args = parser.parse_args(argv)

    try:
        greybox_path = args.greybox or _latest_greybox(args.greybox_dir)
        plant, document = plant_from_receipt(greybox_path)
        traces = load_run_traces(args.run_root)
        template = traces[0]
        box = default_box()
        anchor = default_anchor()
        probes = list(scrambled_sobol(box, count=int(args.scan_count), seed=8))
        # Always include the design anchor and a few P/D staircase levels.
        for scale in (1.0, 2.0, 4.0, 8.0, 16.0):
            probes.append(
                R008Point(
                    log2_pd=math.log2(anchor.pd_ratio * scale),
                    log2_d=anchor.log2_d,
                    log2_tau=anchor.log2_tau,
                    kf_off=anchor.kf_off,
                    log2_kf=anchor.log2_kf,
                    log2_ko=anchor.log2_ko,
                    log2_kp=anchor.log2_kp,
                )
            )
        scan = [scan_point(point, plant, template) for point in probes]
        body = build_domain_document(
            greybox_document=document,
            greybox_sha256=str(document["artifact_sha256"]),
            plant=plant,
            anchor=anchor,
            box=box,
            scan=scan,
        )
        digest = sha256_bytes(canonical_bytes(body))
        body["artifact_sha256"] = digest
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_bytes(body) + b"\n")
    except (DesignError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output),
                "artifact_sha256": digest,
                "greybox": str(greybox_path),
                "scan_summary": body["scan_summary"],
                "anchor_pd_ratio": body["anchor"]["pd_ratio"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
