#!/usr/bin/env python3
"""Prepare or evaluate exact-5.25.2 URSim evidence without runtime transport."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ur10e_vic.ursim_protocol import dry_run_packet, evaluate_runtime_result  # noqa: E402


DEFAULT_SPEC = ROOT / "config" / "ursim_5_25_2_protocol.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--runtime-result", type=Path)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    if not args.execute:
        print(json.dumps(dry_run_packet(spec, experiment_root=ROOT), indent=2))
        return 0

    if args.runtime_result is None:
        packet = dry_run_packet(spec, experiment_root=ROOT)
        packet["mode"] = "execute_requested"
        packet["availability"] = "unavailable_no_runtime_result_or_adapter"
        packet["blockers"].append("runtime_result_required")
        print(json.dumps(packet, indent=2))
        return 2

    result = json.loads(args.runtime_result.read_text(encoding="utf-8"))
    decision = evaluate_runtime_result(result, spec, experiment_root=ROOT)
    print(
        json.dumps(
            {
                "schema": "ur10e_ursim_5_25_2_runtime_evaluation_v1",
                "accepted": decision.accepted,
                "stage": decision.stage,
                "blockers": list(decision.blockers),
                "protocol_fingerprint_sha256": decision.protocol_fingerprint_sha256,
                "runner_opened_socket": False,
                "runner_started_container": False,
                "runner_pulled_image": False,
            },
            indent=2,
        )
    )
    return 0 if decision.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
