#!/usr/bin/env python3
"""Consume a persisted R012 snapshot for an offline dry-run or gated host run."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
R012_R008_WT = Path("/home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/tase-contact-reproduction")
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
# Current R012/R006/R004 modules are the owner.  The sibling is a fallback for
# mature assets that are not copied into this worktree; it must not shadow the
# current readable-identity and guard adapters.
for path in (
    ROOT.parents[1] / "src/ur10e_experiment_runtime",
    R012_R008_WT / "tools",
):
    if str(path) not in sys.path:
        sys.path.append(str(path))

from step5d_autotune_v4_r012.live_host import (  # noqa: E402
    AutotuneRunMode,
    R012HostError,
    build_host_stack,
    dry_run_dispatch_plan,
    require_r012_run_mode,
)
from step5d_autotune_v4_r012.live_adapter import R012HostLoop, R012TrialExecutor  # noqa: E402
from step5d_autotune_v4_r012.serial_queue import SerialOneSlotQueue  # noqa: E402
from step5d_autotune_v4_r012.runtime_composition import (  # noqa: E402
    build_readiness_report,
    require_bench_launch_ready,
    verify_cbf_timing_receipt,
    verify_controller_readback,
    verify_local_controller_triplet,
)
from step5d_autotune_v4_r012.snapshot import CampaignMode, LiveSnapshot, load_live_snapshot  # noqa: E402


DEFAULT_RUNS = ROOT / "runs/step5d_autotune_v4_r012"


def _persisted_snapshot(path: Path | None, *, discover: bool = False) -> LiveSnapshot:
    if path is not None:
        return load_live_snapshot(path)
    if discover:
        candidates = sorted(DEFAULT_RUNS.glob("**/r012_live_snapshot.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if candidates:
            return load_live_snapshot(candidates[0])
    raise SystemExit("R012 run requires --snapshot pointing to a persisted prepare artifact")


def run_dry_run(args: argparse.Namespace) -> int:
    snapshot = _persisted_snapshot(args.snapshot, discover=True)
    stack = build_host_stack(campaign_id=snapshot.campaign_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id)
    plan = dry_run_dispatch_plan(stack)
    report = build_readiness_report(snapshot, evidence={
        "production_gp_fit": True,
        "historical_gp_calibration": True,
        "early_end_backtest": True,
        "snapshot_and_focused_tests": True,
        "controller_package_structurally_valid": verify_local_controller_triplet(snapshot),
        "controller_readback_byte_equal": False,
        "runtime_protocol_observed": False,
        "home_stationary_safety_normal": False,
        "rtde_and_kunwei_fresh": False,
        "cbf_timing_physical_guards": bool(args.snapshot) and verify_cbf_timing_receipt(args.snapshot.resolve().parent / "r012_cbf_timing_receipt.json"),
    })
    print(json.dumps({
        "status": "dry_run_ok",
        "snapshot": str(args.snapshot) if args.snapshot else "discovered_persisted_snapshot",
        "campaign_id": snapshot.campaign_id,
        "run_id": snapshot.run_id,
        "attempt_id": snapshot.attempt_id,
        "campaign_mode": snapshot.campaign_mode,
        "r008_b3_motion_tools": str(R012_R008_WT / "tools"),
        "host_stack_schema": stack["schema"],
        "protocol": stack["protocol"],
        "dispatch_plan": plan,
        "readiness": {
            "host_modules_present": report["host_modules_present"],
            "controller_package_structurally_valid": report["gates"]["controller_package_structurally_valid"],
            "controller_delivery_ready": report["controller_delivery_ready"],
            "bench_launch_ready": report["bench_launch_ready"],
            "live_ready": report["live_ready"],
            "bounded_tuning_ready": report["bounded_tuning_ready"],
            "blockers_tail": report["blockers"][-6:],
        },
    }, sort_keys=True, indent=2))
    return 0


def run_live(args: argparse.Namespace) -> int:
    mode = require_r012_run_mode(args.mode, "run_step5d_autotune_v4_r012_live.live")
    snapshot = _persisted_snapshot(args.snapshot)
    snapshot_path = args.snapshot.resolve()
    run_dir = snapshot_path.parent
    campaign_mode = snapshot.campaign_mode if args.campaign_mode is None else CampaignMode.parse(args.campaign_mode).value
    if campaign_mode != snapshot.campaign_mode:
        raise R012HostError("--campaign-mode differs from the persisted R012 snapshot")
    report = build_readiness_report(snapshot, evidence={
        "production_gp_fit": True,
        "historical_gp_calibration": True,
        "early_end_backtest": True,
        "snapshot_and_focused_tests": True,
        "controller_package_structurally_valid": verify_local_controller_triplet(snapshot),
        "controller_readback_byte_equal": bool(args.controller_readback) and verify_controller_readback(snapshot, args.controller_readback),
        "runtime_protocol_observed": False,
        "home_stationary_safety_normal": False,
        "rtde_and_kunwei_fresh": False,
        "cbf_timing_physical_guards": verify_cbf_timing_receipt(run_dir / "r012_cbf_timing_receipt.json"),
    })
    require_bench_launch_ready(report)
    stack = build_host_stack(
        campaign_id=snapshot.campaign_id,
        run_id=snapshot.run_id,
        attempt_id=snapshot.attempt_id,
        use_production_gp=True,
        ledger_path=snapshot_path.parent / "r012_ledger.jsonl",
    )
    factory_spec = str(args.owner_factory).strip()
    if ":" not in factory_spec:
        raise R012HostError("--owner-factory must be an explicit module:callable direct live owner")
    module_name, attribute = factory_spec.split(":", 1)
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise R012HostError(f"R012 direct live owner factory is unavailable: {factory_spec}")
    supplied = factory(
        stack=stack,
        mode=mode,
        run_dir=run_dir,
        controller_host=args.controller_host,
        kunwei_host=args.kunwei_host,
        kunwei_port=args.kunwei_port,
        launch_profile=args.launch_profile,
    )
    if isinstance(supplied, R012TrialExecutor):
        executor = supplied
    elif isinstance(supplied, tuple) and len(supplied) == 2:
        executor = R012TrialExecutor(
            owner=supplied[0],
            runner=supplied[1],
            guard_stack=stack["path_guard_stack"],
        )
    else:
        raise R012HostError("R012 direct live owner must return a guard-bound trial executor")
    if executor.guard_stack is not stack["path_guard_stack"]:
        raise R012HostError("R012 owner factory did not bind the current PATH guard stack")
    queue = SerialOneSlotQueue(
        run_dir / "r012_queue.jsonl",
        route_id=args.route_id or f"r012-route-{snapshot.run_id}",
        session_id=args.resident_session_id or f"r012-session-{snapshot.run_id}",
        session_epoch=args.session_epoch,
    )
    loop = R012HostLoop(
        stack["controller"], stack["path_early_end"], queue,
        executor=executor,
        optimizer=stack["optimizer"],
    )
    result = loop.run_one() if campaign_mode == CampaignMode.ONE_SHOT.value else loop.run_continuous()
    print(json.dumps({
        "status": "bounded_live_complete" if mode is AutotuneRunMode.BOUNDED_LIVE else "formal_release_complete",
        "campaign_mode": campaign_mode,
        "result": result,
        "events": loop.events,
    }, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="build the offline R012 dispatch plan")
    parser.add_argument("--snapshot", type=Path, default=None, help="persisted r012_live_snapshot.json from prepare")
    sub = parser.add_subparsers(dest="command")
    live = sub.add_parser("live", help="run through the internally isolated R008 B3 adapter")
    live.add_argument("--mode", choices=[mode.value for mode in AutotuneRunMode], default=AutotuneRunMode.BOUNDED_LIVE.value)
    live.add_argument("--campaign-mode", choices=[mode.value for mode in CampaignMode], default=None)
    live.add_argument("--owner-factory", required=True, help="module:callable direct live owner returning (writer, runner) or R012TrialExecutor")
    live.add_argument("--controller-host", default="192.168.1.18")
    live.add_argument("--kunwei-host", default="192.168.50.25")
    live.add_argument("--kunwei-port", type=int, default=5152)
    live.add_argument("--route-id")
    live.add_argument("--resident-session-id")
    live.add_argument("--session-epoch", type=int, default=1)
    live.add_argument("--launch-profile", type=Path, default=None)
    live.add_argument("--timing-canary", action="store_true")
    live.add_argument("--controller-readback", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(raw)
    try:
        if args.command == "live":
            return run_live(args)
        return run_dry_run(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"status": "live_failed" if args.command == "live" else "dry_run_failed", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
