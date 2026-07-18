#!/usr/bin/env python3
"""Own one continuous V3 bridge + campaign launch after HIL promotion."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import verify_step5d_autotune_v3_execution_readiness as execution_readiness
from prepare_step5d_autotune_launch import prepare
from preflight_readonly import dashboard_exchange
from run_step5d_autotune_campaign import validate_legacy_campaign_adoption
from run_step5d_autotune_v3_hil_hold import _stop_v3_program, _validate_preflight
from step5d_autotune_batch_plan import load_plan
from step5d_autotune_contract import ForceCandidate
from step5d_autotune_v3 import cli as v3_cli
from step5d_autotune_v3.launcher import build_bridge_argv, check_effective_config
from step5d_autotune_v3.runtime_calibration import bootstrap_stable_cuda_runtime
from step5d_autotune_v3.runtime_profile import (
    CONTROL_PROFILE_ID,
    DEFAULT_OVERLAY,
    RELEASE_STAGE_ID,
    TP_PROGRAM_ID,
    load_launch_profile,
    normalize_trial_overlay,
    overlay_fingerprint,
)
from step5d_autotune_v3.state import CampaignPaths, atomic_json, read_strict_json


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tools/run_step5d_autotune_v3_bridge.py"
RUNNER = ROOT / "tools/run_step5d_autotune_campaign.py"
RESULT_SCHEMA = "step5d.autotune-v3/live-campaign-launch-result-v1"
INITIAL_BATCH_SOURCE = "v3-initial-target-log2-batch-20260719"
DEFAULT_LEGACY_CAMPAIGN_ROOT = ROOT / "runs/step5d_native_autotune_recovered_runtime_v2"
DEFAULT_LEGACY_CAMPAIGN_EPOCH = 16
INITIAL_LOG2 = (
    (1.0, 0.0, 0.25),
    (0.75, 0.0, 0.5),
    (0.75, 0.25, 0.25),
    (0.75, 0.5, 0.25),
    (0.75, 0.75, 0.25),
)


class LiveLaunchError(RuntimeError):
    pass


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def initial_candidates() -> tuple[ForceCandidate, ...]:
    return tuple(
        ForceCandidate.from_log2(p=p, i=i, damping=damping)
        for p, i, damping in INITIAL_LOG2
    )


def _ensure_initial_batch(
    *,
    campaign_root: Path,
    campaign_id: str,
    launch_profile_path: Path,
) -> tuple[Any, Mapping[str, Any]]:
    paths = CampaignPaths(campaign_root)
    plan = load_plan(paths.candidate_plan, campaign_id=campaign_id)
    expected = initial_candidates()
    if plan.revision == 0:
        profile = load_launch_profile(launch_profile_path)
        overlays = []
        for candidate in expected:
            overlay = dict(DEFAULT_OVERLAY)
            overlay.update(
                {
                    "force_p_gain": candidate.force_p_gain,
                    "force_i_gain": candidate.force_i_gain,
                    "force_damping": candidate.force_damping,
                }
            )
            overlays.append(normalize_trial_overlay(overlay, profile=profile))
        validated, validated_overlays, _control, _profile, _launch = (
            v3_cli._validate_candidates(
                overlays,
                ledger_path=ROOT / "config/step5/step5d_autotune_v3_attempt_ledger.json",
                launch_profile_path=launch_profile_path,
            )
        )
        if tuple(validated) != expected:
            raise LiveLaunchError("validated V3 initial batch identity differs")
        plan = v3_cli._append_batch(
            paths,
            campaign_id=campaign_id,
            source=INITIAL_BATCH_SOURCE,
            candidates=expected,
        )
        overlay_plan = v3_cli._append_overlay_batch(
            paths,
            plan=plan,
            source=INITIAL_BATCH_SOURCE,
            candidates=expected,
            overlays=validated_overlays,
            launch_profile_fingerprint=profile.fingerprint,
        )
    else:
        if plan.batches[0] != expected:
            raise LiveLaunchError("existing candidate plan does not retain the exact V3 initial batch")
        overlay_plan = read_strict_json(paths.trial_overlays, role="V3 trial-overlay plan")
        if (
            not isinstance(overlay_plan, Mapping)
            or overlay_plan.get("schema") != "step5d.autotune-v3/trial-overlay-plan-v1"
            or overlay_plan.get("revision") != plan.revision
            or overlay_plan.get("candidate_count") != len(plan.candidates)
        ):
            raise LiveLaunchError("candidate and trial-overlay plans are not coherent")
    return plan, overlay_plan


def _latest_csv_row(path: Path) -> Mapping[str, str] | None:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return None
    return rows[-1] if rows else None


def _ready_home_zero_identity(
    row: Mapping[str, str] | None,
    *,
    require_playing: bool = True,
) -> bool:
    if row is None:
        return False
    try:
        state = int(float(row["ur_output_int_register_26"]))
        runtime_state = int(float(row["ur_runtime_state"]))
        safety = int(float(row["ur_safety_mode"]))
        identity = [
            int(float(row[f"ur_output_int_register_{index}"]))
            for index in (24, 25, 27, 28, 29, 30)
        ]
    except (KeyError, TypeError, ValueError):
        return False
    return (
        state == 10
        and (runtime_state == 2 if require_playing else runtime_state != 2)
        and safety == 1
        and identity == [0, 0, 0, 0, 0, 0]
    )


def _runtime_playing_normal(row: Mapping[str, str] | None) -> bool:
    if row is None:
        return False
    try:
        return (
            int(float(row["ur_runtime_state"])) == 2
            and int(float(row["ur_safety_mode"])) == 1
        )
    except (KeyError, TypeError, ValueError):
        return False


def _announce_stop_if_playing(robot_host: str) -> bool:
    try:
        state = dashboard_exchange(robot_host, ["programState"], timeout=2.0)
    except Exception:
        state = {}
    if str(state.get("programState", "")).startswith("PLAYING"):
        print("ACTION_REQUIRED_PRESS_TP_STOP_NOW", flush=True)
        return True
    return False


def _wait_file(path: Path, process: subprocess.Popen[Any], timeout_s: float, role: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LiveLaunchError(f"{role} process exited before readiness rc={process.returncode}")
        if path.is_file():
            return
        time.sleep(0.05)
    raise LiveLaunchError(f"{role} readiness timeout")


def _terminate(process: subprocess.Popen[Any] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
    return process.returncode


def run(args: argparse.Namespace) -> Mapping[str, Any]:
    readiness = execution_readiness.verify(ROOT, require_live=True)
    legacy_preflight = None
    if args.legacy_campaign_root is not None:
        legacy_preflight = validate_legacy_campaign_adoption(
            args.legacy_campaign_root.resolve(),
            campaign_epoch=args.legacy_campaign_epoch,
        )
    runtime_root = args.output_root.expanduser().absolute() / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    bridge_run = runtime_root / "bridge"
    bridge_runtime = bridge_run / "runtime"
    bridge_runtime.mkdir(parents=True, exist_ok=False, mode=0o700)
    launch_profile = load_launch_profile(args.launch_profile)
    check = check_effective_config(
        runtime_root=runtime_root,
        launch_profile_path=args.launch_profile,
        trial_overlay=DEFAULT_OVERLAY,
    )
    command = [
        sys.executable,
        str(WRAPPER),
        *build_bridge_argv(
            runtime_root,
            launch_profile=launch_profile,
            trial_overlay=DEFAULT_OVERLAY,
        )[2:],
    ]
    preflight = _validate_preflight(
        args.preflight,
        {"identity": readiness["identity"]},
    )

    authorization = bridge_runtime / "campaign_authorization.json"
    launch_plan_path = bridge_runtime / "campaign_launch_plan.json"
    prepared = prepare(
        SimpleNamespace(
            experiment_root=ROOT,
            campaign_root=args.campaign_root,
            legacy_campaign_root=args.legacy_campaign_root,
            legacy_campaign_epoch=args.legacy_campaign_epoch,
            authorization_file=authorization,
            authorization_source="canonical V3 live entrypoint internal campaign binding",
        )
    )
    atomic_json(launch_plan_path, prepared)
    plan, overlay_plan = _ensure_initial_batch(
        campaign_root=args.campaign_root,
        campaign_id=str(prepared["campaign_id"]),
        launch_profile_path=args.launch_profile,
    )
    paths = CampaignPaths(args.campaign_root)
    launch_id = uuid.uuid4().hex
    ticket = {
        "schema": "step5d.autotune-v3/runtime-ticket-v2",
        "parent_pid": os.getpid(),
        "argv_sha256": _sha256_json(command[2:]),
        "launch_id": launch_id,
        "scope": "live_continuous_campaign",
        "identity": readiness["identity"],
        "launch_profile_fingerprint": launch_profile.fingerprint,
        "trial_overlay_fingerprint": overlay_fingerprint(launch_profile, DEFAULT_OVERLAY),
        "release_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "tp_program_id": TP_PROGRAM_ID,
        "campaign_binding": {
            "campaign_id": prepared["campaign_id"],
            "campaign_epoch": prepared["campaign_epoch"],
            "candidate_plan_revision": plan.revision,
            "candidate_plan_sha256": _sha256_path(paths.candidate_plan),
            "trial_overlay_plan_sha256": _sha256_path(paths.trial_overlays),
        },
    }
    ticket_path = runtime_root / "runtime_ticket.json"
    atomic_json(ticket_path, ticket)
    environment = {
        **os.environ,
        "STEP5D_V3_RUNTIME_TICKET": str(ticket_path),
        "STEP5D_BRIDGE_LAUNCH_NONCE": uuid.uuid4().hex,
    }
    runner_ready = bridge_runtime / "campaign_runner_ready.json"
    bridge_log_path = args.output_root / "bridge.log"
    runner_log_path = args.output_root / "campaign_runner.log"
    bridge: subprocess.Popen[Any] | None = None
    runner: subprocess.Popen[Any] | None = None
    cleanup: Mapping[str, Any] | None = None
    play_observed = False
    try:
        with bridge_log_path.open("wb") as bridge_log:
            bridge = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=bridge_log,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            _wait_file(bridge_run / "bridge_ready.json", bridge, args.ready_timeout_s, "bridge")
            csv_path = bridge_run / "bridge_rtde_500hz.csv"
            deadline = time.monotonic() + args.ready_timeout_s
            while time.monotonic() < deadline:
                if bridge.poll() is not None:
                    raise LiveLaunchError("bridge exited before pre-Play READY_HOME")
                if _ready_home_zero_identity(
                    _latest_csv_row(csv_path),
                    require_playing=False,
                ):
                    break
                time.sleep(0.05)
            else:
                raise LiveLaunchError(
                    "TP must remain STOPPED at stationary zero-identity READY_HOME"
                )

            runner_command = [
                sys.executable,
                str(RUNNER),
                "--experiment-root",
                str(ROOT),
                "--bridge-run",
                str(bridge_run),
                "--campaign-root",
                str(args.campaign_root),
                "--mailbox",
                str(runtime_root / "command.json"),
                "--runner-ready-file",
                str(runner_ready),
                "--authorization-file",
                str(authorization),
                "--campaign-epoch",
                str(prepared["campaign_epoch"]),
                "--selection-policy",
                "codex_batches",
                "--candidate-plan",
                str(paths.candidate_plan),
                "--wait-for-home",
                "--recover-infra-aborted-active",
                "--v3-stop-latch",
                str(paths.stop_latch),
                "--v3-derived-postprocess-root",
                str(paths.postprocess),
                "--v3-trial-overlays",
                str(paths.trial_overlays),
                "--v3-launch-profile",
                str(args.launch_profile),
                "--v3-runtime-root",
                str(runtime_root),
            ]
            if args.legacy_campaign_root is not None:
                runner_command.extend(
                    [
                        "--legacy-campaign-root",
                        str(args.legacy_campaign_root),
                        "--legacy-campaign-epoch",
                        str(args.legacy_campaign_epoch),
                    ]
                )
            with runner_log_path.open("wb") as runner_log:
                runner = subprocess.Popen(
                    runner_command,
                    cwd=ROOT,
                    env=os.environ,
                    stdin=subprocess.DEVNULL,
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                _wait_file(runner_ready, runner, args.ready_timeout_s, "campaign runner")
                print("READY_FOR_ONE_PLAY_TO_MOVE", flush=True)
                deadline = time.monotonic() + args.play_timeout_s
                while time.monotonic() < deadline:
                    if bridge.poll() is not None:
                        raise LiveLaunchError("bridge exited while waiting for TP Play")
                    if runner.poll() is not None:
                        raise LiveLaunchError("campaign runner exited while waiting for TP Play")
                    if _runtime_playing_normal(_latest_csv_row(csv_path)):
                        play_observed = True
                        break
                    time.sleep(0.05)
                else:
                    raise LiveLaunchError("TP Play was not observed before timeout")
                print("V3_CAMPAIGN_RUNNING_ONE_PLAY_CONTINUOUS", flush=True)
                while bridge.poll() is None and runner.poll() is None:
                    time.sleep(0.2)
                if bridge.poll() is not None and runner.poll() is None:
                    raise LiveLaunchError(f"bridge exited unexpectedly rc={bridge.returncode}")
    finally:
        runner_rc = _terminate(runner)
        bridge_rc = _terminate(bridge)
        if bridge is not None:
            if play_observed:
                _announce_stop_if_playing(str(check["effective_config"]["robot_host"]))
            cleanup = _stop_v3_program(str(check["effective_config"]["robot_host"]))
        atomic_json(
            args.output_root / "cleanup.json",
            {
                "runner_exit_code": runner_rc,
                "bridge_exit_code": bridge_rc,
                "program_stop": cleanup,
            },
        )
    if cleanup is not None and cleanup.get("ok") is not True:
        raise LiveLaunchError("TP program remains PLAYING; PRESS_TP_STOP")
    result = {
        "schema": RESULT_SCHEMA,
        "ok": runner is not None and runner.returncode == 0,
        "launch_id": launch_id,
        "campaign_id": prepared["campaign_id"],
        "campaign_epoch": prepared["campaign_epoch"],
        "legacy_preflight": legacy_preflight,
        "candidate_plan_revision": plan.revision,
        "bridge_run": str(bridge_run),
        "preflight_controller_identity_sha256": preflight["controller_identity_sha256"],
        "program_stop": cleanup,
    }
    atomic_json(args.output_root / "live_campaign_result.json", result)
    if result["ok"] is not True:
        raise LiveLaunchError(f"campaign runner exited rc={None if runner is None else runner.returncode}")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument(
        "--campaign-root",
        type=Path,
        default=ROOT / "runs/step5d_autotune_v3",
    )
    parser.add_argument(
        "--legacy-campaign-root",
        type=Path,
        default=DEFAULT_LEGACY_CAMPAIGN_ROOT,
    )
    parser.add_argument(
        "--legacy-campaign-epoch",
        type=int,
        default=DEFAULT_LEGACY_CAMPAIGN_EPOCH,
    )
    parser.add_argument(
        "--launch-profile",
        type=Path,
        default=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    parser.add_argument("--ready-timeout-s", type=float, default=45.0)
    parser.add_argument("--play-timeout-s", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        result = {"schema": RESULT_SCHEMA, "ok": False, "blocker": str(exc)}
        try:
            atomic_json(args.output_root / "live_campaign_result.json", result)
        except Exception as evidence_exc:
            result["evidence_write_blocker"] = str(evidence_exc)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    bootstrap_stable_cuda_runtime()
    raise SystemExit(main())
