#!/usr/bin/env python3
"""JSON-only offline operator entrypoint for TacDiffusion Remote/headless.

This module has no socket, subprocess, Dashboard, bridge, RTDE writer, or
controller implementation.  Queue/campaign operations are local durable JSON
operations; lifecycle evaluation consumes captured evidence only.  The only
campaign executor supplied here is an injected synthetic fixture executor.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from ur10e_vic.tacdiffusion.direct_torque_receiver import build_receiver_source, parse_receiver_source
from ur10e_vic.tacdiffusion.mainline_dataset import validate_mainline_dataset
from ur10e_vic.tacdiffusion.mainline_model import benchmark_runtime, load_mainline_checkpoint
from ur10e_vic.tacdiffusion.promotion import run_offline_shadow_gate, validate_promotion_manifest, write_blocked_promotion_manifest
from ur10e_vic.tacdiffusion.queue import (
    CampaignLifecycle,
    CampaignRunner,
    EpisodeRequest,
    HomeIdentityLedger,
    PersistentRollingQueue,
    QueueDecision,
    SafeRetractPlan,
)
from ur10e_vic.tacdiffusion.raw_artifact import read_raw_frames
from ur10e_vic.tacdiffusion.remote_headless import (
    RemoteLifecycleExecutor,
    RemoteLifecycleMode,
    build_dry_run_command_plan,
    inspect_remote_headless,
    parse_dashboard_response,
)
from ur10e_vic.tacdiffusion.trajectory import episode_plan as build_episode_plan


def _request_payload(request: EpisodeRequest) -> dict[str, object]:
    return asdict(request)


def _report_payload(report: object) -> dict[str, object]:
    payload = asdict(report)
    if isinstance(payload.get("statuses"), tuple):
        payload["statuses"] = dict(payload["statuses"])
    for key, value in tuple(payload.items()):
        if isinstance(value, tuple):
            payload[key] = list(value)
    return payload


def _queue(args: argparse.Namespace) -> PersistentRollingQueue:
    return PersistentRollingQueue(state_path=args.state, campaign_home=args.campaign_home, max_pending=args.max_pending)


def _queue_status(queue: PersistentRollingQueue) -> dict[str, object]:
    return {
        "schema": "ur10e_tacdiffusion_queue/v2",
        "state_path": str(queue.state_path),
        "campaign_home": queue.campaign_home,
        "mode": queue.mode.value,
        "pending_count": queue.pending_count,
        "inflight": None if queue.inflight is None else _request_payload(queue.inflight),
        "completed_count": len(queue.completed),
        "failed_count": len(queue.failed),
        "high_water": queue.high_water,
        "sleep_calls": 0,
        "live_io": False,
    }


def deterministic_campaign_plan(*, episodes: int = 50, seed: int = 0, task_ready_home: str = "task-ready-home") -> tuple[dict[str, object], ...]:
    if episodes != 50:
        raise ValueError("offline mainline campaign plan requires exactly 50 episodes")
    return tuple(
        {
            "episode_id": f"episode-{index:03d}",
            "trajectory_family": family,
            "seed": seed + index,
            "task_ready_home": task_ready_home,
            "dispatch_id": f"dispatch-{index:03d}",
            "speed_scale": profile.speed_scale,
            "normal_force_target_n": profile.normal_force_target_n,
            "preload_n": profile.preload_n,
        }
        for index, family, profile in build_episode_plan(episodes, seed=seed)
    )


def run_offline_campaign(
    state_dir: str | Path,
    *,
    fail_modulo: int = 0,
    terminal: str = "DRAIN",
) -> dict[str, object]:
    """Exercise CampaignRunner with an injected no-I/O episode fixture."""

    if fail_modulo < 0 or terminal not in {"END", "DRAIN"}:
        raise ValueError("offline campaign options are invalid")
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    plan = deterministic_campaign_plan()
    requests = tuple(EpisodeRequest(entry["episode_id"], entry["trajectory_family"], entry["seed"], entry["task_ready_home"], entry["dispatch_id"]) for entry in plan)
    queue = PersistentRollingQueue(state_path=root / "queue.json", campaign_home="task-ready-home", max_pending=100)
    for request in requests:
        queue.append(request)
    ledger = HomeIdentityLedger(state_path=root / "home.json")
    lifecycle = CampaignLifecycle(queue)
    executed: list[str] = []
    failures: list[str] = []
    counters = {"expert_reset_count": 0, "filter_reset_count": 0, "retract_count": 0, "home_ack_count": 0, "home_consume_count": 0}

    def episode_executor(request: EpisodeRequest) -> bool:
        executed.append(request.episode_id)
        index = int(request.episode_id.rsplit("-", 1)[1])
        return fail_modulo == 0 or index % fail_modulo != 0

    def retract_plan(request: EpisodeRequest, identity: str) -> SafeRetractPlan:
        return SafeRetractPlan(identity, request.task_ready_home, (0.0, 0.0, 1.0), (0.0, 0.0, -1.0), 0.01)

    def retract_executor(plan: SafeRetractPlan) -> bool:
        counters["retract_count"] += 1
        return plan.retract_distance_m > 0.0 and plan.retract_phase == "RETRACT_ALONG_CALIBRATED_REACTION_NORMAL"

    def expert_reset() -> None:
        counters["expert_reset_count"] += 1

    def filter_reset() -> None:
        counters["filter_reset_count"] += 1

    runner = CampaignRunner(
        lifecycle,
        ledger,
        episode_runner=episode_executor,
        retract_plan_provider=retract_plan,
        retract_to_home=retract_executor,
        expert_reset=expert_reset,
        filter_reset=filter_reset,
    )
    for _ in requests:
        step = runner.step()
        if step.decision != "WAITING_HOME":
            raise RuntimeError(f"offline campaign did not reach Home ACK boundary: {step.decision}")
        if step.status == "failed":
            failures.append(step.episode_id or "")
        home_identity = runner.pending_home_identity or ""
        counters["home_ack_count"] += 1
        counters["home_consume_count"] += 1
        runner.acknowledge_and_consume_home(home_identity)
    wait_step = runner.step()
    if wait_step.decision != QueueDecision.WAITING_FOR_EPISODE.value:
        raise RuntimeError(f"offline campaign did not remain WAIT_FOREVER: {wait_step.decision}")
    if terminal == "END":
        runner.request_end()
    else:
        runner.request_drain()
    terminal_step = runner.step()
    return {
        "schema": "ur10e_tacdiffusion_offline_campaign/v1",
        "fixture_only": True,
        "episodes_requested": len(requests),
        "episodes_executed": len(executed),
        "episodes_failed": len(failures),
        "failure_episode_ids": failures,
        "failure_continuation": bool(failures) and len(executed) == len(requests),
        "executions": len(executed),
        **counters,
        "wait_forever_decision": wait_step.decision,
        "terminal_request": terminal,
        "terminal_decision": terminal_step.decision,
        "queue_mode": queue.mode.value,
        "home_ack_consume_required": True,
        "sleep_calls": 0,
        "live_io": False,
        "state_dir": str(root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UR10e TacDiffusion offline Remote/headless tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dashboard = subparsers.add_parser("parse-dashboard")
    dashboard.add_argument("capture", type=Path)
    subparsers.add_parser("dry-run-plan")

    plan = subparsers.add_parser("plan-campaign")
    plan.add_argument("--episodes", type=int, default=50)
    plan.add_argument("--seed", type=int, default=0)
    plan.add_argument("--task-ready-home", default="task-ready-home")

    campaign = subparsers.add_parser("run-offline-campaign")
    campaign.add_argument("--state-dir", type=Path, required=True)
    campaign.add_argument("--fail-modulo", type=int, default=0)
    campaign.add_argument("--terminal", choices=("END", "DRAIN"), default="DRAIN")

    queue = subparsers.add_parser("queue")
    queue.add_argument("--state", type=Path, required=True)
    queue.add_argument("--campaign-home", default="task-ready-home")
    queue.add_argument("--max-pending", type=int, default=10000)
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_commands.add_parser("init")
    append = queue_commands.add_parser("append")
    append.add_argument("--episode-id", required=True)
    append.add_argument("--trajectory-family", default="circle")
    append.add_argument("--seed", type=int, required=True)
    append.add_argument("--task-ready-home", default="task-ready-home")
    append.add_argument("--dispatch-id")
    append.add_argument("--priority", action="store_true")
    queue_commands.add_parser("status")
    queue_commands.add_parser("next")
    queue_commands.add_parser("end")
    queue_commands.add_parser("drain")
    reconcile = queue_commands.add_parser("reconcile")
    reconcile.add_argument("--outcome", choices=("completed", "failed"), required=True)
    reconcile.add_argument("--result-identity")
    reconcile.add_argument("--reason", default="operator_reconciled")

    dataset = subparsers.add_parser("validate-dataset")
    dataset.add_argument("--dataset", type=Path, required=True)
    dataset.add_argument("--manifest", type=Path, required=True)

    checkpoint = subparsers.add_parser("benchmark-checkpoint")
    checkpoint.add_argument("--checkpoint", type=Path, required=True)
    checkpoint.add_argument("--iterations", type=int, default=8)
    checkpoint.add_argument("--warmup-iterations", type=int, default=2)
    checkpoint.add_argument("--require-cuda", action="store_true")

    promotion = subparsers.add_parser("promotion")
    promotion_commands = promotion.add_subparsers(dest="promotion_command", required=True)
    validate = promotion_commands.add_parser("validate")
    validate.add_argument("--manifest", type=Path, required=True)
    blocked = promotion_commands.add_parser("write-blocked")
    blocked.add_argument("--manifest", type=Path, required=True)
    blocked.add_argument("--checkpoint-binding", type=Path, required=True)
    blocked.add_argument("--model-checkpoint", type=Path, required=True)
    blocked.add_argument("--raw-replay", type=Path, required=True)
    blocked.add_argument("--reason", default="shadow_artifacts_missing")

    receiver = subparsers.add_parser("receiver-report")
    receiver.add_argument("source", type=Path, nargs="?")

    raw = subparsers.add_parser("raw-summary")
    raw.add_argument("artifact", type=Path)

    shadow = subparsers.add_parser("shadow-gate")
    shadow.add_argument("--raw-artifact", type=Path, required=True)
    shadow.add_argument("--low", type=Path, required=True)
    shadow.add_argument("--high", type=Path, required=True)

    lifecycle = subparsers.add_parser("lifecycle-no-motion")
    lifecycle.add_argument("--capture", type=Path, required=True)
    lifecycle.add_argument("--captured-at-s", type=float)
    lifecycle.add_argument("--now-s", type=float)
    lifecycle.add_argument("--max-age-s", type=float, default=1.0)
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "parse-dashboard":
        evidence = parse_dashboard_response(args.capture.read_text(encoding="utf-8"))
        report = inspect_remote_headless(evidence)
        return {"evidence": asdict(evidence), "statuses": dict(report.statuses), "no_motion": report.no_motion, "next_required": list(report.next_required), "live_io": False}
    if args.command == "dry-run-plan":
        return {"plan": list(build_dry_run_command_plan()), "live_io": False, "sleep_calls": 0}
    if args.command == "plan-campaign":
        plan = deterministic_campaign_plan(episodes=args.episodes, seed=args.seed, task_ready_home=args.task_ready_home)
        return {"schema": "ur10e_tacdiffusion_campaign_plan/v1", "fixture_only": True, "episodes": list(plan), "trajectory_families": sorted({entry["trajectory_family"] for entry in plan}), "sleep_calls": 0, "live_io": False}
    if args.command == "run-offline-campaign":
        return run_offline_campaign(args.state_dir, fail_modulo=args.fail_modulo, terminal=args.terminal)
    if args.command == "queue":
        queue = _queue(args)
        if args.queue_command == "init":
            return _queue_status(queue)
        if args.queue_command == "append":
            request = EpisodeRequest(args.episode_id, args.trajectory_family, args.seed, args.task_ready_home, args.dispatch_id)
            (queue.priority_insert if args.priority else queue.append)(request)
            return {**_queue_status(queue), "appended": _request_payload(request), "priority": args.priority}
        if args.queue_command == "status":
            return _queue_status(queue)
        if args.queue_command == "next":
            read = queue.next()
            return {**_queue_status(queue), "decision": read.decision.value, "item": None if read.item is None else _request_payload(read.item)}
        if args.queue_command == "end":
            queue.request_end()
            return _queue_status(queue)
        if args.queue_command == "drain":
            queue.request_drain()
            return _queue_status(queue)
        if args.queue_command == "reconcile":
            terminal = queue.reconcile_recovered_inflight(outcome=args.outcome, result_identity=args.result_identity, reason=args.reason)
            return {**_queue_status(queue), "reconciled": asdict(terminal)}
        raise ValueError(f"unsupported queue command: {args.queue_command}")
    if args.command == "validate-dataset":
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        validated = validate_mainline_dataset(args.dataset, manifest)
        return {"schema": validated["schema"], "dataset_artifact": validated["dataset_artifact"], "row_count": validated["row_count"], "episode_count": validated["episode_count"], "validated": True, "live_io": False}
    if args.command == "benchmark-checkpoint":
        bundle = load_mainline_checkpoint(args.checkpoint, require_cuda=args.require_cuda)
        benchmark = benchmark_runtime(bundle["model"], iterations=args.iterations, warmup_iterations=args.warmup_iterations, require_cuda=args.require_cuda)
        return {"schema": bundle["schema"], "device": bundle["device"], "checkpoint": str(args.checkpoint), "benchmark": benchmark, "live_io": False}
    if args.command == "promotion":
        if args.promotion_command == "validate":
            manifest = validate_promotion_manifest(args.manifest)
            return {"schema": manifest["schema"], "active_allowed": manifest["active_allowed"], "blockers": manifest["blockers"], "validated": True, "live_io": False}
        if args.promotion_command == "write-blocked":
            manifest = write_blocked_promotion_manifest(args.manifest, checkpoint_binding_path=args.checkpoint_binding, model_checkpoint_path=args.model_checkpoint, raw_replay_artifact_path=args.raw_replay, reason=args.reason)
            return {"schema": manifest["schema"], "active_allowed": manifest["active_allowed"], "blockers": manifest["blockers"], "written": str(args.manifest), "live_io": False}
        raise ValueError(f"unsupported promotion command: {args.promotion_command}")
    if args.command == "receiver-report":
        source = args.source.read_text(encoding="utf-8") if args.source else build_receiver_source()
        contract = parse_receiver_source(source)
        return {"schema": contract.schema, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "physical_io_enabled": contract.physical_io_enabled, "commands": list(contract.commands), "report_only": True, "live_io": False}
    if args.command == "raw-summary":
        frames = read_raw_frames(args.artifact)
        return {"episode_id": frames[0].episode_id if frames else None, "rows": len(frames), "rate_hz": 500, "raw_evidence_retained": True, "live_io": False}
    if args.command == "shadow-gate":
        result = run_offline_shadow_gate(offline_replay_artifact=args.raw_artifact, shadow_artifact_paths=(args.low, args.high))
        return {"active_allowed": result.active_allowed, "blockers": list(result.blockers), "sleep_calls": result.sleep_calls, "live_io": False}
    if args.command == "lifecycle-no-motion":
        evidence = parse_dashboard_response(args.capture.read_text(encoding="utf-8"), captured_at_s=args.captured_at_s, now_s=args.now_s, max_age_s=args.max_age_s)
        report = RemoteLifecycleExecutor(evidence, mode=RemoteLifecycleMode.NO_MOTION).execute()
        return {**_report_payload(report), "captured_evidence_only": True, "live_io": False}
    raise ValueError(f"unsupported command: {args.command}")


def main() -> int:
    try:
        payload = run(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
