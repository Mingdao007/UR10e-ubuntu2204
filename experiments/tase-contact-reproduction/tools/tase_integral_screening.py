"""Prepare, run, and score the five-arm integral screening campaign."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any
import statistics

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "tase_integral_screening_v1.json"
SUPPORTED_PROTOCOLS = {
    "figure8_window60_r013_rate400_v1": "r013_60_rate400",
    "figure8_window60_r013_rate460_v1": "r013_60_rate460",
}


def load_screening_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != "tase.integral-screening-v1":
        raise ValueError("screening schema differs")
    if payload.get("method") != "TASE_RNN_MATURE":
        raise ValueError("screening method must be TASE_RNN_MATURE")
    protocol_id = payload.get("protocol_id")
    if protocol_id not in SUPPORTED_PROTOCOLS or payload.get("duration_token") != SUPPORTED_PROTOCOLS[protocol_id]:
        raise ValueError("screening must bind a supported 60 s R013 rate protocol")
    expected_triplet = payload.get("expected_controller_readback_triplet_sha256")
    if expected_triplet is not None and (
        not isinstance(expected_triplet, dict)
        or set(expected_triplet) != {"script", "txt", "urp"}
        or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
               for value in expected_triplet.values())
    ):
        raise ValueError("expected controller read-back triplet must bind three SHA-256 values")
    if int(payload.get("repeats_per_arm", 0)) != 5:
        raise ValueError("screening requires five repeats per arm")
    arms = payload.get("arms")
    if not isinstance(arms, list) or [a.get("arm_id") for a in arms] != ["A", "B", "C", "D", "E"]:
        raise ValueError("screening arms must be ordered A, B, C, D, E")
    seen = set()
    for arm in arms:
        required = {"arm_id", "label", "force_integral_limit_n_s", "force_integral_policy", "force_integral_authority_error_n"}
        if set(arm) != required:
            raise ValueError("screening arm fields differ")
        if arm["arm_id"] in seen:
            raise ValueError("screening arm ids must be unique")
        seen.add(arm["arm_id"])
        if float(arm["force_integral_limit_n_s"]) not in {0.1, 0.5, 1.0}:
            raise ValueError("screening integral limit is outside the approved set")
        expected_policy = {
            "A": "legacy-clamp-v1", "B": "legacy-clamp-v1",
            "C": "legacy-clamp-v1", "D": "conditional-double-clamp-v1",
            "E": "integral-off-v1",
        }[arm["arm_id"]]
        if arm["force_integral_policy"] != expected_policy:
            raise ValueError("screening integral policy is unknown")
    return payload


def build_schedule(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = load_screening_config() if config is None else config
    rng = np.random.default_rng(int(config["seed"]))
    arms = config["arms"]
    rows: list[dict[str, Any]] = []
    for block in range(int(config["repeats_per_arm"])):
        order = [arms[int(i)] for i in rng.permutation(len(arms))]
        for position, arm in enumerate(order):
            rows.append({
                "schema": "tase.integral-screening-attempt-v1",
                "protocol_id": config["protocol_id"],
                "duration_token": config["duration_token"],
                "method": config["method"],
                "block": block,
                "position": position,
                "arm_id": arm["arm_id"],
                "candidate_id": f"screen-{arm['arm_id']}-{block:02d}",
                "Md_scalar": float(config["Md_scalar"]),
                "Bd_scalar": float(config["Bd_scalar"]),
                "frozen": {
                    "kp": 4.0,
                    "ko": 5.0,
                    "kf": 1.0,
                    "force_target_n": float(config["target_force_n"]),
                    "force_integral_limit_n_s": float(arm["force_integral_limit_n_s"]),
                    "force_integral_policy": arm["force_integral_policy"],
                    "force_integral_authority_error_n": float(arm["force_integral_authority_error_n"]),
                    "force_sign_convention": "step5_step6_positive_normal_load",
                },
                "status": "planned",
            })
    return rows


def _schedule_document(config: dict[str, Any], *, config_sha256: str | None = None) -> dict[str, Any]:
    return {
        "schema": "tase.integral-screening-schedule-v1",
        "method": config["method"],
        "protocol_id": config["protocol_id"],
        "duration_token": config["duration_token"],
        "seed": int(config["seed"]),
        "repeats_per_arm": int(config["repeats_per_arm"]),
        "arm_ids": [arm["arm_id"] for arm in config["arms"]],
        "expected_controller_readback_triplet_sha256": config.get("expected_controller_readback_triplet_sha256"),
        "config_sha256": config_sha256,
        "rows": build_schedule(config),
    }


def write_schedule(output: Path, config_path: Path = DEFAULT_CONFIG) -> Path:
    config = load_screening_config(config_path)
    config_sha256 = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
    schedule = _schedule_document(config, config_sha256=config_sha256)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schedule, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def prepare_campaign(campaign_dir: Path, config_path: Path = DEFAULT_CONFIG) -> Path:
    """Freeze all 25 Home-loaded candidates for one resident session."""

    config = load_screening_config(config_path)
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    campaign_dir.mkdir(parents=True, exist_ok=False)
    config_path = Path(config_path)
    config_bytes = config_path.read_bytes()
    config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    rows = build_schedule(config)
    candidates = campaign_dir / "candidates"
    candidates.mkdir()
    parameter_files: list[str] = []
    for ordinal, row in enumerate(rows, start=1):
        payload = {
            "schema": "tase.outer-parameters-v1",
            "candidate_id": row["candidate_id"],
            "stage": "integral_screening",
            "index": ordinal - 1,
            "Md_scalar": row["Md_scalar"],
            "Bd_scalar": row["Bd_scalar"],
            "protocol_id": row["protocol_id"],
            "duration_token": row["duration_token"],
            "path_duration_s": 60.0,
            "frozen": row["frozen"],
        }
        path = candidates / f"{ordinal:02d}-{row['candidate_id']}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        parameter_files.append(str(path.relative_to(campaign_dir)))
    (campaign_dir / "config-frozen.json").write_bytes(config_bytes)
    (campaign_dir / "screening-schedule.json").write_text(
        json.dumps(_schedule_document(config, config_sha256=config_sha256),
                   indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = campaign_dir / "resident-parameter-manifest.json"
    manifest.write_text(json.dumps({
        "schema": "tase.resident-parameter-manifest-v1",
        "protocol_id": config["protocol_id"],
        "duration_token": config["duration_token"],
        "parameter_files": parameter_files,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _session_verified_closed(session: Path) -> bool:
    dispatch_path = session / "dispatch_receipt.json"
    if not dispatch_path.is_file():
        no_dispatch = session / "terminal-no-dispatch-receipt.json"
        return bool(
            no_dispatch.is_file()
            and json.loads(no_dispatch.read_text()).get("writer_artifacts_absent") is True
        )
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    if len(dispatch.get("attempts") or []) != len(list(session.glob("attempts/*/attempt-result.json"))):
        return False
    supervisor_path = session / "supervisor-result.json"
    supervisor = (json.loads(supervisor_path.read_text(encoding="utf-8"))
                  if supervisor_path.is_file() else {})
    recovery = supervisor.get("autonomous_home_recovery") or dispatch.get("automatic_home_recovery")
    if recovery is not None:
        # Recovery happens after the original stop. A later BLOCKED recovery
        # supersedes an earlier Home/STOP observation. Accept both the older
        # nested dashboard_after receipt and the current typed recovery result,
        # which carries home_verified separately from the final STOP receipt.
        if recovery.get("success") is not True:
            return False
        if ((recovery.get("home") or {}).get("dashboard_after", {}).get("running")
                == "Program running: false"):
            return True
        stop = dispatch.get("stop") or {}
        dashboard_stop = stop.get("dashboard_stop") or {}
        dashboard = dashboard_stop.get("dashboard") or {}
        return bool(
            recovery.get("home_verified") is True
            and stop.get("home_verified") is True
            and stop.get("observed_stationary") is True
            and stop.get("program_stopped") is True
            and dashboard_stop.get("program_stopped") is True
            and dashboard.get("running") == "Program running: false"
            and str(dashboard.get("programState", "")).startswith("STOPPED")
            and dashboard.get("safetymode") == "Safetymode: NORMAL"
            and stop.get("safety_mode") == 1
        )
    stop = dispatch.get("stop") or {}
    return stop.get("home_verified") is True and stop.get("program_stopped") is True


def _campaign_sessions(campaign_dir: Path) -> list[Path]:
    # Recovery owns sibling directories such as session-01-autonomous-home.
    # They are evidence for one session, not additional candidate sessions.
    return sorted(
        path for path in campaign_dir.iterdir()
        if path.is_dir() and re.fullmatch(r"session-[0-9]+", path.name)
    )


def score_campaign(campaign_dir: Path) -> dict[str, Any]:
    """Account for every sealed attempt without promoting partial metrics."""

    campaign_dir = Path(campaign_dir).expanduser().resolve()
    schedule = json.loads((campaign_dir / "screening-schedule.json").read_text())
    if schedule.get("schema") != "tase.integral-screening-schedule-v1":
        raise ValueError("screening schedule schema differs")
    planned = schedule["rows"]
    if len(planned) != 25:
        raise ValueError("screening schedule must contain 25 cells")
    if any(row.get("protocol_id") != schedule.get("protocol_id")
           or row.get("duration_token") != schedule.get("duration_token") for row in planned):
        raise ValueError("screening schedule rows differ from their frozen protocol identity")
    minimum_rate = 460.0 if schedule["protocol_id"] == "figure8_window60_r013_rate460_v1" else 400.0
    observed: list[tuple[Path, dict[str, Any]]] = []
    for session in _campaign_sessions(campaign_dir):
        for path in sorted(session.glob("attempts/*/attempt-result.json")):
            observed.append((path, json.loads(path.read_text(encoding="utf-8"))))
    if len(observed) > len(planned):
        raise ValueError("screening has more physical attempts than scheduled cells")
    session_identities: dict[Path, dict[str, Any] | None] = {}
    for session in _campaign_sessions(campaign_dir):
        receipt_path = session / "dispatch_receipt.json"
        if not receipt_path.is_file():
            session_identities[session] = None
            continue
        dispatch = json.loads(receipt_path.read_text(encoding="utf-8"))
        triplet = dispatch.get("controller_readback_triplet")
        valid_triplet = (
            isinstance(triplet, dict)
            and set(triplet) == {"script", "txt", "urp"}
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in triplet.values())
        )
        live_path = dispatch.get("live_path") or {}
        controller_identity = dispatch.get("controller_identity") or {}
        session_identities[session] = {
            "method": dispatch.get("method"),
            "controller_method": controller_identity.get("method"),
            "protocol_id": live_path.get("protocol_id"),
            "path_duration_s": live_path.get("path_duration_s"),
            "triplet": dict(triplet) if valid_triplet else None,
        }
    configured_triplet = schedule.get("expected_controller_readback_triplet_sha256")
    reference_triplet = configured_triplet or next(
        (identity["triplet"] for identity in session_identities.values()
         if identity is not None and identity["triplet"] is not None), None
    )
    attempts: list[dict[str, Any]] = []
    for index, (path, item) in enumerate(observed):
        row = planned[index]
        if item.get("physical_dispatched") is not True:
            raise ValueError(f"screening result lacks physical ARM boundary at ordinal {index}")
        binding = item.get("parameter_binding") or (item.get("state") or {}).get("parameter_binding") or {}
        if binding.get("candidate_id") != row["candidate_id"]:
            raise ValueError(f"screening candidate identity differs at ordinal {index}")
        applied = item.get("applied_runtime_parameters")
        if item.get("partial") is not True and not item.get("failed_after_collector"):
            expected = {
                "Md_scalar": row["Md_scalar"],
                "Bd_scalar": row["Bd_scalar"],
                "force_integral_limit_n_s": row["frozen"]["force_integral_limit_n_s"],
                "force_integral_policy": row["frozen"]["force_integral_policy"],
                "force_integral_authority_error_n": row["frozen"]["force_integral_authority_error_n"],
            }
            if not isinstance(applied, dict) or any(applied.get(key) != value for key, value in expected.items()):
                raise ValueError(f"screening runtime parameters differ at ordinal {index}")
        metrics = (item.get("evidence") or {}).get("metrics") or {}
        timing = metrics.get("timing_evidence") or {}
        sealed = path.with_name("seal.json").is_file()
        session_identity = session_identities.get(path.parents[2])
        session_triplet = None if session_identity is None else session_identity["triplet"]
        session_identity_pass = bool(
            session_identity is not None
            and session_identity.get("method") == schedule["method"]
            and session_identity.get("controller_method") == schedule["method"]
            and session_identity.get("protocol_id") == row["protocol_id"]
            and session_identity.get("path_duration_s") == 60.0
        )
        package_identity_pass = (
            session_triplet is not None
            and reference_triplet is not None
            and session_triplet == reference_triplet
        )
        rates = timing.get("layer_rates_hz") or {}
        rate_evidence_pass = bool(
            timing.get("successful") is True
            and timing.get("minimum_rate_hz") == minimum_rate
            and rates
            and all(isinstance(rate, (int, float)) and not isinstance(rate, bool)
                    and math.isfinite(float(rate)) and float(rate) >= minimum_rate
                    for rate in rates.values())
        )
        eligible = bool(
            item.get("evidence_eligible") is True
            and item.get("lifecycle", {}).get("path_complete") is True
            and metrics.get("complete_bins") == 550
            and metrics.get("path_duration_s") == 60.0
            and metrics.get("protocol_id") == row["protocol_id"]
            and metrics.get("timing_gate_passed") is True
            and timing.get("acceptance_protocol_id") == row["protocol_id"]
            and rate_evidence_pass
            and session_identity_pass
            and package_identity_pass
            and item.get("lifecycle", {}).get("home_verified") is True
            and sealed
            and isinstance(metrics.get("normal_force_mae_n"), (int, float))
            and math.isfinite(float(metrics["normal_force_mae_n"]))
        )
        mae = float(metrics["normal_force_mae_n"]) if eligible else None
        failure = None
        if not eligible:
            failure = (
                item.get("failed_after_collector")
                or (item.get("evidence") or {}).get("failure")
                or metrics.get("failure")
            )
            if failure is None and metrics.get("timing_gate_passed") is False:
                rates = timing.get("layer_rates_hz") or {}
            if failure is None and not rate_evidence_pass:
                slow_layers = [
                    f"{name}={float(rate):.1f}<{minimum_rate:.1f}Hz"
                    for name, rate in sorted(rates.items())
                    if isinstance(rate, (int, float)) and not isinstance(rate, bool)
                    and (not math.isfinite(float(rate)) or float(rate) < minimum_rate)
                ]
                if timing.get("minimum_rate_hz") != minimum_rate:
                    slow_layers.insert(0, f"declared_minimum={timing.get('minimum_rate_hz')}!=${minimum_rate:.1f}Hz".replace("$", ""))
                if timing.get("successful") is not True:
                    slow_layers.insert(0, "timing_receipt_not_successful")
                failure = "rate_evidence_mismatch:" + ",".join(slow_layers or ["unknown"])
            if failure is None and not package_identity_pass:
                failure = "controller_readback_triplet_missing_or_mismatched"
            if failure is None and not session_identity_pass:
                failure = "controller_method_or_protocol_identity_mismatch"
            if failure is None:
                failure = "evidence_ineligible"
        attempts.append({
            "ordinal": index, "arm_id": row["arm_id"],
            "candidate_id": row["candidate_id"],
            "run_dir": str(path.parents[2]),
            "status": "complete" if eligible else "failed",
            "mae_n": mae,
            "diagnostic_mae_n": metrics.get("normal_force_mae_n"),
            "path_duration_s": metrics.get("path_duration_s"),
            "complete_bins": metrics.get("complete_bins"),
            "controller_readback_triplet_sha256": session_triplet,
            "controller_package_identity_pass": package_identity_pass,
            "controller_session_identity_pass": session_identity_pass,
            "home_verified": item.get("lifecycle", {}).get("home_verified") is True,
            "sealed": sealed,
            "failure": failure,
        })
    arms: dict[str, dict[str, Any]] = {}
    for arm_id in "ABCDE":
        rows = [row for row in attempts if row["arm_id"] == arm_id]
        values = [row["mae_n"] for row in rows if row["status"] == "complete"]
        arms[arm_id] = {
            "attempts": len(rows), "complete": len(values),
            "failed": len(rows) - len(values),
            "mean_mae_n": None if not values else statistics.fmean(values),
        }
    complete_budget = len(attempts) == 25
    sessions = _campaign_sessions(campaign_dir)
    sessions_closed = bool(sessions) and all(_session_verified_closed(path) for path in sessions)
    winner = None
    if (complete_budget and sessions_closed
        and any(arms[arm_id]["complete"] for arm_id in "ABCDE")):
        winner = min(
            "ABCDE",
            key=lambda arm_id: (
                -arms[arm_id]["complete"],
                float("inf") if arms[arm_id]["mean_mae_n"] is None else arms[arm_id]["mean_mae_n"],
                "ABCDE".index(arm_id),
            ),
        )
    result = {
        "schema": "tase.integral-screening-results-v1",
        "protocol_id": schedule["protocol_id"],
        "planned": 25, "attempted": len(attempts),
        "status": ("complete" if complete_budget and sessions_closed
                   else "awaiting_verified_close" if complete_budget else "incomplete"),
        "sessions_closed": sessions_closed,
        "expected_controller_readback_triplet_sha256": reference_triplet,
        "controller_identity_valid": bool(
            session_identities
            and all(identity is not None for identity in session_identities.values())
            and all(row["controller_package_identity_pass"]
                    and row["controller_session_identity_pass"] for row in attempts)
        ),
        "attempts": attempts, "arms": arms, "selected_arm": winner,
        "selection_rule": "complete-count desc, successful MAE mean asc, A/B/C/D/E tie",
        "claim_scope": "screening selection only; no paired improvement claim",
    }
    (campaign_dir / "screening-results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return result


def execute_campaign(campaign_dir: Path, *, resume: bool = False) -> dict[str, Any]:
    """Dispatch the remaining cells through the one Figure-eight live owner."""

    campaign_dir = Path(campaign_dir).expanduser().resolve()
    result = score_campaign(campaign_dir)
    sessions = _campaign_sessions(campaign_dir)
    if sessions and not resume:
        raise ValueError("existing screening session requires explicit resume")
    if sessions:
        previous = sessions[-1]
        dispatch_path = previous / "dispatch_receipt.json"
        if dispatch_path.is_file():
            dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
            sealed_count = len(list(previous.glob("attempts/*/attempt-result.json")))
            if len(dispatch.get("attempts") or []) != sealed_count:
                raise RuntimeError("previous screening session has an unaccounted physical attempt")
        if not _session_verified_closed(previous):
            raise RuntimeError("previous screening session lacks verified joint Home and STOPPED")
    if result["attempted"] == 25:
        return result
    root_manifest = json.loads((campaign_dir / "resident-parameter-manifest.json").read_text())
    remaining = root_manifest["parameter_files"][result["attempted"]:]
    number = len(sessions) + 1
    session_dir = campaign_dir / f"session-{number:02d}"
    session_manifest = campaign_dir / f"session-{number:02d}-manifest.json"
    session_manifest.write_text(json.dumps({
        "schema": "tase.resident-parameter-manifest-v1",
        "protocol_id": root_manifest["protocol_id"],
        "duration_token": root_manifest["duration_token"],
        "parameter_files": remaining,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    command = [
        str(ROOT / "scripts/figure8.sh"),
        "--method", "TASE_RNN_MATURE",
        "--duration", root_manifest["duration_token"],
        "--control-cpu", "4",
        "--video-policy", "evidence-only",
        "--resident-attempts", str(len(remaining)),
        "--resident-parameter-manifest", str(session_manifest),
        "--run-dir", str(session_dir),
    ]
    completed = subprocess.run(command, cwd=str(ROOT), check=False)
    result = score_campaign(campaign_dir)
    result["last_session_returncode"] = completed.returncode
    result["last_session_dir"] = str(session_dir)
    (campaign_dir / "screening-results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--prepare-campaign", type=Path)
    action.add_argument("--score-campaign", type=Path)
    action.add_argument("--execute-campaign", type=Path)
    action.add_argument("--resume-campaign", type=Path)
    args = parser.parse_args()
    if args.prepare_campaign:
        print(prepare_campaign(args.prepare_campaign, args.config))
    elif args.score_campaign:
        print(json.dumps(score_campaign(args.score_campaign), sort_keys=True))
    elif args.execute_campaign or args.resume_campaign:
        print(json.dumps(execute_campaign(
            args.execute_campaign or args.resume_campaign,
            resume=bool(args.resume_campaign),
        ), sort_keys=True))
    else:
        print(write_schedule(args.output, args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
