#!/usr/bin/env python3
"""Public live entry: status, qualify, pilot, stop over the mature writer.

Lifecycle is open -> sensor/output readiness -> resident identity -> ARM ->
execute -> stop/Home.  Native seeds are not physical qualification.  This
process does not Load, Play, or rewrite payload/TCP.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from contact_yield_live_contract import (
    CONTACT_PROGRAM,
    HOME_PROGRAM,
    READABLE_RUNTIME_IDENTITY,
    RUNTIME_PROTOCOL,
    load_identity_contract,
)
from contact_yield_live_path import parse_live_duration
from contact_yield_live_writer import (
    YieldLiveWriterError,
    build_native_yield_owner,
    load_run_dir_receipts,
    native_attempt,
    stop_and_confirm,
)
from contact_yield_resident_session import (
    ResidentSessionError,
    finish_session,
    prepare_session,
    run_attempt,
)
from contact_yield_method_registry import (
    MethodUnavailableError,
    load_live_entry_config,
    load_method_records,
    native_status_payload,
    resolve_method,
)
from step5d_autotune_v4_r004_live_writer import LiveWriterError
from step5d_autotune_v4_r005.live_adapter import R005_LIVE_ACK
from step5d_autotune_v4_r006.live_adapter import R006LiveAdapterError
from tase_r013_timing_ledger import lifecycle_events_from_writer


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REMAINING_AFTER_ENTRY = (
    "physical contact qualification of SFC/DSFC/MSFC",
    "full-chain 500 Hz formal timing on this route",
    "current_stage promotion after main validates the actual route",
)


class YieldLiveError(RuntimeError):
    """Yield live entry failed closed."""


def _blocked_home_recovery(*, run_dir: Path, reason: str) -> dict[str, Any]:
    """Describe a Home attempt that could not be safely dispatched.

    A fault remains a failed attempt even when the recovery owner is blocked.
    Keeping this shape in the live-entry receipt makes the old revoke-only
    outcome impossible to mistake for an automatic Home result.
    """
    return {
        "success": False,
        "motion": False,
        "state": "BLOCKED",
        "source_attempt": str(Path(run_dir)),
        "trial_stays_failed": True,
        "recovery_policy": "AUTO_HOME_WHEN_COMMANDABLE",
        "home_required": True,
        "recovery_owner_invoked": True,
        "home_commandability_checked": False,
        "home_attempted": False,
        "home_commandable": False,
        "home_motion_dispatched": False,
        "home_blocked": True,
        "home_blocked_reason": str(reason),
    }


def automatic_home_after_fault(
    *, run_dir: Path, controller_host: str | None, video_url: str,
    video_policy: str = "required",
) -> dict[str, Any]:
    """Use the existing monitored recovery owner after a physical fault.

    The in-TP bounded Home is attempted first by the generated contact
    package. This host-side route is the second line for a protective stop,
    an RTDE/TP fault, or a failed STOP acknowledgement. It is deliberately
    one-shot: a recovered Home never retries the failed task.
    """
    if not controller_host:
        return _blocked_home_recovery(
            run_dir=run_dir,
            reason="controller host is unavailable for monitored Home recovery",
        )
    try:
        from run_contact_recovery import recover_failed_contact_run
    except BaseException as exc:
        return _blocked_home_recovery(
            run_dir=run_dir,
            reason=f"Home recovery owner import failed: {type(exc).__name__}: {exc}",
        )
    try:
        if video_policy == "required":
            result = recover_failed_contact_run(Path(run_dir), controller_host, video_url)
        else:
            result = recover_failed_contact_run(
                Path(run_dir), controller_host, video_url, video_policy=video_policy
            )
    except BaseException as exc:
        # The normal owner already escalates its own failures.  Keep one
        # direct, monitored fallback here as well so an unexpected exception
        # in the dispatcher cannot silently become a revoke-only outcome while
        # the controller is still commandable.
        try:
            from run_contact_recovery import (
                PACKAGE_DIR,
                _emergency_home_when_commandable,
            )

            import inspect
            fallback_kwargs = {
                "video_url": video_url,
                "video_policy": video_policy,
            }
            if "video_url" not in inspect.signature(_emergency_home_when_commandable).parameters:
                fallback_kwargs = {}
            fallback = _emergency_home_when_commandable(
                Path(run_dir),
                Path(run_dir).with_name(Path(run_dir).name + "-home-fallback"),
                controller_host,
                PACKAGE_DIR,
                reason=exc,
                **fallback_kwargs,
            )
            if isinstance(fallback, dict):
                fallback.setdefault(
                    "primary_recovery_error",
                    f"{type(exc).__name__}: {exc}",
                )
                return fallback
            raise TypeError(
                f"direct Home fallback returned {type(fallback).__name__}"
            )
        except BaseException as fallback_exc:
            return _blocked_home_recovery(
                run_dir=run_dir,
                reason=(
                    "Home recovery dispatch and direct monitored fallback failed: "
                    f"primary={type(exc).__name__}: {exc}; "
                    f"fallback={type(fallback_exc).__name__}: {fallback_exc}"
                ),
            )
    if not isinstance(result, dict):
        return _blocked_home_recovery(
            run_dir=run_dir,
            reason=f"Home recovery owner returned {type(result).__name__}, not a receipt",
        )
    return result


def remaining_machine_gates() -> list[str]:
    return list(REMAINING_AFTER_ENTRY)


def status_payload() -> dict[str, Any]:
    config = load_live_entry_config()
    contract = load_identity_contract()
    records = load_method_records(config)
    return {
        "schema": "yield-live-entry-status-v1",
        "command": "status",
        "program": CONTACT_PROGRAM,
        "home_program": HOME_PROGRAM,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "readable_runtime_identity": list(READABLE_RUNTIME_IDENTITY),
        "package_triplet": dict(contract.triplet),
        "task": dict(config["task"]),
        "durations": dict(config["durations"]),
        "guards": dict(config["guards"]),
        "user_standing_live_authority": config["user_standing_live_authority"],
        "live_discontinued_reason": config.get("live_discontinued_reason"),
        "machine_evidence_fresh": False,
        "physical_qualification": False,
        "current_stage_untouched": True,
        "native_parameters_are_seeds": True,
        "remaining_machine_gates": remaining_machine_gates(),
        "methods": native_status_payload(records),
        "endpoints_opened": False,
        "claim_scope": config["claim_scope"],
    }


def _print_json(payload: Mapping[str, Any]) -> None:
    json.dump(dict(payload), sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Software/registry/package identity. No devices.")
    qualify = sub.add_parser("qualify", help="Open, ARM, qualification attempt, stop/Home")
    qualify.add_argument("--method", default="SFC")
    qualify.add_argument("--run-dir", type=Path, required=True)
    qualify.add_argument("--qp-library", type=Path)
    qualify.add_argument("--parameter-file", type=Path,
                         help="validated TASE outer-loop parameter snapshot")
    qualify.add_argument("--controller-host")
    qualify.add_argument("--kunwei-host")
    qualify.add_argument("--kunwei-port", type=int, default=5152)
    qualify.add_argument("--route-id", default="r006-yield-live")
    qualify.add_argument("--attempt-id", default="r006-yield-live-qualify")
    qualify.add_argument("--authority-root", type=Path)
    qualify.add_argument("--control-cpu", type=int)
    qualify.add_argument("--video-url", default="rtsp://127.0.0.1:8554/arm")
    pilot = sub.add_parser("pilot", help="Open, ARM, PATH 2s/10s/r013_60/full, stop/Home")
    pilot.add_argument("--method", required=True)
    pilot.add_argument("--duration", required=True)
    pilot.add_argument("--run-dir", type=Path, required=True)
    pilot.add_argument("--qp-library", type=Path)
    pilot.add_argument("--parameter-file", type=Path,
                       help="validated TASE outer-loop parameter snapshot")
    pilot.add_argument("--controller-host")
    pilot.add_argument("--kunwei-host")
    pilot.add_argument("--kunwei-port", type=int, default=5152)
    pilot.add_argument("--route-id", default="r006-yield-live")
    pilot.add_argument("--attempt-id", default="r006-yield-live-pilot")
    pilot.add_argument("--authority-root", type=Path)
    pilot.add_argument("--control-cpu", type=int)
    pilot.add_argument("--video-url", default="rtsp://127.0.0.1:8554/arm")
    stop = sub.add_parser("stop", help="Stop the in-process mature writer")
    stop.add_argument("--run-dir", type=Path)
    return parser.parse_args(argv)


def _process_start(pid):
    # Linux proc stat field 22, allowing spaces/parentheses in comm.
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text[text.rfind(")") + 2:].split()[19])


def request_process_stop(run_dir):
    import os, signal, time
    if run_dir is None:
        raise YieldLiveError("stop requires --run-dir")
    path = Path(run_dir).resolve()
    owner_path = path / "owner.json"
    if owner_path.is_symlink():
        raise YieldLiveError("owner receipt cannot be a symlink")
    owner = json.loads(owner_path.read_text())
    pid = int(owner["pid"])
    if (owner.get("active") is not True or owner.get("entry") != str(Path(__file__).resolve())
        or owner.get("run_dir") != str(path) or pid == os.getpid()
        or _process_start(pid) != owner["process_start"]):
        raise YieldLiveError("live owner process identity differs")
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    allowed_entries = (Path(__file__).resolve(), Path(__file__).resolve().with_name('contact_yield_supervisor.py'))
    if not any(str(entry).encode() in argv for entry in allowed_entries):
        raise YieldLiveError("live owner command differs")
    os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + 3.
    while time.monotonic() < deadline:
        receipt_path = path / "dispatch_receipt.json"
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text())
            except json.JSONDecodeError:
                time.sleep(.05)
                continue
            if receipt.get("attempt_id") != owner["attempt_id"]:
                raise YieldLiveError("stop receipt attempt differs")
            return {"schema": "yield-live-entry-stop-v1", **receipt["stop"],
                    "errors": receipt.get("cleanup_errors", [])}
        time.sleep(.05)
    return {"stop_requested": True, "stopped": False,
            "reason": "owner stop receipt pending; do not infer physical stop"}


def run_live(
    args: argparse.Namespace,
    *,
    controller_transport: Any | None = None,
    kunwei_transport: Any | None = None,
    wall_clock=None,
    mono_clock=None,
    sleep=None,
    now_s: float | None = None,
    owner_holder: list[Any] | None = None,
    observer_guard=None,
    defer_recovery: bool = False,
    attempt_count: int = 1,
    parameter_bindings: list[Mapping[str, Any]] | None = None,
    parameter_files: list[Path] | None = None,
    refresh_readback=None,
    dashboard_stop_and_verify=None,
    deferred_seals=None,
) -> dict[str, Any]:
    if args.command not in {"qualify", "pilot"}:
        raise YieldLiveError(f"unknown live command {args.command!r}")
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count <= 0:
        raise YieldLiveError("resident attempt count must be a positive integer")
    if parameter_bindings is not None and len(parameter_bindings) != attempt_count:
        raise YieldLiveError("resident parameter binding count differs from attempt count")
    if parameter_files is not None and len(parameter_files) != attempt_count:
        raise YieldLiveError("resident parameter file count differs from attempt count")
    # The live entry owns all artifacts below one canonical run directory.
    # Relative paths otherwise depend on the caller's current directory and
    # can split the attempt receipt from its read-back and recovery evidence.
    args.run_dir = Path(args.run_dir).expanduser().resolve()
    if getattr(args, "authority_root", None) is not None:
        args.authority_root = Path(args.authority_root).expanduser().resolve()
    config = load_live_entry_config()
    if (controller_transport is None or kunwei_transport is None) and not config["user_standing_live_authority"]:
        raise YieldLiveError("further hardware execution was discontinued by the user; no endpoints opened")
    resolve_method(args.method)
    if args.command == "pilot":
        parse_live_duration(args.duration)
    if controller_transport is None and not args.controller_host:
        raise YieldLiveError("explicit controller host or injected endpoint is required")
    if kunwei_transport is None and not args.kunwei_host:
        raise YieldLiveError("explicit Kunwei host or injected endpoint is required")
    if (args.run_dir / "dispatch_receipt.json").exists():
        raise YieldLiveError("run directory already contains an attempt receipt")
    if controller_transport is None and getattr(args, "control_cpu", None) is None:
        raise YieldLiveError("hardware execution requires the selected --control-cpu")
    video_url = getattr(args, "video_url", "rtsp://127.0.0.1:8554/arm")
    contract = load_identity_contract()
    clock = time_clock(now_s)
    prerequisites, home_binding = load_run_dir_receipts(
        args.run_dir,
        contract=contract,
        route_id=args.route_id,
        attempt_id=args.attempt_id,
        now_s=clock,
    )
    mature, runtime, provider, request = build_native_yield_owner(
        method=args.method,
        duration=getattr(args, "duration", None),
        command=args.command,
        prerequisites=prerequisites,
        home_binding=home_binding,
        qp_library=args.qp_library,
        parameter_file=getattr(args, "parameter_file", None),
        authority_root=args.authority_root or (Path(args.run_dir) / "authority"),
        route_id=args.route_id,
        attempt_id=args.attempt_id,
        controller_host=args.controller_host,
        kunwei_host=args.kunwei_host,
        kunwei_port=args.kunwei_port,
        controller_transport=controller_transport,
        kunwei_transport=kunwei_transport,
        wall_clock=wall_clock,
        mono_clock=mono_clock,
        sleep=sleep,
    )
    # All lifecycle marks use the writer's monotonic domain.  The preflight
    # Home check has completed at this boundary; no nominal receipt timestamp
    # is synthesized if construction fails before this point.
    import time as _time
    lifecycle_clock = getattr(mature.writer, "_mono_clock", None)
    if not callable(lifecycle_clock):
        lifecycle_clock = _time.monotonic
    lifecycle_home_check_s = float(lifecycle_clock())
    lifecycle_contact_search_s: float | None = None
    if observer_guard is not None:
        mature.writer._controller_transport = ObservedTransport(
            mature.writer._controller_transport, observer_guard,
            stopping=lambda: mature.writer._stopped,
            clock=mature.writer._mono_clock)
    if owner_holder is not None:
        owner_holder[:] = [mature, runtime]
    from dataclasses import asdict
    receipt: dict[str, Any] = {
        "motion_profile": asdict(mature.injection.motion_profile),
        "schema": "yield-live-entry-dispatch-v1",
        "evidence_scope": ("live" if controller_transport is None else "offline_synthetic_endpoints"),
        "command": args.command,
        "attempt_id": args.attempt_id,
        "method": args.method,
        "program": CONTACT_PROGRAM,
        "home_program": HOME_PROGRAM,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "requested_input_recipe": list(__import__('contact_yield_transport').NATIVE_INPUT_FIELDS),
        "controller_readback_triplet": dict(contract.triplet),
        "readable_runtime_identity": list(READABLE_RUNTIME_IDENTITY),
        "provider": type(provider).__name__,
        "provider_id": id(provider),
        "writer_id": id(mature.writer),
        "command_writer_count": 1,
        "live_path": None if request is None else request.as_dict(),
        "formally_qualified": False,
        "full_cycle_acceptance": False,
        "physical_qualification": False,
        "continuous_contact_path": bool(args.command == "pilot"),
        "rnn_hash_or_profile": provider.solver_profile.as_dict() if args.method == "TASE_RNN_MATURE" else False,
        "tase_parameter_binding": getattr(provider, "parameter_binding", None),
        "prewarmed_before_endpoints": True,
        "provider_prewarm": getattr(provider, "prewarm_record", None),
        "command_timeline": getattr(provider, "command_timeline", []),
        "opened": False,
        "armed": False,
        "executed": False,
        "automatic_home_policy": "AUTO_HOME_WHEN_COMMANDABLE",
        "video_url": video_url,
    }
    owner_path = None
    old_handlers = {}
    if controller_transport is None:
        import os, signal
        owner_path = Path(args.run_dir).resolve() / "owner.json"
        owner = {"pid": os.getpid(), "process_start": _process_start(os.getpid()),
                 "entry": str(Path(__file__).resolve()), "run_dir": str(Path(args.run_dir).resolve()),
                 "attempt_id": args.attempt_id, "active": True}
        with owner_path.open("x") as handle:
            json.dump(owner, handle)
        def interrupted(signum, frame):
            raise YieldLiveError(f"operator process stop signal {signum}")
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, interrupted)
    return _run_live_with_resident_session(
        args=args,
        controller_transport=controller_transport,
        mature=mature,
        runtime=runtime,
        provider=provider,
        prerequisites=prerequisites,
        request=request,
        contract=contract,
        video_url=video_url,
        owner_holder=owner_holder,
        owner_path=owner_path,
        owner=owner if owner_path is not None else None,
        old_handlers=old_handlers,
        receipt=receipt,
        lifecycle_clock=lifecycle_clock,
        parameter_bindings=parameter_bindings,
        parameter_files=parameter_files,
        attempt_count=attempt_count,
        defer_recovery=defer_recovery,
        refresh_readback=refresh_readback,
        dashboard_stop_and_verify=dashboard_stop_and_verify,
        deferred_seals=deferred_seals,
    )


def _run_live_with_resident_session(
    *,
    args: argparse.Namespace,
    controller_transport: Any | None,
    mature: Any,
    runtime: Any,
    provider: Any,
    prerequisites: Any,
    request: Any,
    contract: Any,
    video_url: str,
    owner_holder: list[Any] | None,
    owner_path: Path | None,
    owner: dict[str, Any] | None,
    old_handlers: dict[Any, Any],
    receipt: dict[str, Any],
    lifecycle_clock: Any,
    parameter_bindings: list[Mapping[str, Any]] | None,
    parameter_files: list[Path] | None,
    attempt_count: int,
    defer_recovery: bool,
    refresh_readback: Any | None,
    dashboard_stop_and_verify: Any | None,
    deferred_seals: list | None,
) -> dict[str, Any]:
    from dataclasses import asdict, is_dataclass
    import os
    import signal

    session_started_s = float(lifecycle_clock())
    if controller_transport is None:
        session_started_s = float(os.environ.get('TASE_FIGURE8_STARTED_MONOTONIC', session_started_s))
    session = prepare_session(
        mature=mature,
        runtime=runtime,
        provider=provider,
        prerequisites=prerequisites,
        run_dir=Path(args.run_dir),
        mono_clock=getattr(mature.writer, "_mono_clock", lifecycle_clock),
        wall_clock=getattr(mature.writer, "_wall_clock", time.time),
        sleep=getattr(mature.writer, "_sleep", time.sleep),
        injected_endpoints=(controller_transport is not None),
        refresh_readback=refresh_readback,
        dashboard_stop_and_verify=dashboard_stop_and_verify,
    )
    active_attempt: tuple[int, str] | None = None
    partial_item: dict[str, Any] | None = None
    errors: list[str] = []
    try:
        end_controller = getattr(mature.writer, "_r013_path_early_end_controller", None)
        if end_controller is not None:
            end_controller.writer = mature.writer._controller_transport
        session.prepare_session(live_ack=R005_LIVE_ACK)
        receipt["opened"] = True
        receipt["attempts"] = []
        phase = "qualify" if args.command == "qualify" else "pilot"
        for sequence in range(1, attempt_count + 1):
            if receipt["attempts"]:
                previous = receipt["attempts"][-1].get("timing", {})
                previous["next_started_monotonic_s"] = float(lifecycle_clock())
                previous["turnaround_s"] = (
                    previous["next_started_monotonic_s"] - previous["started_monotonic_s"]
                )
            active_attempt = (sequence, phase)
            binding = None if parameter_bindings is None else parameter_bindings[sequence - 1]
            parameter_file = None if parameter_files is None else parameter_files[sequence - 1]
            item = run_attempt(
                session,
                phase=phase,
                sequence=sequence,
                control_cpu=getattr(args, "control_cpu", None),
                parameter_binding=binding,
                parameter_file=parameter_file,
            )
            item = session.seal_attempt(item)
            receipt["attempts"].append(item)
            receipt["armed"] = True
            receipt["executed"] = True
            active_attempt = None
            if (request is not None and request.kind == "r013_compat_60"
                and item.get("evidence_eligible") is not True):
                raise YieldLiveError("completed resident attempt failed evidence gates")
            if phase == "qualify":
                evidence = item.get("evidence") or {}
                if item.get("evidence_eligible") is not True:
                    raise YieldLiveError("physical qualification failed; PATH not admitted")
                path = Path(args.run_dir) / f"qualification-{sequence}.json"
                body = json.dumps(item, sort_keys=True, allow_nan=False)
                with path.open("x") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                if path.read_text() != body:
                    raise YieldLiveError("qualification receipt cold-read differs")
                admission_writer = getattr(mature.writer, "writer", mature.writer)
                admission_writer.set_baseline_state(
                    consecutive_successes=sequence,
                    sticky_one_newton_latched=0,
                )
        last = receipt["attempts"][-1]
        evidence = last.get("evidence") or {}
        receipt["evidence_type"] = "ResidentAttemptEvidence"
        receipt["evidence_eligible"] = bool(last.get("evidence_eligible"))
        receipt["evidence_metrics"] = dict(evidence.get("metrics") or {})
        receipt["success"] = bool(receipt["evidence_eligible"])
    except BaseException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["failure_state"] = provider.snapshot()
        if active_attempt is not None and not any(
            isinstance(row, dict) and row.get("sequence") == active_attempt[0]
            for row in receipt.get("attempts", ())
        ):
            partial_writer = getattr(mature.writer, "writer", mature.writer)
            partial_item = {
                "sequence": active_attempt[0],
                "phase": active_attempt[1],
                "partial": True,
                "evidence": {
                    "eligible": False,
                    "complete": False,
                    "metrics": {
                        "complete": False,
                        "stage": active_attempt[1],
                        "failure": str(exc),
                        "raw_sensor_samples": len(getattr(partial_writer, "raw_observations", ())),
                        "robot_frames": len(getattr(partial_writer, "robot_observations", ())),
                    },
                    "failure": str(exc),
                },
                "state": provider.snapshot(),
                "lifecycle": {
                    "path_complete": False,
                    "home_verified": False,
                    "ready_for_next": False,
                    "program_stopped": False,
                    "sealed": False,
                },
            }
            if session.last_completed_sequence == active_attempt[0]:
                completed = session.last_completed_evidence
                payload = asdict(completed) if is_dataclass(completed) else dict(completed)
                partial_item['evidence'] = payload
                partial_item['failed_after_collector'] = str(exc)
                partial_item['evidence_eligible'] = bool(getattr(completed, 'eligible', False))
                metrics = payload.get('metrics', {})
                proof = payload.get('home_proof', {})
                partial_item['lifecycle']['path_complete'] = metrics.get('complete') is True
                partial_item['lifecycle']['home_verified'] = bool(
                    proof.get('stationary') is True and proof.get('fixed_home_route') is True)
                partial_item['partial'] = not partial_item['lifecycle']['path_complete']
                receipt['evidence_metrics'] = metrics
                receipt['evidence_eligible'] = False
            receipt.setdefault("attempts", []).append(partial_item)
        if request is None or request.kind != "diagnostic" or "of 550 bins" not in str(exc):
            raise
        receipt["diagnostic_incomplete"] = True
        receipt["evidence_eligible"] = False
        receipt["evidence_type"] = "PartialAttemptEvidence"
        receipt["evidence_metrics"] = receipt["attempts"][-1]["evidence"]["metrics"]
    finally:
        if old_handlers:
            for sig in old_handlers:
                signal.signal(sig, signal.SIG_IGN)
        try:
            receipt["stop"] = finish_session(
                session,
                reason="operator_stop" if not receipt.get("error") else "attempt_fault",
            )
        except BaseException as exc:
            receipt["stop"] = {"stopped": False, "tp_ack": False, "error": str(exc)}
            errors.append(f"stop: {type(exc).__name__}: {exc}")
        if owner is not None and owner_path is not None:
            owner["active"] = False
            owner_path.write_text(json.dumps(owner))
        stop_errors = receipt.get("stop", {}).get("cleanup_errors", [])
        if isinstance(stop_errors, list):
            errors.extend(str(value) for value in stop_errors)
        physical_fault = bool(receipt.get("error")) or bool(errors) or not receipt.get("stop", {}).get("stopped")
        if physical_fault and controller_transport is None:
            if defer_recovery:
                receipt["automatic_home_recovery_deferred"] = {
                    "state": "DEFERRED_UNTIL_SINGLE_WRITER_RELEASE",
                    "policy": "AUTO_HOME_WHEN_COMMANDABLE",
                    "reason": "resident supervisor still owns the single-writer lock",
                }
            else:
                recovery = automatic_home_after_fault(
                    run_dir=Path(args.run_dir),
                    controller_host=args.controller_host,
                    video_url=video_url,
                )
                receipt["automatic_home_recovery"] = recovery
                if recovery.get("success") is not True:
                    errors.append(
                        "automatic_home: "
                        + str(recovery.get("home_blocked_reason") or recovery.get("error") or recovery.get("state"))
                    )
        def seal_after_recovery():
            if partial_item is not None and partial_item.get("lifecycle", {}).get("sealed") is not True:
                session.seal_attempt(partial_item, service=False)
            receipt["service_tail"] = session.seal_service_tail()

        seal_deferred = bool(physical_fault and controller_transport is None and defer_recovery)
        if not seal_deferred:
            try:
                seal_after_recovery()
            except Exception as exc:
                errors.append(f"evidence seal: {type(exc).__name__}: {exc}")
        receipt["evidence_seal_deferred"] = seal_deferred
        receipt["cleanup_errors"] = errors
        receipt["session"] = {
            "schema": "yield-live-entry/resident-session-v2",
            "resource_scope": "one_writer_one_rtde_one_kunwei_one_provider",
            "attempt_count": len(receipt.get("attempts", ())),
            "resident_resources_reused": True,
            "resident_tp_stop_between_attempts": False,
            "lifecycle_events": list(session.lifecycle_events),
            "transport_trace": list(session.transport_trace),
            "refreshes": list(session.refreshes),
            "protocol_stop_ack": bool(receipt.get("stop", {}).get("protocol_stop_ack")),
            "program_stopped": bool(receipt.get("stop", {}).get("program_stopped")),
        }
        if attempt_count == 6:
            from contact_yield_resident_session import infrastructure_report
            receipt['infrastructure_acceptance'] = infrastructure_report(
                receipt, session_started_s=session_started_s,
                finished_s=lifecycle_clock(), live=controller_transport is None)
        try:
            receipt["lifecycle_events"] = lifecycle_events_from_writer(
                mature.writer,
                home_check_s=float(lifecycle_clock()),
                contact_search_s=None,
                stop_s=float(lifecycle_clock()),
                home_verified=bool(receipt.get("stop", {}).get("home_verified")),
            )
        except Exception as exc:
            receipt["lifecycle_events"] = []
            receipt["lifecycle_event_error"] = f"{type(exc).__name__}: {exc}"
        def encode(value):
            if is_dataclass(value):
                return asdict(value)
            if hasattr(value, "tolist"):
                return value.tolist()
            raise TypeError(f"unsupported receipt type: {type(value).__name__}")
        initial_document = recovery_handoff_receipt(receipt) if seal_deferred else receipt
        try:
            with (Path(args.run_dir) / "dispatch_receipt.json").open("x") as handle:
                json.dump(initial_document, handle, indent=2, sort_keys=True, allow_nan=False, default=encode)
                handle.write("\n")
        except Exception as exc:
            errors.append(f"receipt sink: {type(exc).__name__}: {exc}")
        if seal_deferred and deferred_seals is not None:
            def deferred_seal():
                try:
                    seal_after_recovery()
                    receipt["evidence_seal_deferred"] = False
                except Exception as exc:
                    errors.append(f"evidence seal: {type(exc).__name__}: {exc}")
                temporary = Path(args.run_dir) / "dispatch_receipt.seal.tmp"
                temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False, default=encode) + "\n")
                temporary.replace(Path(args.run_dir) / "dispatch_receipt.json")
            deferred_seals.append(deferred_seal)
        if owner_holder is not None:
            owner_holder.clear()
        if owner is not None and owner_path is not None:
            owner["active"] = False
            owner_path.write_text(json.dumps(owner))
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if not receipt.get("error") and (errors or not receipt["stop"].get("stopped")):
            raise YieldLiveError("attempt cleanup or physical stop confirmation failed")
    return receipt


def recovery_handoff_receipt(receipt):
    """Bounded metadata only; full traces remain owned until after recovery."""
    keys = ('schema', 'attempt_id', 'command', 'method', 'program', 'home_program',
            'runtime_protocol', 'evidence_scope', 'armed', 'executed', 'error',
            'stop', 'automatic_home_policy', 'automatic_home_recovery_deferred',
            'evidence_seal_deferred')
    return {key: receipt[key] for key in keys if key in receipt}


def time_clock(now_s: float | None) -> float:
    import time as _time

    return float(now_s) if now_s is not None else float(_time.time())


class ObservedTransport:
    """Propagate observer aborts to the same writer, including admission.

    Terminal polling stays available after STOP so a fault cannot prevent its
    own physical-stop observation. The observer itself never writes commands.
    """
    def __init__(self, transport, guard, *, stopping, clock):
        self.transport, self.guard = transport, guard
        self.stopping, self.clock = stopping, clock
        self.checked_at = None

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def poll_output(self, **kwargs):
        now = self.clock()
        if not self.stopping() and (self.checked_at is None or now-self.checked_at >= .020):
            self.guard()
            self.checked_at = now
        return self.transport.poll_output(**kwargs)


def stop_owner(owner_holder: list[Any] | None) -> dict[str, Any]:
    if not owner_holder:
        raise YieldLiveError("no live yield owner")
    mature = owner_holder[0]
    runtime = owner_holder[1] if len(owner_holder) > 1 else None
    errors: list[str] = []
    try:
        result = stop_and_confirm(mature.writer)
    except Exception as exc:
        result = {"stopped": False, "error": str(exc)}
        errors.append(str(exc))
    for target in (mature, runtime):
        if target is not None:
            try:
                target.close()
            except Exception as exc:
                errors.append(str(exc))
    owner_holder.clear()
    return {"schema": "yield-live-entry-stop-v1", **result,
            "errors": errors, "command_writer_count": 1}


def main(
    argv: list[str] | None = None,
    *,
    controller_transport: Any | None = None,
    kunwei_transport: Any | None = None,
    wall_clock=None,
    mono_clock=None,
    sleep=None,
    now_s: float | None = None,
    owner_holder: list[Any] | None = None,
) -> int:
    args = _parse_args(argv)
    if args.command == "status":
        _print_json(status_payload())
        return 0
    if args.command == "stop":
        result = stop_owner(owner_holder) if owner_holder else request_process_stop(args.run_dir)
        _print_json(result)
        return 0 if result.get("stopped") and not result.get("errors") else 1
    try:
        receipt = run_live(
            args,
            controller_transport=controller_transport,
            kunwei_transport=kunwei_transport,
            wall_clock=wall_clock,
            mono_clock=mono_clock,
            sleep=sleep,
            now_s=now_s,
            owner_holder=owner_holder,
        )
        _print_json(receipt)
        return 0
    except MethodUnavailableError as exc:
        _print_json(
            {
                "schema": "yield-live-entry-unavailable-v1",
                "error": str(exc),
                "executed": False,
                "formally_qualified": False,
            }
        )
        return 2
    except (YieldLiveError, YieldLiveWriterError, LiveWriterError, R006LiveAdapterError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
