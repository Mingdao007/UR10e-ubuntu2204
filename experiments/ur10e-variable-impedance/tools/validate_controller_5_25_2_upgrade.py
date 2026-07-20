#!/usr/bin/env python3
"""Validate captured upgrade evidence; this tool has no controller transport."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ur10e_vic.controller_upgrade import (  # noqa: E402
    evaluate_post_upgrade_readback,
    evaluate_upgrade_preflight,
)


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return payload


def _artifact_hashes(record: dict) -> dict[str, str]:
    return {
        str(item["role"]): str(item["sha256"])
        for item in record.get("artifacts", ())
        if isinstance(item, dict) and "role" in item and "sha256" in item
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "post-readback"))
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    evidence = _load(args.evidence)
    if args.mode == "preflight":
        if args.preflight is not None:
            parser.error("--preflight is only valid for post-readback")
        decision = evaluate_upgrade_preflight(evidence)
    else:
        if args.preflight is None:
            parser.error("post-readback requires --preflight")
        preflight = _load(args.preflight)
        decision = evaluate_post_upgrade_readback(
            evidence,
            preflight_artifact_hashes=_artifact_hashes(preflight),
            preflight_identity={
                key: str(preflight.get(key, ""))
                for key in (
                    "controller_serial",
                    "robot_serial",
                    "tcp_payload_binding_sha256",
                )
            },
        )

    payload = {
        "schema": "ur10e_controller_upgrade_gate_result_v1",
        "stage": decision.stage,
        "accepted": decision.accepted,
        "controller_verified": decision.controller_verified,
        "blockers": list(decision.blockers),
        "claim_boundary": (
            "upgrade evidence only; not live/contact/torque authorization"
        ),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if decision.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
