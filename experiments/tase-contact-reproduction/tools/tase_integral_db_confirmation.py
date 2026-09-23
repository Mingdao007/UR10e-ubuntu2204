"""Randomized B-versus-corrected-D confirmation through one resident owner.

The earlier five D screening rows predate the final-output feedback fix. This
small follow-up keeps those rows historical and compares corrected D with the
selected B policy in five fresh, randomized within-block pairs.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import statistics
from typing import Any

import numpy as np
from scipy.stats import t

from tase_integral_screening import (
    DEFAULT_CONFIG,
    ROOT,
    _campaign_sessions,
    _session_verified_closed,
    load_screening_config,
)


PROTOCOL = "figure8_window60_r013_rate400_v1"
PAIR_COUNT = 5
ORDER_SEED = 20260923


def build_schedule(
    config: dict[str, Any] | None = None,
    *,
    seed: int = ORDER_SEED,
) -> dict[str, Any]:
    config = load_screening_config() if config is None else config
    if config["protocol_id"] != PROTOCOL or config["duration_token"] != "r013_60_rate400":
        raise ValueError("corrected D/B follow-up requires the 60 s rate400 protocol")
    if float(config["Md_scalar"]) != 9.565272137974492 or float(config["Bd_scalar"]) != 693.6559295653944:
        raise ValueError("corrected D/B follow-up must use the frozen screening Md/Bd")
    if float(config["target_force_n"]) != 5.0:
        raise ValueError("corrected D/B follow-up target force differs")
    arms = {row["arm_id"]: row for row in config["arms"]}
    b, d = arms["B"], arms["D"]
    kf = 1.0
    b_cap = float(b["force_integral_limit_n_s"])
    d_state_cap = float(d["force_integral_limit_n_s"])
    # In this outer loop P=1/Md and I=kf/Md, so P/I=1/kf; the
    # authority-error clamp therefore corresponds to authority_error/kf N*s.
    d_authority_cap = float(d["force_integral_authority_error_n"]) / kf
    d_effective_cap = min(d_state_cap, d_authority_cap)
    if not math.isclose(b_cap, d_effective_cap, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("B and D do not have the same effective integral-state cap")
    if b["force_integral_policy"] != "legacy-clamp-v1" or d["force_integral_policy"] != "conditional-double-clamp-v1":
        raise ValueError("B/D policy identities differ from the approved comparison")

    rng = np.random.default_rng(int(seed))
    first_b_count = int(rng.choice([2, 3]))
    first_b_blocks = set(int(value) for value in rng.choice(PAIR_COUNT, size=first_b_count, replace=False))
    rows: list[dict[str, Any]] = []
    for block in range(PAIR_COUNT):
        order = ["B", "D"] if block in first_b_blocks else ["D", "B"]
        for position, arm_id in enumerate(order):
            arm = arms[arm_id]
            ordinal = len(rows) + 1
            rows.append({
                "ordinal": ordinal,
                "block": block,
                "position": position,
                "arm_id": arm_id,
                "candidate_id": f"corrected-{arm_id}-{block:02d}",
                "protocol_id": PROTOCOL,
                "duration_token": "r013_60_rate400",
                "method": "TASE_RNN_MATURE",
                "Md_scalar": float(config["Md_scalar"]),
                "Bd_scalar": float(config["Bd_scalar"]),
                "target_force_n": 5.0,
                "frozen": {
                    "kp": 4.0,
                    "ko": 5.0,
                    "kf": kf,
                    "force_integral_limit_n_s": float(arm["force_integral_limit_n_s"]),
                    "force_integral_policy": arm["force_integral_policy"],
                    "force_integral_authority_error_n": float(arm["force_integral_authority_error_n"]),
                },
            })
    return {
        "schema": "tase.integral-db-correction-schedule-v1",
        "protocol_id": PROTOCOL,
        "seed": int(seed),
        "pairs": PAIR_COUNT,
        "blocks_starting_with_b": first_b_count,
        "effective_integral_cap_n_s": b_cap,
        "comparison": "B legacy-clamp-v1 versus corrected D conditional-double-clamp-v1",
        "old_d_rows_included": False,
        "rows": rows,
    }


def prepare_campaign(campaign_dir: Path, *, seed: int = ORDER_SEED) -> Path:
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    campaign_dir.mkdir(parents=True, exist_ok=False)
    schedule = build_schedule(seed=seed)
    candidates = campaign_dir / "candidates"
    candidates.mkdir()
    names: list[str] = []
    for row in schedule["rows"]:
        parameter_file = candidates / f'{row["ordinal"]:02d}-{row["candidate_id"]}.json'
        payload = {
            "schema": "tase.outer-parameters-v1",
            "candidate_id": row["candidate_id"],
            "stage": "integral_db_confirmation",
            "index": row["ordinal"] - 1,
            "Md_scalar": row["Md_scalar"],
            "Bd_scalar": row["Bd_scalar"],
            "protocol_id": row["protocol_id"],
            "duration_token": row["duration_token"],
            "path_duration_s": 60.0,
            "frozen": {
                "kp": row["frozen"]["kp"],
                "ko": row["frozen"]["ko"],
                "kf": row["frozen"]["kf"],
                "force_target_n": row["target_force_n"],
                "force_integral_limit_n_s": row["frozen"]["force_integral_limit_n_s"],
                "force_integral_policy": row["frozen"]["force_integral_policy"],
                "force_integral_authority_error_n": row["frozen"]["force_integral_authority_error_n"],
                "force_sign_convention": "step5_step6_positive_normal_load",
            },
        }
        parameter_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        names.append(str(parameter_file.relative_to(campaign_dir)))
    schedule["candidate_files"] = names
    (campaign_dir / "screening-schedule.json").write_text(
        json.dumps(schedule, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (campaign_dir / "resident-parameter-manifest.json").write_text(json.dumps({
        "schema": "tase.resident-parameter-manifest-v1",
        "protocol_id": PROTOCOL,
        "duration_token": "r013_60_rate400",
        "parameter_files": names,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return campaign_dir


def _schedule(campaign_dir: Path) -> dict[str, Any]:
    payload = json.loads((campaign_dir / "screening-schedule.json").read_text(encoding="utf-8"))
    if payload.get("schema") != "tase.integral-db-correction-schedule-v1" or len(payload.get("rows", [])) != 2 * PAIR_COUNT:
        raise ValueError("corrected D/B schedule is absent or invalid")
    return payload


def _score_attempt(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    item = json.loads(path.read_text(encoding="utf-8"))
    binding = item.get("parameter_binding") or (item.get("state") or {}).get("parameter_binding") or {}
    applied = item.get("applied_runtime_parameters") or {}
    if item.get("physical_dispatched") is not True or binding.get("candidate_id") != row["candidate_id"]:
        raise ValueError(f'physical candidate identity differs for {row["candidate_id"]}')
    if (binding.get("protocol_id") != PROTOCOL
        or binding.get("duration_token") != "r013_60_rate400"
        or binding.get("Md_scalar") != row["Md_scalar"]
        or binding.get("Bd_scalar") != row["Bd_scalar"]
        or binding.get("force_integral_policy") != row["frozen"]["force_integral_policy"]
        or binding.get("force_integral_limit_n_s") != row["frozen"]["force_integral_limit_n_s"]):
        raise ValueError(f'applied binding differs for {row["candidate_id"]}')
    metrics = (item.get("evidence") or {}).get("metrics") or {}
    lifecycle = item.get("lifecycle") or {}
    complete = bool(
        item.get("evidence_eligible") is True
        and item.get("failed_after_collector") is None
        and lifecycle.get("path_complete") is True
        and lifecycle.get("home_verified") is True
        and lifecycle.get("ready_for_next") is True
        and metrics.get("complete_bins") == 550
        and isinstance(metrics.get("path_duration_s"), (int, float))
        and math.isfinite(float(metrics["path_duration_s"]))
        and math.isclose(float(metrics["path_duration_s"]), 60.0, rel_tol=0.0, abs_tol=1e-6)
        and metrics.get("formal_window_s") == [5.0, 60.0]
        and metrics.get("protocol_id") == PROTOCOL
        and metrics.get("timing_gate_passed") is True
        and (metrics.get("timing_evidence") or {}).get("acceptance_protocol_id") == PROTOCOL
        and path.with_name("seal.json").is_file()
        and isinstance(metrics.get("normal_force_mae_n"), (int, float))
        and math.isfinite(float(metrics["normal_force_mae_n"]))
    )
    if complete:
        expected_applied = {
            "Md_scalar": row["Md_scalar"],
            "Bd_scalar": row["Bd_scalar"],
            "force_integral_policy": row["frozen"]["force_integral_policy"],
            "force_integral_limit_n_s": row["frozen"]["force_integral_limit_n_s"],
            "force_integral_authority_error_n": row["frozen"]["force_integral_authority_error_n"],
        }
        if any(applied.get(key) != value for key, value in expected_applied.items()):
            raise ValueError(f'runtime did not apply the frozen profile for {row["candidate_id"]}')
    return {
        **{key: row[key] for key in ("ordinal", "block", "position", "arm_id", "candidate_id")},
        "status": "complete" if complete else "failed",
        "mae_n": float(metrics["normal_force_mae_n"]) if complete else None,
        "diagnostic_mae_n": metrics.get("normal_force_mae_n"),
        "path_duration_s": metrics.get("path_duration_s"),
        "complete_bins": metrics.get("complete_bins"),
        "tp_rate_hz": (metrics.get("timing_evidence") or {}).get("layer_rates_hz", {}).get("tp_consumed_packet_echoes"),
        "endpoint_closure_applied": metrics.get("endpoint_closure_applied"),
        "endpoint_closure_deficit_s": metrics.get("endpoint_closure_deficit_s"),
        "terminal_reference_time_s": metrics.get("terminal_reference_time_s"),
        "home_verified": lifecycle.get("home_verified") is True,
        "ready_for_next": lifecycle.get("ready_for_next") is True,
        "failure": None if complete else item.get("failed_after_collector") or metrics.get("failure") or "evidence_ineligible",
        "source_attempt_result": str(path),
    }


def score_campaign(campaign_dir: Path) -> dict[str, Any]:
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    schedule = _schedule(campaign_dir)
    observed = [path for session in _campaign_sessions(campaign_dir)
                for path in sorted(session.glob("attempts/*/attempt-result.json"))]
    if len(observed) > len(schedule["rows"]):
        raise ValueError("corrected D/B campaign has more physical attempts than planned")
    attempts = [_score_attempt(path, schedule["rows"][i]) for i, path in enumerate(observed)]
    paired: list[dict[str, Any]] = []
    for block in range(PAIR_COUNT):
        pair = {row["arm_id"]: row for row in attempts if row["block"] == block}
        if set(pair) != {"B", "D"} or any(pair[arm]["status"] != "complete" for arm in ("B", "D")):
            continue
        paired.append({
            "block": block,
            "B_mae_n": pair["B"]["mae_n"],
            "D_mae_n": pair["D"]["mae_n"],
            "D_improvement_vs_B_n": pair["B"]["mae_n"] - pair["D"]["mae_n"],
        })
    ci = None
    mean = None
    supported = False
    if paired:
        deltas = [row["D_improvement_vs_B_n"] for row in paired]
        mean = statistics.fmean(deltas)
        if len(deltas) >= 2:
            half = float(t.ppf(0.975, len(deltas) - 1)) * statistics.stdev(deltas) / math.sqrt(len(deltas))
            ci = [mean - half, mean + half]
            supported = mean >= 0.10 and ci[0] > 0.0
    sessions = _campaign_sessions(campaign_dir)
    closed = bool(sessions) and all(_session_verified_closed(session) for session in sessions)
    result = {
        "schema": "tase.integral-db-correction-results-v1",
        "protocol_id": PROTOCOL,
        "attempted": len(attempts),
        "planned": len(schedule["rows"]),
        "complete": sum(row["status"] == "complete" for row in attempts),
        "failed": sum(row["status"] != "complete" for row in attempts),
        "pairs": paired,
        "complete_pairs": len(paired),
        "mean_D_improvement_vs_B_n": mean,
        "paired_t_ci95_n": ci,
        "improvement_supported": supported,
        "sessions_closed": closed,
        "status": "complete" if len(attempts) == len(schedule["rows"]) and closed else "incomplete",
        "claim_scope": "corrected D versus selected B follow-up only; no five-arm screening replacement",
        "attempts": attempts,
    }
    (campaign_dir / "screening-results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def execute_campaign(campaign_dir: Path, *, resume: bool = False) -> dict[str, Any]:
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    _schedule(campaign_dir)
    sessions = _campaign_sessions(campaign_dir)
    result = score_campaign(campaign_dir)
    if sessions and not resume:
        raise ValueError("existing corrected D/B session requires explicit resume")
    if any(not _session_verified_closed(session) for session in sessions):
        raise RuntimeError("corrected D/B prior owner lacks verified joint Home and STOPPED")
    remaining_rows = json.loads((campaign_dir / "screening-schedule.json").read_text())["rows"][result["attempted"]:]
    if not remaining_rows:
        return result
    manifest = campaign_dir / "resident-parameter-manifest.json"
    root_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    files = root_manifest["parameter_files"][result["attempted"]:]
    session_number = len(sessions) + 1
    session_dir = campaign_dir / f"session-{session_number:02d}"
    session_manifest = campaign_dir / f"session-{session_number:02d}-manifest.json"
    session_manifest.write_text(json.dumps({
        "schema": "tase.resident-parameter-manifest-v1",
        "protocol_id": PROTOCOL,
        "duration_token": "r013_60_rate400",
        "parameter_files": files,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    command = [
        str(ROOT / "scripts/figure8.sh"), "--method", "TASE_RNN_MATURE",
        "--duration", "r013_60_rate400", "--control-cpu", "4",
        "--video-policy", "evidence-only", "--resident-attempts", str(len(files)),
        "--resident-parameter-manifest", str(session_manifest), "--run-dir", str(session_dir),
    ]
    completed = subprocess.run(command, cwd=str(ROOT), check=False)
    result = score_campaign(campaign_dir)
    result["last_session_returncode"] = completed.returncode
    result["last_session_dir"] = str(session_dir)
    (campaign_dir / "screening-results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", type=Path)
    actions.add_argument("--run", type=Path)
    actions.add_argument("--resume", type=Path)
    actions.add_argument("--score", type=Path)
    parser.add_argument("--seed", type=int, default=ORDER_SEED)
    args = parser.parse_args()
    if args.prepare:
        print(prepare_campaign(args.prepare, seed=args.seed))
    elif args.score:
        print(json.dumps(score_campaign(args.score), indent=2, sort_keys=True))
    else:
        print(json.dumps(execute_campaign(args.run or args.resume, resume=bool(args.resume)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
