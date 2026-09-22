"""Independent five-pair rate400 confirmation through the resident Figure-eight owner."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
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
        raise ValueError("independent confirmation requires the sealed 24-unit rate400 tuner")
    return source["best"]


def _parameter(arm: str, block: int, session_number: int,
               config: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
    if arm == "A":
        md = config["incumbent"]["Md_scalar"]
        bd = config["incumbent"]["Bd_scalar"]
        integral_limit = 1.0
    elif arm == "B":
        md = float(best["Md_scalar"])
        bd = float(best["Bd_scalar"])
        integral_limit = 0.5
    else:
        raise ValueError("confirmation arm must be A or B")
    return {
        "schema": "tase.outer-parameters-v1",
        "candidate_id": f"confirm-s{session_number:02d}-b{block:02d}-{arm}",
        "stage": "paired_confirmation",
        "index": block * 2 + (0 if arm == "A" else 1),
        "Md_scalar": md, "Bd_scalar": bd,
        "protocol_id": PROTOCOL, "duration_token": "r013_60_rate400",
        "path_duration_s": 60.0,
        "frozen": {
            "kp": 4.0, "ko": 5.0, "kf": 1.0,
            "force_target_n": 5.0,
            "force_integral_policy": "legacy-clamp-v1",
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
        "baseline": "incumbent Md/Bd + A (1.0 N s)",
        "challenger": "frozen best Md/Bd + B (0.5 N s)",
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


def run_once(config_path: Path, tuning_dir: Path, *, resume: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    tuning_dir = Path(tuning_dir).expanduser().resolve()
    best = _tuning_best(tuning_dir)
    confirmation_dir = tuning_dir / "confirmation-rate400-b-v1"
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
    prior = score(confirmation_dir) if list(_campaign_sessions(confirmation_dir)) else None
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
        "protocol_id": PROTOCOL, "duration_token": "r013_60_rate400",
        "parameter_files": parameter_files,
    }, indent=2, sort_keys=True) + "\n")
    command = [str(ROOT / "scripts/figure8.sh"),
               "--method", "TASE_RNN_MATURE", "--duration", "r013_60_rate400",
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
