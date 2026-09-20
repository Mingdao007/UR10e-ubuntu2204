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


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REMAINING_AFTER_ENTRY = (
    "physical contact qualification of SFC/DSFC/MSFC",
    "full-chain 500 Hz formal timing on this route",
    "current_stage promotion after main validates the actual route",
)


class YieldLiveError(RuntimeError):
    """Yield live entry failed closed."""


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
    qualify.add_argument("--controller-host")
    qualify.add_argument("--kunwei-host")
    qualify.add_argument("--kunwei-port", type=int, default=5152)
    qualify.add_argument("--route-id", default="r006-yield-live")
    qualify.add_argument("--attempt-id", default="r006-yield-live-qualify")
    qualify.add_argument("--authority-root", type=Path)
    qualify.add_argument("--control-cpu", type=int)
    pilot = sub.add_parser("pilot", help="Open, ARM, PATH 2s/10s/full, stop/Home")
    pilot.add_argument("--method", required=True)
    pilot.add_argument("--duration", required=True)
    pilot.add_argument("--run-dir", type=Path, required=True)
    pilot.add_argument("--qp-library", type=Path)
    pilot.add_argument("--controller-host")
    pilot.add_argument("--kunwei-host")
    pilot.add_argument("--kunwei-port", type=int, default=5152)
    pilot.add_argument("--route-id", default="r006-yield-live")
    pilot.add_argument("--attempt-id", default="r006-yield-live-pilot")
    pilot.add_argument("--authority-root", type=Path)
    pilot.add_argument("--control-cpu", type=int)
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
    if str(Path(__file__).resolve()).encode() not in argv:
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
) -> dict[str, Any]:
    if args.command not in {"qualify", "pilot"}:
        raise YieldLiveError(f"unknown live command {args.command!r}")
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
    if (Path(args.run_dir) / "dispatch_receipt.json").exists():
        raise YieldLiveError("run directory already contains an attempt receipt")
    if controller_transport is None and getattr(args, "control_cpu", None) is None:
        raise YieldLiveError("hardware execution requires the selected --control-cpu")
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
        "rnn_hash_or_profile": provider.solver_profile.as_dict() if args.method == "TASE_RNN_MATURE" else False,
        "prewarmed_before_endpoints": True,
        "provider_prewarm": getattr(provider, "prewarm_record", None),
        "command_timeline": getattr(provider, "command_timeline", []),
        "opened": False,
        "armed": False,
        "executed": False,
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
    try:
        mature.open(live_ack=R005_LIVE_ACK)
        receipt["opened"] = True
        if getattr(mature.writer, "_r013_path_early_end_controller", None) is not None:
            mature.writer._r013_path_early_end_controller.writer = (
                mature.writer._controller_transport
            )
        from dataclasses import asdict
        import copy
        import os
        seed_state = provider.snapshot()
        phases = ["qualify"] if args.command == "qualify" else ["qualify"] * 3 + ["pilot"]
        receipt["attempts"] = []
        for sequence, phase in enumerate(phases, start=1):
            # Separate physical attempts start from the documented seed; state
            # is never reset inside contact, disturbance or recovery.
            provider.restore(copy.deepcopy(seed_state))
            attempt = native_attempt(command=phase, epoch=prerequisites.session_epoch,
                                     sequence=sequence)
            mature.writer._r013_path_early_end_controller.arm(sequence)
            mature.dispatch(attempt, object())
            if controller_transport is None:
                from step5d_autotune_v4_r013.timing_scheduler import (
                    FormalTimingSchedulerLeaseV2, TimingSchedulerProfileV1)
                lease = FormalTimingSchedulerLeaseV2(TimingSchedulerProfileV1(
                    control_cpu_affinity=(int(args.control_cpu),)))
                mature.writer.install_timing_scheduler_lease(lease)
                mature.writer.prepare_timing_scheduler_lease()
            mature.arm(attempt)
            receipt["armed"] = True
            evidence = mature.run_60s(attempt)
            receipt["executed"] = True
            scheduler = mature.writer.release_timing_scheduler_lease()
            item = {"sequence": sequence, "phase": phase, "scheduler": scheduler,
                    "evidence": asdict(evidence), "state": provider.snapshot()}
            receipt["attempts"].append(item)
            if phase == "qualify":
                if not evidence.eligible:
                    raise YieldLiveError("physical qualification failed; PATH not admitted")
                # Count only actual completed, timing-qualified, returned
                # attempts, after durable write and cold-read verification.
                path = Path(args.run_dir) / f"qualification-{sequence}.json"
                body = json.dumps(item, sort_keys=True, allow_nan=False)
                with path.open("x") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                if path.read_text() != body:
                    raise YieldLiveError("qualification receipt cold-read differs")
                mature.writer.set_baseline_state(consecutive_successes=sequence,
                                                 sticky_one_newton_latched=0)
        receipt["evidence_type"] = type(evidence).__name__
        receipt["evidence_eligible"] = bool(evidence.eligible)
        receipt["evidence_metrics"] = dict(evidence.metrics)
        return receipt
    except BaseException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["failure_state"] = provider.snapshot()
        raise
    finally:
        errors = []
        if old_handlers:
            # A repeated request must not interrupt transport cleanup.
            for sig in old_handlers:
                signal.signal(sig, signal.SIG_IGN)
        try:
            receipt["stop"] = stop_and_confirm(mature.writer)
        except Exception as exc:
            receipt["stop"] = {"stopped": False, "error": str(exc)}
            errors.append(f"stop: {exc}")
        for label, close in (("mature", mature.close), ("runtime", runtime.close)):
            try:
                close()
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        from dataclasses import asdict, is_dataclass
        def encode(value):
            if is_dataclass(value):
                return asdict(value)
            if hasattr(value, "tolist"):
                return value.tolist()
            raise TypeError(f"unsupported evidence type: {type(value).__name__}")
        for filename, rows in (
            ("raw_sensor.jsonl", mature.writer.raw_observations),
            ("robot_frames.jsonl", mature.writer.robot_observations),
            ("admission_robot_frames.jsonl", mature.writer.admission_robot_observations),
            ("rejected_robot_frames.jsonl", mature.writer.rejected_robot_observations),
            ("published_packets.jsonl", mature.writer.command_observations),
        ):
            try:
                with (Path(args.run_dir) / filename).open("x") as handle:
                    for row in rows:
                        handle.write(json.dumps(row, default=encode, allow_nan=False) + "\n")
            except Exception as exc:
                errors.append(f"evidence {filename}: {exc}")
        receipt["cleanup_errors"] = errors
        # Preserve failed attempts as well as successful ones. Never overwrite
        # a prior attempt: the caller must supply a fresh run directory.
        with (Path(args.run_dir) / "dispatch_receipt.json").open("x") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        if owner_holder is not None:
            owner_holder.clear()
        if owner_path is not None:
            owner["active"] = False
            owner_path.write_text(json.dumps(owner))
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if not receipt.get("error") and (errors or not receipt["stop"].get("stopped")):
            raise YieldLiveError("attempt cleanup or physical stop confirmation failed")


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
