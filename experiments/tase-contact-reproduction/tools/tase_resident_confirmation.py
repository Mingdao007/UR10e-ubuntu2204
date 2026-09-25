"""Independent five-pair rate460 confirmation through the resident Figure-eight owner."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

import numpy as np
from scipy.stats import t as student_t

from tase_integral_screening import _campaign_sessions, _session_verified_closed
from tase_resident_autotuner import DEFAULT_CONFIG, PROTOCOL, ROOT, load_config


ROUNDS = 5


def _tuning_best(tuning_dir: Path) -> dict[str, Any]:
    source = json.loads((tuning_dir / "summary.json").read_text(encoding="utf-8"))
    if (source.get("schema") != "tase.resident-autotuner-summary-v1"
        or source.get("protocol_id") != PROTOCOL
        or source.get("status") != "complete"
        or source.get("resident_sessions_closed") is not True
        or source.get("attempted") != 24
        or not isinstance(source.get("best"), dict)):
        raise ValueError("independent confirmation requires the sealed 24-unit rate460 tuner")
    return source["best"]


def _parameter(arm: str, block: int, session_number: int,
               config: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
    if arm == "A":
        md = config["incumbent"]["Md_scalar"]
        bd = config["incumbent"]["Bd_scalar"]
        integral_limit = 1.0
        integral_policy = "legacy-clamp-v1"
    elif arm == "B":
        md = float(best["Md_scalar"])
        bd = float(best["Bd_scalar"])
        integral_limit = float(config["force_integral_limit_n_s"])
        integral_policy = str(config["force_integral_policy"])
    else:
        raise ValueError("confirmation arm must be A or B")
    return {
        "schema": "tase.outer-parameters-v1",
        "candidate_id": f"confirm-s{session_number:02d}-b{block:02d}-{arm}",
        "stage": "paired_confirmation",
        "index": block * 2 + (0 if arm == "A" else 1),
        "Md_scalar": md, "Bd_scalar": bd,
        "protocol_id": config["protocol_id"], "duration_token": config["duration_token"],
        "path_duration_s": 60.0,
        "frozen": {
            "kp": 4.0, "ko": 5.0, "kf": 1.0,
            "force_target_n": 5.0,
            "force_integral_policy": integral_policy,
            "force_integral_limit_n_s": integral_limit,
            "force_integral_authority_error_n": 0.5,
            "force_sign_convention": "step5_step6_positive_normal_load",
        },
    }


def _orders(config: dict[str, Any]) -> list[list[str]]:
    rng = np.random.default_rng(int(config["seed"]) + 7001)
    # Five pairs cannot split order perfectly; randomize a balanced 2/3
    # allocation so an unlucky seed cannot put one arm first every round.
    first_arms = np.asarray(["A", "A", "B", "B", "B"])
    rng.shuffle(first_arms)
    return [[first, "B" if first == "A" else "A"] for first in first_arms.tolist()]


def _score_item(path: Path, planned: dict[str, Any]) -> dict[str, Any]:
    item = json.loads(path.read_text(encoding="utf-8"))
    binding = item.get("parameter_binding") or (item.get("state") or {}).get("parameter_binding") or {}
    actual = item.get("applied_runtime_parameters") or {}
    expected = planned["parameter"]
    if (item.get("physical_dispatched") is not True
        or binding.get("candidate_id") != expected["candidate_id"]
        or binding.get("protocol_id") != PROTOCOL):
        raise ValueError("confirmation physical candidate identity differs")
    if item.get("partial") is not True and not item.get("failed_after_collector"):
        for key, value in {
            "Md_scalar": expected["Md_scalar"], "Bd_scalar": expected["Bd_scalar"],
            "force_integral_policy": expected["frozen"]["force_integral_policy"],
            "force_integral_limit_n_s": expected["frozen"]["force_integral_limit_n_s"],
            "force_integral_authority_error_n": 0.5,
        }.items():
            if actual.get(key) != value:
                raise ValueError(f"confirmation actual {key} differs")
    metrics = (item.get("evidence") or {}).get("metrics") or {}
    lifecycle = item.get("lifecycle") or {}
    eligible = bool(
        item.get("evidence_eligible") is True
        and not item.get("failed_after_collector")
        and lifecycle.get("path_complete") is True
        and lifecycle.get("home_verified") is True
        and path.with_name("seal.json").is_file()
        and metrics.get("complete_bins") == 550
        and metrics.get("protocol_id") == PROTOCOL
        and metrics.get("timing_gate_passed") is True
        and (metrics.get("timing_evidence") or {}).get("acceptance_protocol_id") == PROTOCOL
        and isinstance(metrics.get("normal_force_mae_n"), (int, float))
        and math.isfinite(float(metrics["normal_force_mae_n"]))
    )
    return {
        "block": planned["block"], "arm": planned["arm"],
        "pair_attempt_id": planned["pair_attempt_id"],
        "candidate_id": expected["candidate_id"],
        "source_attempt_result": str(path),
        "status": "complete" if eligible else "failed",
        "mae_n": float(metrics["normal_force_mae_n"]) if eligible else None,
        "diagnostic_mae_n": metrics.get("normal_force_mae_n"),
        "complete_bins": metrics.get("complete_bins"),
        "home_verified": lifecycle.get("home_verified") is True,
        "failure": None if eligible else (item.get("evidence") or {}).get("failure")
                   or item.get("failed_after_collector") or metrics.get("failure")
                   or "evidence_ineligible",
    }


def score(confirmation_dir: Path) -> dict[str, Any]:
    confirmation_dir = Path(confirmation_dir).expanduser().resolve()
    frozen = json.loads(
        (confirmation_dir / "comparison-frozen.json").read_text(encoding="utf-8")
    )
    config = frozen["config"]
    results: list[dict[str, Any]] = []
    sessions = _campaign_sessions(confirmation_dir)
    for session in sessions:
        plan = json.loads((confirmation_dir / f"{session.name}-plan.json").read_text())
        expected_rows = plan["rows"]
        observed = sorted(session.glob("attempts/*/attempt-result.json"))
        if len(observed) > len(expected_rows):
            raise ValueError("confirmation has more physical attempts than planned")
        for sequence, path in enumerate(observed):
            results.append(_score_item(path, expected_rows[sequence]))
    valid_pairs: dict[int, dict[str, Any]] = {}
    for block in range(ROUNDS):
        for pair_id in sorted({row["pair_attempt_id"] for row in results if row["block"] == block}):
            pair = [row for row in results if row["pair_attempt_id"] == pair_id]
            if (len(pair) == 2 and {row["arm"] for row in pair} == {"A", "B"}
                and all(row["status"] == "complete" for row in pair)):
                by_arm = {row["arm"]: row for row in pair}
                valid_pairs[block] = {
                    "block": block, "pair_attempt_id": pair_id,
                    "baseline_a_mae_n": by_arm["A"]["mae_n"],
                    "challenger_b_mae_n": by_arm["B"]["mae_n"],
                    "improvement_a_minus_b_n": by_arm["A"]["mae_n"] - by_arm["B"]["mae_n"],
                }
                break
    all_closed = bool(sessions) and all(_session_verified_closed(session) for session in sessions)
    pairs = [valid_pairs[block] for block in sorted(valid_pairs)]
    differences = np.asarray([row["improvement_a_minus_b_n"] for row in pairs], dtype=float)
    mean = float(np.mean(differences)) if len(differences) else None
    ci = None
    if len(differences) >= 2:
        margin = float(student_t.ppf(0.975, len(differences) - 1)
                       * np.std(differences, ddof=1) / math.sqrt(len(differences)))
        ci = [mean - margin, mean + margin]
    complete = len(pairs) == ROUNDS and all_closed
    result = {
        "schema": "tase.resident-confirmation-summary-v1",
        "protocol_id": PROTOCOL,
        "baseline": "incumbent Md/Bd + A (legacy clamp, 1.0 N s)",
        "challenger": (
            f"frozen best Md/Bd + {config.get('selected_screening_arm')} "
            f"({config.get('force_integral_policy')}, "
            f"{config.get('force_integral_limit_n_s')} N s)"
        ),
        "planned_pairs": ROUNDS,
        "valid_pairs": len(pairs),
        "physical_attempts": len(results),
        "failed_attempts": sum(row["status"] == "failed" for row in results),
        "all_sessions_closed": all_closed,
        "status": "complete" if complete else "incomplete",
        "pairs": pairs,
        "attempts": results,
        "mean_improvement_n": mean,
        "paired_ci95_n": ci,
        "improvement_supported": bool(
            complete and mean is not None and mean >= 0.1
            and ci is not None and ci[0] > 0.0
        ),
        "claim_scope": "paired sensor-feedback normal-force MAE only; no independent task-force truth",
    }
    (confirmation_dir / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def _resident_owner_process_alive(session: Path) -> bool:
    """Return true only for the exact recorded live owner of this session."""
    owner_path = session / "owner.json"
    if not owner_path.is_file() or owner_path.is_symlink():
        return False
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    try:
        pid = int(owner["pid"])
        expected_start = int(owner["process_start"])
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        actual_start = int(stat[stat.rfind(")") + 2 :].split()[19])
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, KeyError, TypeError, ValueError, IndexError):
        return False
    if actual_start != expected_start:
        return False
    run_dir = str(session.resolve()).encode()
    expected_entries = (b"contact_yield_live.py", b"contact_yield_supervisor.py")
    return run_dir in argv and any(any(entry in arg for entry in expected_entries) for arg in argv)


def _verified_orphan_recovery(session: Path) -> tuple[Path, dict[str, Any]] | None:
    """Find fresh persisted evidence that an interrupted owner closed at Home."""
    for result_path in sorted(
        session.glob("recovery-*/result.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        source = Path(str(result.get("source_attempt", "")))
        if not source.is_absolute():
            source = ROOT / source
        if source.resolve() != session.resolve():
            continue
        home = result.get("home") or {}
        dashboard = home.get("dashboard_after") or {}
        final_q = home.get("final_q")
        joint_error = home.get("final_joint_max_error_rad")
        if (
            result.get("success") is True
            and result.get("state") == "HOME_RECOVERED"
            and result.get("home_commandable") is True
            and result.get("home_attempted") is True
            and result.get("home_motion_dispatched") is True
            and result.get("home_blocked") is False
            and home.get("success") is True
            and isinstance(final_q, list)
            and len(final_q) == 6
            and isinstance(joint_error, (int, float))
            and math.isfinite(float(joint_error))
            and dashboard.get("running") == "Program running: false"
            and str(dashboard.get("programState", "")).startswith("STOPPED")
            and dashboard.get("safetymode") == "Safetymode: NORMAL"
            and dashboard.get("is in remote control") == "true"
        ):
            return result_path, result
    return None


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def _reconcile_recovered_orphan(session: Path, confirmation_dir: Path) -> bool:
    """Seal only a dead resident owner whose persisted recovery proves safe Home.

    Any ARM visible in the session RTDE stream but missing a sealed result is
    retained as a failed partial attempt. No incomplete PATH is promoted to a
    metric or optimizer observation.
    """
    if (session / "dispatch_receipt.json").exists():
        return False
    if _resident_owner_process_alive(session):
        return False
    recovery_found = _verified_orphan_recovery(session)
    if recovery_found is None:
        return False
    recovery_path, recovery = recovery_found
    plan_path = confirmation_dir / f"{session.name}-plan.json"
    if not plan_path.is_file():
        return False
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    planned_rows = plan.get("rows")
    if not isinstance(planned_rows, list):
        return False

    result_paths = sorted(session.glob("attempts/*/attempt-result.json"))
    existing_sequences = []
    for path in result_paths:
        try:
            existing_sequences.append(int(path.parent.name))
        except ValueError:
            return False
    if existing_sequences != list(range(1, len(existing_sequences) + 1)):
        return False

    observed_by_ordinal: dict[int, dict[str, Any]] = {}
    trace_path = session / "supervisor-rtde.jsonl"
    if trace_path.is_file():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            try:
                frame = json.loads(line)
                ordinal = int(frame.get("output_int_register_25"))
                state = int(frame.get("output_int_register_26"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            # The stale pre-ARM image can contain the previous session's
            # ordinal in STOP/Home state. Only active attempt states prove a
            # new physical ARM in this session.
            if ordinal > 0 and state in {11, 20, 21, 25, 40}:
                observed_by_ordinal[ordinal] = frame

    highest_observed = max(observed_by_ordinal, default=len(existing_sequences))
    if highest_observed > len(planned_rows):
        return False
    partial_results: list[Path] = []
    for ordinal in range(len(existing_sequences) + 1, highest_observed + 1):
        frame = observed_by_ordinal.get(ordinal)
        if frame is None:
            return False
        planned = planned_rows[ordinal - 1]
        parameter = planned.get("parameter") or {}
        if parameter.get("protocol_id") != PROTOCOL or not parameter.get("candidate_id"):
            return False
        folder = session / "attempts" / f"{ordinal:04d}"
        result_path = folder / "attempt-result.json"
        if result_path.exists():
            return False
        state = int(frame["output_int_register_26"])
        result = {
            "schema": "tase.resident-confirmation-partial-attempt-v1",
            "physical_dispatched": True,
            "partial": True,
            "evidence_eligible": False,
            "parameter_binding": {
                "candidate_id": parameter["candidate_id"],
                "protocol_id": parameter["protocol_id"],
                "source": parameter.get("source"),
                "identity_source": "frozen session plan; runtime application not verified",
            },
            "applied_runtime_parameters": None,
            "pair_attempt_id": planned.get("pair_attempt_id"),
            "arm": planned.get("arm"),
            "failure": "resident owner exited before this attempt was sealed",
            "last_tp_observation": {
                "attempt_ordinal": ordinal,
                "tp_state": state,
                "controller_timestamp": frame.get("timestamp"),
                "observed_at_s": frame.get("observed_at_s"),
                "actual_TCP_pose": frame.get("actual_TCP_pose"),
                "safety_mode": frame.get("safety_mode"),
                "robot_mode": frame.get("robot_mode"),
            },
            "recovery_receipt": str(recovery_path),
            "lifecycle": {
                "path_complete": False,
                "home_verified": False,
                "program_stopped": False,
                "returned_to_home_by_session_recovery": True,
            },
            "evidence": {
                "failure": "resident_orchestrator_interrupted",
                "metrics": {
                    "protocol_id": PROTOCOL,
                    "complete_bins": 0,
                    "normal_force_mae_n": None,
                    "timing_gate_passed": False,
                },
            },
        }
        _write_json_new(result_path, result)
        partial_results.append(result_path)

    all_results = sorted(session.glob("attempts/*/attempt-result.json"))
    if len(all_results) != len(existing_sequences) + len(partial_results):
        return False
    attempt_rows = []
    for path in all_results:
        item = json.loads(path.read_text(encoding="utf-8"))
        attempt_rows.append({
            "candidate_id": (item.get("parameter_binding") or {}).get("candidate_id"),
            "physical_dispatched": item.get("physical_dispatched") is True,
            "evidence_eligible": item.get("evidence_eligible") is True,
            "source_attempt_result": str(path),
        })
    home = recovery["home"]
    dashboard = home["dashboard_after"]
    stop = {
        "home_verified": True,
        "observed_stationary": True,
        "program_stopped": True,
        "safety_mode": 1,
        "dashboard_stop": {"program_stopped": True, "dashboard": dashboard},
        "source": "verified autonomous recovery after resident owner exit",
    }
    dispatch = {
        "schema": "tase.resident-session-interruption-reconciled-v1",
        "success": False,
        "session_interrupted": True,
        "attempts": attempt_rows,
        "stop": stop,
        "automatic_home_recovery": recovery,
        "recovery_receipt": str(recovery_path),
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_new(session / "dispatch_receipt.json", dispatch)
    supervisor = {
        "success": False,
        "session_interrupted": True,
        "failure": "resident owner process disappeared before normal session closure",
        "stop": stop,
        "autonomous_home_recovery": recovery,
        "recovery_receipt": str(recovery_path),
    }
    _write_json_new(session / "supervisor-result.json", supervisor)

    owner_path = session / "owner.json"
    if owner_path.is_file() and not owner_path.is_symlink():
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        owner.update({
            "active": False,
            "terminal_state": "interrupted_and_recovered_to_home",
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "recovery_receipt": str(recovery_path),
        })
        temporary = owner_path.with_suffix(".reconcile.tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(owner, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(owner_path)
    return True


def run_once(config_path: Path, tuning_dir: Path, *, resume: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    tuning_dir = Path(tuning_dir).expanduser().resolve()
    best = _tuning_best(tuning_dir)
    confirmation_dir = tuning_dir / "confirmation-rate460-e-v1"
    if confirmation_dir.exists() and not resume:
        raise FileExistsError("confirmation exists; use --resume")
    confirmation_dir.mkdir(parents=True, exist_ok=True)
    frozen_path = confirmation_dir / "comparison-frozen.json"
    frozen = {
        "config": config,
        "tuning_best": best,
        "tuning_source": str(tuning_dir / "summary.json"),
    }
    if frozen_path.is_file():
        if json.loads(frozen_path.read_text(encoding="utf-8")) != frozen:
            raise ValueError("confirmation comparison changed across resume")
    else:
        with frozen_path.open("x", encoding="utf-8") as stream:
            json.dump(frozen, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    sessions = _campaign_sessions(confirmation_dir)
    for session in sessions:
        _reconcile_recovered_orphan(session, confirmation_dir)
    prior = score(confirmation_dir) if sessions else None
    if prior and not prior["all_sessions_closed"]:
        raise RuntimeError("previous confirmation owner lacks joint Home and STOPPED")
    if prior and prior["status"] == "complete":
        return prior
    pending = [block for block in range(ROUNDS)
               if prior is None or block not in {pair["block"] for pair in prior["pairs"]}]
    occupied = [int(match.group(1)) for path in confirmation_dir.iterdir()
                if (match := re.fullmatch(
                    r"session-([0-9]+)(?:-plan\.json|-manifest\.json)?", path.name
                ))]
    candidate_dir = confirmation_dir / "candidates"
    if candidate_dir.is_dir():
        occupied.extend(int(match.group(1)) for path in candidate_dir.iterdir()
                        if (match := re.fullmatch(
                            r"session-([0-9]+)-[0-9]+-[AB]\.json", path.name
                        )))
    session_number = max(occupied, default=0) + 1
    session_name = f"session-{session_number:02d}"
    rows = []
    for block in pending:
        pair_id = f"{session_name}-block-{block:02d}"
        for arm in _orders(config)[block]:
            rows.append({"block": block, "arm": arm,
                         "pair_attempt_id": pair_id,
                         "parameter": _parameter(arm, block, session_number, config, best)})
    parameter_files = []
    for sequence, row in enumerate(rows, 1):
        path = confirmation_dir / "candidates" / f"{session_name}-{sequence:02d}-{row['arm']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(row["parameter"], stream, indent=2, sort_keys=True)
            stream.write("\n")
        parameter_files.append(str(path.relative_to(confirmation_dir)))
    plan = confirmation_dir / f"{session_name}-plan.json"
    plan.write_text(json.dumps({"rows": rows}, indent=2, sort_keys=True) + "\n")
    manifest = confirmation_dir / f"{session_name}-manifest.json"
    manifest.write_text(json.dumps({
        "schema": "tase.resident-parameter-manifest-v1",
        "protocol_id": config["protocol_id"],
        "duration_token": config["duration_token"],
        "parameter_files": parameter_files,
    }, indent=2, sort_keys=True) + "\n")
    command = [str(ROOT / "scripts/figure8.sh"),
               "--method", "TASE_RNN_MATURE", "--duration", config["duration_token"],
               "--control-cpu", str(config["control_cpu"]),
               "--video-policy", config["video_policy"],
               "--resident-attempts", str(len(rows)),
               "--resident-parameter-manifest", str(manifest),
               "--run-dir", str(confirmation_dir / session_name)]
    subprocess.run(command, cwd=str(ROOT), check=False)
    return score(confirmation_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--tuning-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_once(args.config, args.tuning_dir, resume=args.resume),
                     sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
