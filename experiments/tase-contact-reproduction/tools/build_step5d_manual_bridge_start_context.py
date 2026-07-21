#!/usr/bin/env python3
"""Build one fresh, write-once manual-hold NO_ARM bridge-start context."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from step5d_manual_bridge import ROOT, ManualBridgeError, build_context, write_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--plant-epoch", type=int, required=True)
    parser.add_argument("--launch-profile", type=Path, default=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = build_context(
            args.root,
            plant_epoch=args.plant_epoch,
            launch_profile_path=args.launch_profile,
        )
        write_once(args.output, payload)
    except (OSError, ValueError, ManualBridgeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "ok": True,
        "output": str(args.output.expanduser().absolute()),
        "context_sha256": payload["context_sha256"],
        "program": payload["program"],
        "protocol": payload["protocol"],
        "bridge_authorized": True,
        "arm_authorized": False,
        "motion_authorized": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
