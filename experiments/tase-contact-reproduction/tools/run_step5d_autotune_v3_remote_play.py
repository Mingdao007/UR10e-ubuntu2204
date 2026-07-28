#!/usr/bin/env python3
"""Trigger one governed Dashboard Play for an active Step5d V3 Remote route."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from step5d_autotune_v3.remote_play import (  # noqa: E402
    RemotePlayError,
    governed_remote_play,
)
from step5d_bridge_status import resolve_status  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--dashboard-port", type=int, default=29999)
    parser.add_argument("--dashboard-timeout-s", type=float, default=3.0)
    parser.add_argument("--observe-timeout-s", type=float, default=3.0)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = governed_remote_play(
            args.experiment_root,
            robot_host=args.robot_host,
            evidence_output=args.evidence_output,
            dashboard_port=args.dashboard_port,
            dashboard_timeout_s=args.dashboard_timeout_s,
            observe_timeout_s=args.observe_timeout_s,
            status_resolver=resolve_status,
        )
    except (OSError, RemotePlayError, ValueError) as exc:
        print(f"Remote Play blocked: {type(exc).__name__}:{exc}", file=sys.stderr)
        return 3
    print(json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
