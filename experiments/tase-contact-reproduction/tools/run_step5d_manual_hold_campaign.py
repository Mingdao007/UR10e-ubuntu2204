#!/usr/bin/env python3
"""Offline operator CLI for the isolated one-request Step5d manual-hold core.

This command prepares or inspects durable identities only.  It deliberately has
no controller, bridge, Load, Play, ARM-register, or motion transport.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from step5d_manual_runtime import load_state, prepare_next_intent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("command", choices=("prepare-next", "status"))
    args = parser.parse_args(argv)
    if args.command == "prepare-next":
        intent = prepare_next_intent(
            queue_path=args.queue,
            state_path=args.state,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        result = None if intent is None else intent.document()
    else:
        result = load_state(
            args.state,
            campaign_id=args.campaign_id,
            release_sha=args.release_manifest_sha256,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
