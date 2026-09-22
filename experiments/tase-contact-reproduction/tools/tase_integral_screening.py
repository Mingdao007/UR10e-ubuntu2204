"""Prepare, run, and score the five-arm integral screening campaign."""
from __future__ import annotations

import argparse
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


def load_screening_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != "tase.integral-screening-v1":
        raise ValueError("screening schema differs")
    if payload.get("method") != "TASE_RNN_MATURE":
        raise ValueError("screening method must be TASE_RNN_MATURE")
    if (payload.get("protocol_id") != "figure8_window60_r013_rate400_v1"
        or payload.get("duration_token") != "r013_60_rate400"):
        raise ValueError("screening must use the separate 60 s rate400 protocol")
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


def write_schedule(output: Path, config_path: Path = DEFAULT_CONFIG) -> Path:
    config = load_screening_config(config_path)
    rows = build_schedule(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema": "tase.integral-screening-schedule-v1", "rows": rows}, indent=2) + "\n", encoding="utf-8")
    return output


def prepare_campaign(campaign_dir: Path, config_path: Path = DEFAULT_CONFIG) -> Path:
    """Freeze all 25 Home-loaded candidates for one resident session."""

    config = load_screening_config(config_path)
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    campaign_dir.mkdir(parents=True, exist_ok=False)
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
    (campaign_dir / "screening-schedule.json").write_text(
        json.dumps({"schema": "tase.integral-screening-schedule-v1", "rows": rows},
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
        # supersedes an earlier Home/STOP observation.
        return bool(
            recovery.get("success") is True
            and (recovery.get("home") or {}).get("dashboard_after", {}).get("running")
            == "Program running: false"
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
    observed: list[tuple[Path, dict[str, Any]]] = []
    for session in _campaign_sessions(campaign_dir):
        for path in sorted(session.glob("attempts/*/attempt-result.json")):
            observed.append((path, json.loads(path.read_text(encoding="utf-8"))))
    if len(observed) > len(planned):
        raise ValueError("screening has more physical attempts than scheduled cells")
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
        eligible = bool(
            item.get("evidence_eligible") is True
            and item.get("lifecycle", {}).get("path_complete") is True
            and metrics.get("complete_bins") == 550
            and metrics.get("protocol_id") == row["protocol_id"]
            and metrics.get("timing_gate_passed") is True
            and timing.get("acceptance_protocol_id") == row["protocol_id"]
            and item.get("lifecycle", {}).get("home_verified") is True
            and sealed
            and isinstance(metrics.get("normal_force_mae_n"), (int, float))
            and math.isfinite(float(metrics["normal_force_mae_n"]))
        )
        mae = float(metrics["normal_force_mae_n"]) if eligible else None
        attempts.append({
            "ordinal": index, "arm_id": row["arm_id"],
            "candidate_id": row["candidate_id"],
            "run_dir": str(path.parents[2]),
            "status": "complete" if eligible else "failed",
            "mae_n": mae,
            "diagnostic_mae_n": metrics.get("normal_force_mae_n"),
            "path_duration_s": metrics.get("path_duration_s"),
            "complete_bins": metrics.get("complete_bins"),
            "home_verified": item.get("lifecycle", {}).get("home_verified") is True,
            "sealed": sealed,
            "failure": None if eligible else (
                item.get("failed_after_collector")
                or (item.get("evidence") or {}).get("failure")
                or metrics.get("failure") or "evidence_ineligible"
            ),
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
        "protocol_id": "figure8_window60_r013_rate400_v1",
        "planned": 25, "attempted": len(attempts),
        "status": ("complete" if complete_budget and sessions_closed
                   else "awaiting_verified_close" if complete_budget else "incomplete"),
        "sessions_closed": sessions_closed,
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
        "--duration", "r013_60_rate400",
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
