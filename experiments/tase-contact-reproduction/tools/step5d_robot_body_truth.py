#!/usr/bin/env python3
"""CLI for r008 robot body truth (EE/TP vs host/dashboard claims)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="step5d_robot_body_truth",
        description=(
            "Sample robot body truth. Prefer --run-dir while a live host owns RTDE; "
            "--robot-host opens a direct recipe and may fight the writer."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sample = sub.add_parser("sample", help="One snapshot + verdict")
    sample.add_argument("--run-dir", type=Path, default=None)
    sample.add_argument("--robot-host", default=None)
    sample.add_argument("--json", action="store_true")
    sample.add_argument("--no-dashboard", action="store_true")

    watch = sub.add_parser("watch", help="Poll until interrupted")
    watch.add_argument("--run-dir", type=Path, default=None)
    watch.add_argument("--robot-host", default=None)
    watch.add_argument("--interval", type=float, default=0.5)
    watch.add_argument("--jsonl", action="store_true")
    watch.add_argument("--no-dashboard", action="store_true")
    watch.add_argument("--count", type=int, default=0, help="Stop after N samples (0=forever)")

    return parser


def main(argv: list[str] | None = None) -> int:
    # Ensure tools/ is importable when invoked as a script path.
    tools_root = Path(__file__).resolve().parent
    if str(tools_root) not in sys.path:
        sys.path.insert(0, str(tools_root))

    from step5d_autotune_v4_r008.robot_body_truth import (
        classify_run_dir,
        format_human,
        sample_direct,
        verdict,
        watch_direct,
        watch_run_dir,
    )

    args = _build_parser().parse_args(argv)

    if args.cmd == "sample":
        if args.run_dir is not None:
            snap, v, motion = classify_run_dir(args.run_dir)
        elif args.robot_host:
            snap = sample_direct(
                args.robot_host, include_dashboard=not args.no_dashboard
            )
            v = verdict(snap)
            motion = None
        else:
            print("error: need --run-dir or --robot-host", file=sys.stderr)
            return 2
        if args.json:
            payload = snap.as_dict()
            payload["verdict"] = v.value
            if motion is not None:
                payload["motion"] = motion.as_dict()
            print(json.dumps(payload, sort_keys=True, allow_nan=False))
        else:
            print(format_human(snap, v))
        return 0

    if args.cmd == "watch":
        if args.run_dir is not None:
            stream = watch_run_dir(args.run_dir, interval_s=args.interval)
        elif args.robot_host:
            print(
                "WARN: --robot-host watch may fight live writer if host owns RTDE",
                file=sys.stderr,
            )
            stream = watch_direct(
                args.robot_host,
                interval_s=args.interval,
                include_dashboard=not args.no_dashboard,
            )
        else:
            print("error: need --run-dir or --robot-host", file=sys.stderr)
            return 2
        n = 0
        try:
            for snap, v, motion in stream:
                if args.jsonl:
                    payload = snap.as_dict()
                    payload["verdict"] = v.value
                    if motion is not None:
                        payload["motion"] = motion.as_dict()
                    print(json.dumps(payload, sort_keys=True, allow_nan=False), flush=True)
                else:
                    print(format_human(snap, v), flush=True)
                n += 1
                if args.count and n >= args.count:
                    break
        except KeyboardInterrupt:
            return 0
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
