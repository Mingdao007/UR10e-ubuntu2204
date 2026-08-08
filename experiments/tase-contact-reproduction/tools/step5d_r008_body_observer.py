#!/usr/bin/env python3
"""CLI for r008 body observer (diagnose-only; run-dir preferred while host owns RTDE)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="step5d_r008_body_observer",
        description=(
            "Observe body truth vs host claims for a live run-dir. "
            "Writes r008-body-observer.jsonl. Never kills the host."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    watch = sub.add_parser("watch", help="Poll run-dir until interrupted")
    watch.add_argument("--run-dir", type=Path, required=True)
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--jsonl", action="store_true", help="Print events to stdout")
    watch.add_argument(
        "--also-dashboard",
        metavar="HOST",
        default=None,
        help="Optional dashboard host for PLAYING vs host-dead (no RTDE)",
    )
    watch.add_argument("--count", type=int, default=0, help="Stop after N ticks (0=forever)")
    watch.add_argument(
        "--planned-search-s",
        type=float,
        default=None,
        help="Override planner search estimate (default 13.22)",
    )
    watch.add_argument(
        "--canary-abort-on-stall",
        action="store_true",
        help=(
            "Canary mode: delegate to canary body gate and SIGTERM supervisor on "
            "sustained contacted_stalled (formal campaigns must omit)"
        ),
    )
    watch.add_argument(
        "--sustained-s",
        type=float,
        default=None,
        help="Sustained stall threshold for --canary-abort-on-stall (default 8s)",
    )

    report = sub.add_parser(
        "search-report", help="Summarize contact_search_s vs planner from phase-timings"
    )
    report.add_argument("--run-dir", type=Path, required=True)

    return p


def main(argv: list[str] | None = None) -> int:
    tools_root = Path(__file__).resolve().parent
    if str(tools_root) not in sys.path:
        sys.path.insert(0, str(tools_root))

    from step5d_autotune_v4_r008.body_observer import (
        DEFAULT_CANARY_STALL_ABORT_S,
        DEFAULT_PLANNED_SEARCH_S,
        search_duration_report,
        watch_canary_stall_abort,
        watch_run_dir,
        write_canary_stall_abort_artifact,
    )

    args = _build_parser().parse_args(argv)

    if args.cmd == "search-report":
        doc = search_duration_report(args.run_dir)
        print(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False))
        return 0

    if args.cmd == "watch":
        planned = (
            float(args.planned_search_s)
            if args.planned_search_s is not None
            else DEFAULT_PLANNED_SEARCH_S
        )
        sustained_s = (
            float(args.sustained_s)
            if args.sustained_s is not None
            else DEFAULT_CANARY_STALL_ABORT_S
        )
        if args.canary_abort_on_stall:
            from step5d_r008_canary_body_gate import _revoke_supervisor

            run_dir = args.run_dir.resolve()
            try:
                for events, abort in watch_canary_stall_abort(
                    run_dir,
                    canary_mode=True,
                    sustained_s=sustained_s,
                    interval_s=args.interval,
                    robot_host=args.also_dashboard,
                    write_jsonl=True,
                    count=int(args.count),
                ):
                    if args.jsonl:
                        for ev in events:
                            print(json.dumps(ev, sort_keys=True, allow_nan=False), flush=True)
                    elif events:
                        for ev in events:
                            print(
                                f"{ev.get('event')} verdict={ev.get('verdict')} "
                                f"claim={ev.get('host_claim_phase')} "
                                f"tp={ev.get('tp_state')} z={ev.get('tcp_z_m')}",
                                flush=True,
                            )
                    if abort is None:
                        continue
                    path = write_canary_stall_abort_artifact(run_dir, abort)
                    revoke = _revoke_supervisor(run_dir)
                    print(
                        json.dumps(
                            {**abort, "artifact": str(path), "revoke": revoke},
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        flush=True,
                    )
                    return 1
            except KeyboardInterrupt:
                return 0
            return 0

        try:
            for batch in watch_run_dir(
                args.run_dir,
                interval_s=args.interval,
                planned_search_s=planned,
                robot_host=args.also_dashboard,
                write_jsonl=True,
                count=int(args.count),
            ):
                if args.jsonl:
                    for ev in batch:
                        print(json.dumps(ev, sort_keys=True, allow_nan=False), flush=True)
                elif batch:
                    for ev in batch:
                        print(
                            f"{ev.get('event')} verdict={ev.get('verdict')} "
                            f"claim={ev.get('host_claim_phase')} "
                            f"tp={ev.get('tp_state')} z={ev.get('tcp_z_m')}",
                            flush=True,
                        )
        except KeyboardInterrupt:
            return 0
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
