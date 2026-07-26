#!/usr/bin/env python3
"""Codex-side interface for adding five Step5d log2 candidates at a time."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from step5d_autotune_batch_plan import (
    append_batch,
    candidate_from_log2_payload,
    initialize_plan,
    load_plan,
)


def main(args: argparse.Namespace) -> int:
    if args.command == "init":
        plan = initialize_plan(args.plan, campaign_id=args.campaign_id)
    elif args.command == "append":
        candidates = [
            candidate_from_log2_payload(json.loads(item)) for item in args.candidate
        ]
        plan = append_batch(args.plan, candidates=candidates, source=args.source)
    else:
        plan = load_plan(args.plan)
    print(
        json.dumps(
            {
                "ok": True,
                "campaign_id": plan.campaign_id,
                "revision": plan.revision,
                "batch_count": len(plan.batches),
                "candidate_count": len(plan.candidates),
                "closed": plan.closed,
                "plan": str(args.plan.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--plan", type=Path, required=True)
    init.add_argument("--campaign-id", required=True)
    append = subparsers.add_parser("append")
    append.add_argument("--plan", type=Path, required=True)
    append.add_argument("--source", required=True)
    append.add_argument("--candidate", action="append", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--plan", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))
