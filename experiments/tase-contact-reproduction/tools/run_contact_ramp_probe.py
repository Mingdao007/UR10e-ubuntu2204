#!/usr/bin/env python3
"""Run the isolated 8->4->3->2->1 s contact-ramp diagnostic ladder.

This runner uses the bound Step5d supervisor for every physical attempt. It
never issues motion itself, never starts Figure-eight PATH, and only advances
after the prior attempt has an eligible qualification receipt plus a fresh
stopped joint-Home proof.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDING = ROOT / "programs/step5/step5d/contact-ramp-probe-r002/contact_ramp_probe_v1.binding.json"
DURATIONS_S = (8, 4, 3, 2, 1)
METHOD = "TASE_RNN_MATURE"
VIDEO_URL = "rtsp://127.0.0.1:8554/arm"
CONTROL_CPU = 4


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["AMENT_PREFIX_PATH"] = "/opt/ros/humble"
    env["PYTHONPATH"] = ":".join((
        str(ROOT / "tools"),
        "/opt/ros/humble/lib/python3.10/site-packages",
        "/opt/ros/humble/local/lib/python3.10/dist-packages",
    ))
    env["OMP_NUM_THREADS"] = "1"
    env["OPENBLAS_NUM_THREADS"] = "1"
    return env


def _run_command(
    command: list[str], *, log_path: Path, cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=_runtime_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "COMMAND: " + json.dumps(command) + "\n"
        + "RETURN_CODE: " + str(completed.returncode) + "\n"
        + "--- stdout ---\n" + completed.stdout
        + "\n--- stderr ---\n" + completed.stderr,
        encoding="utf-8",
    )
    return completed


def _strict_joint_home(
    sample: Mapping[str, Any] | None,
    dashboard: Mapping[str, Any] | None,
    *,
    home_q: tuple[float, ...],
    home_pose: tuple[float, ...],
) -> dict[str, Any]:
    if not isinstance(sample, Mapping) or not isinstance(dashboard, Mapping):
        return {"verified": False, "reason": "missing fresh stopped sample or Dashboard state"}
    try:
        q = tuple(float(value) for value in sample["actual_q"])
        pose = tuple(float(value) for value in sample["actual_TCP_pose"])
        qd = tuple(float(value) for value in sample["actual_qd"])
        tcp_speed = tuple(float(value) for value in sample["actual_TCP_speed"])
        if any(len(v) != 6 for v in (q, pose, qd, tcp_speed)):
            raise ValueError("joint/TCP sample length differs")
        values = (*q, *pose, *qd, *tcp_speed)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("joint/TCP sample is nonfinite")
        q_error = max(abs(a - b) for a, b in zip(q, home_q, strict=True))
        p_error = math.dist(pose[:3], home_pose[:3])
        import numpy as np
        from contact_yield_math import so3_exp, so3_log
        orientation_error = float(np.linalg.norm(
            so3_log(so3_exp(pose[3:]) @ so3_exp(home_pose[3:]).T)
        ))
        stopped = (
            dashboard.get("is in remote control") == "true"
            and dashboard.get("safetymode") == "Safetymode: NORMAL"
            and dashboard.get("running") == "Program running: false"
            and str(dashboard.get("programState", "")).startswith("STOPPED")
            and int(sample.get("runtime_state", -1)) == 1
            and int(sample.get("safety_mode", -1)) == 1
            and int(sample.get("robot_mode", -1)) == 7
        )
        stationary = max(map(abs, qd)) <= .001 and math.sqrt(sum(v*v for v in tcp_speed[:3])) <= .0005
        verified = (
            stopped and stationary and q_error <= .020
            and p_error <= .001 and orientation_error <= .005
        )
        return {
            "verified": bool(verified),
            "joint_max_error_rad": q_error,
            "joint_tolerance_rad": .020,
            "tcp_position_error_m": p_error,
            "tcp_position_tolerance_m": .001,
            "tcp_orientation_error_rad": orientation_error,
            "tcp_orientation_tolerance_rad": .005,
            "max_abs_joint_speed_rad_s": max(map(abs, qd)),
            "tcp_linear_speed_m_s": math.sqrt(sum(v*v for v in tcp_speed[:3])),
            "stationary": stationary,
            "dashboard_stopped_normal_remote": stopped,
            "sample": dict(sample),
            "dashboard": dict(dashboard),
            "reason": None if verified else "fresh stopped joint-Home gate failed",
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return {"verified": False, "reason": f"invalid Home evidence: {exc}"}


def _extract_attempt(run_dir: Path, *, home_q: tuple[float, ...], home_pose: tuple[float, ...]) -> dict[str, Any]:
    supervisor = _json(run_dir / "supervisor-result.json") or {}
    dispatch = _json(run_dir / "dispatch_receipt.json") or {}
    attempts = dispatch.get("attempts")
    attempt = attempts[0] if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict) else {}
    evidence = attempt.get("evidence") if isinstance(attempt.get("evidence"), dict) else {}
    dash_stop = supervisor.get("dashboard_stop") if isinstance(supervisor.get("dashboard_stop"), dict) else {}
    if not dash_stop:
        dash_stop = supervisor.get("autonomous_home_verified_joint")
    if not isinstance(dash_stop, dict):
        dash_stop = {}
    home = _strict_joint_home(
        dash_stop.get("sample"), dash_stop.get("dashboard"),
        home_q=home_q, home_pose=home_pose,
    )
    home.update({
        "tp_home_proof": evidence.get("home_proof"),
        "tp_return_gate_passed": evidence.get("return_gate_passed"),
    })
    metrics = evidence.get("metrics") if isinstance(evidence.get("metrics"), dict) else {}
    probe_metrics = attempt.get("contact_ramp_probe_diagnostics")
    if isinstance(probe_metrics, dict):
        metrics = {**metrics, "contact_ramp_probe": dict(probe_metrics)}
    passed = bool(
        supervisor.get("success") is True
        and dispatch.get("evidence_eligible") is True
        and evidence.get("qualification_passed") is True
        and evidence.get("return_gate_passed") is True
        and home.get("verified") is True
    )
    return {
        "success": passed,
        "home_verified": bool(home.get("verified")),
        "home": home,
        "supervisor_success": supervisor.get("success"),
        "dispatch_evidence_eligible": dispatch.get("evidence_eligible"),
        "qualification_passed": evidence.get("qualification_passed"),
        "return_gate_passed": evidence.get("return_gate_passed"),
        "error": supervisor.get("error") or dispatch.get("error"),
        "failure_state": dispatch.get("failure_state"),
        "qualification_metrics": metrics,
        "attempt_evidence": evidence,
        "supervisor_result_path": str(run_dir / "supervisor-result.json"),
        "dispatch_receipt_path": str(run_dir / "dispatch_receipt.json"),
    }


def _attempt_commands(
    *,
    run_dir: Path,
    binding: Path,
    ramp_duration_s: int,
    video_policy: str,
    video_url: str,
    control_cpu: int,
) -> tuple[list[str], list[str]]:
    prepare = [
        sys.executable, str(ROOT / "tools/prepare_figure8.py"),
        "--run-dir", str(run_dir), "--method", METHOD,
        "--video-policy", video_policy,
        "--contact-ramp-probe-binding", str(binding),
        "--ramp-duration-s", str(ramp_duration_s),
    ]
    supervise = [
        str(ROOT / "scripts/contact-yield-live.sh"), "supervise",
        "--action", "qualify", "--method", METHOD,
        "--run-dir", str(run_dir), "--readback-dir", str(run_dir / "readback"),
        "--control-cpu", str(control_cpu), "--video-policy", video_policy,
        "--video-url", video_url,
        "--contact-ramp-probe-binding", str(binding),
        "--ramp-duration-s", str(ramp_duration_s),
    ]
    return prepare, supervise


def _run_attempt(
    *, run_dir: Path, binding: Path, ramp_duration_s: int, video_policy: str,
    video_url: str, control_cpu: int,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = _run_command,
) -> dict[str, Any]:
    if run_dir.exists():
        raise FileExistsError(f"attempt directory already exists: {run_dir}")
    prepare_cmd, supervise_cmd = _attempt_commands(
        run_dir=run_dir, binding=binding, ramp_duration_s=ramp_duration_s,
        video_policy=video_policy, video_url=video_url, control_cpu=control_cpu,
    )
    prep = command_runner(prepare_cmd, log_path=run_dir.parent / f"{run_dir.name}-prepare.log")
    if prep.returncode != 0:
        summary = {
            "success": False,
            "dispatched": False,
            "home_verified": None,
            "phase": "preparation",
            "error": f"prepare_figure8 exited {prep.returncode}",
            "prepare_stdout": prep.stdout,
            "prepare_stderr": prep.stderr,
        }
        _atomic_json(run_dir.parent / f"{run_dir.name}-attempt-summary.json", summary)
        return summary
    readback = _json(run_dir / "readback-results.json") or {}
    if readback.get("pass") is not True:
        summary = {
            "success": False,
            "dispatched": False,
            "home_verified": None,
            "phase": "preparation-readback",
            "error": "probe package read-back did not pass",
            "readback": readback,
        }
        _atomic_json(run_dir.parent / f"{run_dir.name}-attempt-summary.json", summary)
        return summary
    live = command_runner(supervise_cmd, log_path=run_dir.parent / f"{run_dir.name}-supervise.log")
    result = _extract_attempt(
        run_dir,
        home_q=tuple(json.loads(binding.read_text(encoding="utf-8"))["home_q_rad"]),
        home_pose=tuple(json.loads(binding.read_text(encoding="utf-8"))["home_pose_m_rad"]),
    )
    dispatch = _json(run_dir / "dispatch_receipt.json") or {}
    result.update({
        "dispatched": bool(dispatch.get("armed")),
        "supervisor_return_code": int(live.returncode),
        "video_policy": video_policy,
        "video_evidence": (_json(run_dir / "supervisor-result.json") or {}).get("video_evidence"),
        "run_dir": str(run_dir),
    })
    if live.returncode != 0 and result.get("success"):
        result["success"] = False
        result["error"] = f"supervisor exited {live.returncode} despite apparently passing receipt"
    _atomic_json(run_dir.parent / f"{run_dir.name}-attempt-summary.json", result)
    return result


def run_campaign(
    run_root: Path,
    *,
    binding_path: Path = DEFAULT_BINDING,
    video_policy: str = "required",
    video_url: str = VIDEO_URL,
    control_cpu: int = CONTROL_CPU,
    restore_production_readback: bool = True,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = _run_command,
) -> dict[str, Any]:
    from build_contact_ramp_probe import RAMP_DURATIONS_S, PROGRAM
    from contact_yield_live_contract import load_contact_ramp_probe_identity_contract

    root = run_root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"run root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    binding = binding_path.expanduser().resolve()
    contract = load_contact_ramp_probe_identity_contract(binding)
    if tuple(RAMP_DURATIONS_S) != DURATIONS_S or contract.program != PROGRAM:
        raise ValueError("probe package identity or rung set differs")
    if video_policy not in {"required", "evidence-only"}:
        raise ValueError(f"unknown video policy {video_policy}")
    if isinstance(control_cpu, bool) or not isinstance(control_cpu, int) or control_cpu < 0:
        raise ValueError("control CPU must be a nonnegative integer")

    start = dt.datetime.now(dt.timezone.utc).isoformat()
    ledger: dict[str, Any] = {
        "schema": "contact-ramp-probe-ladder-v1",
        "started_at": start,
        "experiment_identity": {
            "method": METHOD,
            "program": contract.program,
            "qualification_only": True,
            "figure8_path": False,
            "bo_observation": False,
            "target_force_n": 5.0,
            "ramp_durations_s": list(DURATIONS_S),
            "force_norm_stop_n": 10.0,
            "home_q_rad": list(contract.home_q),
            "home_pose_m_rad_crosscheck": list(contract.home_pose),
        },
        "video_policy": video_policy,
        "video_url": video_url,
        "attempts": [],
        "initial_passed_durations_s": [],
        "repeat_validation": [],
        "selected_stable_duration_s": None,
        "production_readback": None,
        "complete": False,
    }
    _atomic_json(root / "ladder-progress.json", ledger)

    def persist_attempt(duration: int, label: str) -> dict[str, Any]:
        attempt_dir = root / f"ramp-{duration:02d}s" / label
        attempt_dir.parent.mkdir(parents=True, exist_ok=True)
        item = _run_attempt(
            run_dir=attempt_dir,
            binding=binding,
            ramp_duration_s=duration,
            video_policy=video_policy,
            video_url=video_url,
            control_cpu=control_cpu,
            command_runner=command_runner,
        )
        item["ramp_duration_s"] = duration
        item["label"] = label
        ledger["attempts"].append(item)
        _atomic_json(root / "ladder-progress.json", ledger)
        return item

    passed: list[int] = []
    for duration in DURATIONS_S:
        item = persist_attempt(duration, "initial")
        if item.get("success") is not True:
            ledger["stopped_after"] = duration
            ledger["stop_reason"] = item.get("error") or "rung did not pass qualification and joint Home"
            break
        if item.get("home_verified") is not True:
            ledger["stopped_after"] = duration
            ledger["stop_reason"] = "attempt passed control evidence but joint Home was not freshly verified"
            break
        passed.append(duration)
        ledger["initial_passed_durations_s"] = list(passed)
        _atomic_json(root / "ladder-progress.json", ledger)

    # Retest the fastest sequentially passing rung twice. If it is intermittent,
    # move back through the already-passed slower rungs without changing gates.
    if passed and all(item.get("home_verified") is True for item in ledger["attempts"]):
        for candidate in reversed(passed):
            repeats: list[dict[str, Any]] = []
            for repeat_index in (1, 2):
                repeat = persist_attempt(candidate, f"repeat-{repeat_index:02d}")
                repeats.append(repeat)
                if repeat.get("home_verified") is not True:
                    ledger["stop_reason"] = (
                        f"{candidate}s repeat did not verify joint Home; no further motion dispatched"
                    )
                    break
            ledger["repeat_validation"].append({
                "duration_s": candidate,
                "attempts": repeats,
                "both_passed": len(repeats) == 2 and all(
                    row.get("success") is True and row.get("home_verified") is True
                    for row in repeats
                ),
            })
            _atomic_json(root / "ladder-progress.json", ledger)
            if len(repeats) != 2 or not all(row.get("home_verified") is True for row in repeats):
                break
            if all(row.get("success") is True for row in repeats):
                ledger["selected_stable_duration_s"] = candidate
                break

    all_attempts_home = bool(ledger["attempts"]) and all(
        item.get("home_verified") is True for item in ledger["attempts"]
    )
    if restore_production_readback and all_attempts_home:
        restore_dir = root / "production-package-readback"
        restore_dir.parent.mkdir(parents=True, exist_ok=True)
        restore_cmd = [
            sys.executable, str(ROOT / "tools/prepare_figure8.py"),
            "--run-dir", str(restore_dir), "--method", METHOD,
            "--video-policy", video_policy,
        ]
        restore = command_runner(
            restore_cmd, log_path=root / "production-package-readback.log",
        )
        proof = _json(restore_dir / "readback-results.json") or {}
        restored = None
        restored_result = None
        if restore.returncode == 0 and proof.get("pass") is True:
            restore_owner_cmd = [
                str(ROOT / "scripts/contact-yield-live.sh"), "supervise",
                "--action", "restore-package", "--method", METHOD,
                "--run-dir", str(restore_dir),
                "--readback-dir", str(restore_dir / "readback"),
                "--control-cpu", str(control_cpu),
                "--video-policy", video_policy, "--video-url", video_url,
            ]
            restored = command_runner(
                restore_owner_cmd,
                log_path=root / "production-package-restore.log",
            )
            restored_result = _json(restore_dir / "supervisor-result.json") or {}
        ledger["production_readback"] = {
            "return_code": restore.returncode,
            "readback_pass": proof.get("pass") is True,
            "readback_receipt": str(restore_dir / "readback-results.json"),
            "restore_owner_return_code": None if restored is None else restored.returncode,
            "restore_owner_success": None if restored_result is None else restored_result.get("success"),
            "program_loaded": (
                False if restored_result is None
                else bool(((restored_result.get("body") or {}).get("program_loaded")))
            ),
            "program_stopped": (
                False if restored_result is None
                else restored_result.get("program_stopped") is True
            ),
            "program_started": False,
            "motion_dispatched": False,
        }
    ledger["complete"] = bool(
        ledger["selected_stable_duration_s"] is not None
        and (ledger.get("production_readback") or {}).get("readback_pass") is True
        and (ledger.get("production_readback") or {}).get("restore_owner_success") is True
        and (ledger.get("production_readback") or {}).get("program_loaded") is True
        and (ledger.get("production_readback") or {}).get("program_stopped") is True
    )
    ledger["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_json(root / "ladder-progress.json", ledger)
    return ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--video-policy", choices=("required", "evidence-only"), default="required")
    parser.add_argument("--video-url", default=VIDEO_URL)
    parser.add_argument("--control-cpu", type=int, default=CONTROL_CPU)
    parser.add_argument("--no-restore-production-readback", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_campaign(
            args.run_dir,
            binding_path=args.binding,
            video_policy=args.video_policy,
            video_url=args.video_url,
            control_cpu=args.control_cpu,
            restore_production_readback=not args.no_restore_production_readback,
        )
    except Exception as exc:
        result = {
            "schema": "contact-ramp-probe-ladder-v1",
            "complete": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            _atomic_json(args.run_dir.expanduser().resolve() / "terminal-error.json", result)
        except Exception:
            pass
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("complete") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
