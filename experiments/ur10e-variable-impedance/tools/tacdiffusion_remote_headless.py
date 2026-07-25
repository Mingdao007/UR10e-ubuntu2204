#!/usr/bin/env python3
"""Offline operator entrypoint for TacDiffusion Remote/headless preparation.

This command intentionally parses captures and produces dry-run plans only.
It never opens a network connection, starts a process, sends URScript, or waits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ur10e_vic.tacdiffusion.direct_torque_receiver import parse_receiver_source
from ur10e_vic.tacdiffusion.remote_headless import build_dry_run_command_plan, parse_dashboard_response
from ur10e_vic.tacdiffusion.raw_artifact import read_raw_frames
from ur10e_vic.tacdiffusion.promotion import run_offline_shadow_gate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UR10e TacDiffusion offline Remote/headless tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    dashboard = subparsers.add_parser("parse-dashboard")
    dashboard.add_argument("capture", type=Path)
    subparsers.add_parser("dry-run-plan")
    receiver = subparsers.add_parser("parse-receiver")
    receiver.add_argument("source", type=Path, nargs="?")
    raw = subparsers.add_parser("raw-summary")
    raw.add_argument("artifact", type=Path)
    shadow = subparsers.add_parser("shadow-gate")
    shadow.add_argument("--raw-artifact", type=Path, required=True)
    shadow.add_argument("--low", type=Path, required=True)
    shadow.add_argument("--high", type=Path, required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "parse-dashboard":
        report = parse_dashboard_response(args.capture.read_text(encoding="utf-8"))
        return {"statuses": dict(report.statuses), "no_motion": report.no_motion}
    if args.command == "dry-run-plan":
        return {"plan": list(build_dry_run_command_plan()), "live_io": False}
    if args.command == "parse-receiver":
        source = args.source.read_text(encoding="utf-8") if args.source else None
        contract = parse_receiver_source(source) if source is not None else parse_receiver_source()
        return {"schema": contract.schema, "physical_io_enabled": contract.physical_io_enabled, "commands": contract.commands}
    if args.command == "raw-summary":
        frames = read_raw_frames(args.artifact)
        return {"episode_id": frames[0].episode_id if frames else None, "rows": len(frames), "rate_hz": 500, "raw_evidence_retained": True}
    result = run_offline_shadow_gate(offline_replay_artifact=args.raw_artifact, shadow_artifact_paths=(args.low, args.high))
    return {"active_allowed": result.active_allowed, "blockers": result.blockers, "sleep_calls": result.sleep_calls}


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), indent=2, sort_keys=True))
