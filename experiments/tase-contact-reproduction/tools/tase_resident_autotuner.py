"""Home-gated resident Md/Bd tuning on the accepted rate400 Figure-eight route.

The tuner proposes files and reads sealed results. Only figure8.sh owns the
robot, transport, TP, sensor, and recovery chain.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from contact_yield_live import resident_candidate_can_continue
from tase_autotuner import Candidate, _candidate_for
from tase_integral_screening import _campaign_sessions, _session_verified_closed


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/tase_resident_autotuner_rate400_b_v1.json"
PROTOCOL = "figure8_window60_r013_rate400_v1"
TOTAL = 24


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if (config.get("schema") != "tase.resident-autotuner-v1"
        or config.get("method") != "TASE_RNN_MATURE"
        or config.get("protocol_id") != PROTOCOL
        or config.get("duration_token") != "r013_60_rate400"
        or config.get("formal_window_s") != [5.0, 60.0]
        or config.get("budget") != {"initial": 8, "bo": 12, "repeats": 4}
        or config.get("selected_screening_arm") != "B"
        or config.get("force_integral_policy") != "legacy-clamp-v1"
        or config.get("force_integral_limit_n_s") != 0.5
        or config.get("force_integral_authority_error_n") != 0.5):
        raise ValueError("resident tuner protocol, budget, or selected B policy differs")
    if config.get("center") != {"Md_scalar": 12.0, "Bd_scalar": 550.0}:
        raise ValueError("resident tuner search center differs")
    if config.get("log2_bounds") != {"Md_scalar": [-0.5, 0.5], "Bd_scalar": [-0.5, 0.5]}:
        raise ValueError("resident tuner absolute search box differs")
    if config.get("incumbent") != {"Md_scalar": 9.565272137974492,
                                     "Bd_scalar": 693.6559295653944}:
        raise ValueError("resident tuner incumbent differs")
    for key in ("seed", "bo_pool_size", "control_cpu"):
        if type(config.get(key)) is not int or config[key] < 0:
            raise ValueError(f"resident tuner {key} is invalid")
    if config["bo_pool_size"] < 1 or config.get("video_policy") not in {"required", "evidence-only"}:
        raise ValueError("resident tuner pool or video policy is invalid")
    for key in ("gp_length_scale", "observation_noise_n"):
        value = config.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"resident tuner {key} is invalid")
    return config


def candidate_payload(candidate: Candidate, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "tase.outer-parameters-v1",
        "candidate_id": candidate.candidate_id,
        "stage": candidate.stage,
        "index": candidate.index,
        "Md_scalar": candidate.Md_scalar,
        "Bd_scalar": candidate.Bd_scalar,
        "protocol_id": PROTOCOL,
        "duration_token": "r013_60_rate400",
        "path_duration_s": 60.0,
        "frozen": {
            "kp": 4.0, "ko": 5.0, "kf": 1.0,
            "force_target_n": 5.0,
            "force_integral_limit_n_s": config["force_integral_limit_n_s"],
            "force_integral_policy": config["force_integral_policy"],
            "force_integral_authority_error_n": config["force_integral_authority_error_n"],
            "force_sign_convention": "step5_step6_positive_normal_load",
        },
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"candidate identity already exists: {path}")
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(path)


def _screening_initial(config: dict[str, Any]) -> dict[str, Any]:
    screen = (ROOT / config["screening_campaign"]).resolve()
    result = json.loads((screen / "screening-results.json").read_text(encoding="utf-8"))
    if (result.get("status") != "complete" or result.get("sessions_closed") is not True
        or result.get("protocol_id") != PROTOCOL or result.get("selected_arm") != "B"
        or result.get("attempted") != 25):
        raise ValueError("selected B screening evidence is incomplete")
    matching = [row for row in result["attempts"] if row["candidate_id"] == "screen-B-00"]
    if len(matching) != 1 or matching[0].get("status") != "complete":
        raise ValueError("selected B initial-00 source is not complete")
    source = matching[0]
    session = Path(source["run_dir"])
    # The screening ordinal is global; a resumed session starts its local
    # sequence at 1. Resolve by exact physical candidate identity instead.
    candidates = []
    for path in session.glob("attempts/*/attempt-result.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        binding = payload.get("parameter_binding") or (payload.get("state") or {}).get("parameter_binding") or {}
        if binding.get("candidate_id") == "screen-B-00":
            candidates.append(path)
    if len(candidates) != 1:
        raise ValueError("screening B-00 physical source is missing or ambiguous")
    attempt_path = candidates[0]
    item = json.loads(attempt_path.read_text(encoding="utf-8"))
    binding = item.get("parameter_binding") or {}
    applied = item.get("applied_runtime_parameters") or {}
    if (item.get("evidence_eligible") is not True
        or binding.get("protocol_id") != PROTOCOL
        or binding.get("Md_scalar") != config["incumbent"]["Md_scalar"]
        or binding.get("Bd_scalar") != config["incumbent"]["Bd_scalar"]
        or applied.get("force_integral_policy") != "legacy-clamp-v1"
        or applied.get("force_integral_limit_n_s") != 0.5
        or (item.get("evidence") or {}).get("metrics", {}).get("complete_bins") != 550
        or (item.get("evidence") or {}).get("metrics", {}).get("normal_force_mae_n")
        != source["mae_n"]):
        raise ValueError("screening B-00 runtime does not match tuning initial-00")
    return {
        "ordinal": 0, "candidate_id": "initial-00", "source_candidate_id": "screen-B-00",
        "stage": "initial", "index": 0,
        "Md_scalar": binding["Md_scalar"], "Bd_scalar": binding["Bd_scalar"],
        "status": "complete", "mae_n": source["mae_n"],
        "source_attempt_result": str(attempt_path), "source_screening": str(screen),
        "protocol_id": PROTOCOL, "duration_token": "r013_60_rate400",
        "reused_screening_unit": True,
    }


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if [row.get("ordinal") for row in rows] != list(range(len(rows))):
        raise ValueError("resident tuner ledger ordinals are not contiguous")
    if any(row.get("protocol_id") != PROTOCOL or row.get("status") not in {"complete", "failed"}
           for row in rows):
        raise ValueError("resident tuner ledger contains another protocol or nonterminal row")
    return rows


def _append_ledger(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _score_item(path: Path, candidate: Candidate, ordinal: int,
                config: dict[str, Any]) -> dict[str, Any]:
    item = json.loads(path.read_text(encoding="utf-8"))
    binding = item.get("parameter_binding") or (item.get("state") or {}).get("parameter_binding") or {}
    applied = item.get("applied_runtime_parameters") or {}
    if item.get("physical_dispatched") is not True or binding.get("candidate_id") != candidate.candidate_id:
        raise ValueError("resident tuner physical candidate identity differs")
    expected = candidate_payload(candidate, config)
    if (binding.get("protocol_id") != PROTOCOL
        or binding.get("Md_scalar") != candidate.Md_scalar
        or binding.get("Bd_scalar") != candidate.Bd_scalar
        or binding.get("force_integral_policy") != expected["frozen"]["force_integral_policy"]
        or binding.get("force_integral_limit_n_s") != expected["frozen"]["force_integral_limit_n_s"]):
        raise ValueError("resident tuner requested parameters differ from applied binding")
    if item.get("partial") is not True and not item.get("failed_after_collector"):
        for key, value in {
            "Md_scalar": candidate.Md_scalar, "Bd_scalar": candidate.Bd_scalar,
            "force_integral_policy": config["force_integral_policy"],
            "force_integral_limit_n_s": config["force_integral_limit_n_s"],
            "force_integral_authority_error_n": config["force_integral_authority_error_n"],
        }.items():
            if applied.get(key) != value:
                raise ValueError(f"resident tuner runtime {key} was not applied")
    metrics = (item.get("evidence") or {}).get("metrics") or {}
    eligible = bool(
        item.get("evidence_eligible") is True
        and item.get("failed_after_collector") is None
        and (item.get("lifecycle") or {}).get("path_complete") is True
        and (item.get("lifecycle") or {}).get("home_verified") is True
        and metrics.get("complete_bins") == 550
        and metrics.get("protocol_id") == PROTOCOL
        and metrics.get("timing_gate_passed") is True
        and (metrics.get("timing_evidence") or {}).get("acceptance_protocol_id") == PROTOCOL
        and path.with_name("seal.json").is_file()
        and isinstance(metrics.get("normal_force_mae_n"), (int, float))
        and math.isfinite(float(metrics["normal_force_mae_n"]))
    )
    lifecycle = item.get("lifecycle") or {}
    safe_closure = bool(
        lifecycle.get("path_complete") is True
        and lifecycle.get("home_verified") is True
        and lifecycle.get("ready_for_next") is True
        and path.with_name("seal.json").is_file()
        and metrics.get("complete_bins") == 550
    )
    safe_to_continue = bool(
        safe_closure
        and (
            item.get("evidence_eligible") is True
            or resident_candidate_can_continue(item, research_campaign=True)
        )
    )
    return {
        "ordinal": ordinal, "candidate_id": candidate.candidate_id,
        "stage": candidate.stage, "index": candidate.index,
        "Md_scalar": candidate.Md_scalar, "Bd_scalar": candidate.Bd_scalar,
        "protocol_id": PROTOCOL, "duration_token": "r013_60_rate400",
        "status": "complete" if eligible else "failed",
        "mae_n": float(metrics["normal_force_mae_n"]) if eligible else None,
        "diagnostic_mae_n": metrics.get("normal_force_mae_n"),
        "failure": None if eligible else (item.get("evidence") or {}).get("failure")
                   or item.get("failed_after_collector") or metrics.get("failure")
                   or "evidence_ineligible",
        "complete_bins": metrics.get("complete_bins"),
        "home_verified": lifecycle.get("home_verified") is True,
        "safe_to_continue": safe_to_continue,
        "source_attempt_result": str(path),
    }


def _reconcile_sealed_sessions(campaign_dir: Path, config: dict[str, Any],
                               ledger: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover a sealed physical result if the tuner exited before ledger fsync."""

    observed = [path for session in _campaign_sessions(campaign_dir)
                for path in sorted(session.glob("attempts/*/attempt-result.json"))]
    recorded = rows[1:]
    if len(recorded) > len(observed):
        raise ValueError("resident tuner ledger claims attempts absent from sealed sessions")
    for index, path in enumerate(observed):
        if not path.with_name("seal.json").is_file():
            raise RuntimeError("resident tuner physical result lacks seal")
        ordinal = index + 1
        if index < len(recorded):
            if Path(recorded[index].get("source_attempt_result", "")) != path:
                raise ValueError("resident tuner ledger source path differs from physical order")
            continue
        candidate = _candidate_for(config, rows, ordinal)
        row = _score_item(path, candidate, ordinal, config)
        _append_ledger(ledger, row)
        rows.append(row)
    return rows


def _next_generation(campaign_dir: Path) -> int:
    used = [int(match.group(1)) for path in campaign_dir.iterdir()
            if (match := re.fullmatch(r"session-([0-9]+)(?:-candidates)?", path.name))]
    return max(used, default=0) + 1


def summarize(campaign_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    rows = _read_ledger(campaign_dir / "ledger.jsonl")
    sessions = _campaign_sessions(campaign_dir)
    closed = bool(sessions) and all(_session_verified_closed(session) for session in sessions)
    complete = [row for row in rows if row["status"] == "complete"]
    tuned = [row for row in complete if row["stage"] in {"initial", "bo"}]
    best = min(tuned, key=lambda row: row["mae_n"]) if tuned else None
    result = {
        "schema": "tase.resident-autotuner-summary-v1",
        "protocol_id": PROTOCOL,
        "selected_screening_arm": "B",
        "budget": config["budget"],
        "attempted": len(rows), "complete": len(complete),
        "failed": len(rows) - len(complete),
        "resident_sessions_closed": closed,
        "status": "complete" if len(rows) == TOTAL and closed else "incomplete",
        "best": best,
        "claim_scope": "tuning only; paired confirmation is separate",
    }
    (campaign_dir / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def run_campaign(
    config_path: Path, campaign_dir: Path, *, resume: bool = False,
    max_new_attempts: int | None = None,
) -> dict[str, Any]:
    if max_new_attempts is not None and max_new_attempts < 1:
        raise ValueError("max_new_attempts must be positive")
    config = load_config(config_path)
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    if campaign_dir.exists() and not resume:
        raise FileExistsError("resident tuner campaign exists; use --resume")
    campaign_dir.mkdir(parents=True, exist_ok=True)
    frozen_config = campaign_dir / "config-frozen.json"
    if frozen_config.is_file():
        if json.loads(frozen_config.read_text(encoding="utf-8")) != config:
            raise ValueError("resident tuner config changed across resume")
    else:
        _atomic_json(frozen_config, config)
    ledger = campaign_dir / "ledger.jsonl"
    rows = _read_ledger(ledger)
    if not rows:
        _append_ledger(ledger, _screening_initial(config))
        rows = _read_ledger(ledger)
    if rows[0].get("source_candidate_id") != "screen-B-00":
        raise ValueError("resident tuner initial-00 source differs")
    sessions = _campaign_sessions(campaign_dir)
    if sessions and not all(_session_verified_closed(session) for session in sessions):
        raise RuntimeError("resident tuner previous owner lacks final Home and STOPPED")
    rows = _reconcile_sealed_sessions(campaign_dir, config, ledger, rows)
    if len(rows) >= TOTAL:
        return summarize(campaign_dir, config)
    # A failed launch can leave a candidate directory without ever creating
    # its live session. Preserve it as evidence and use a fresh generation.
    generation = _next_generation(campaign_dir)
    session_dir = campaign_dir / f"session-{generation:02d}"
    candidate_dir = campaign_dir / f"session-{generation:02d}-candidates"
    candidate_dir.mkdir(exist_ok=False)
    start_ordinal = len(rows)
    end_ordinal = min(TOTAL, start_ordinal + max_new_attempts) if max_new_attempts else TOTAL
    first = _candidate_for(config, rows, start_ordinal)
    _atomic_json(candidate_dir / "candidate-0001.json", candidate_payload(first, config))
    command = [
        str(ROOT / "scripts/figure8.sh"), "--method", "TASE_RNN_MATURE",
        "--duration", "r013_60_rate400", "--control-cpu", str(config["control_cpu"]),
        "--video-policy", config["video_policy"],
        "--resident-attempts", str(end_ordinal - start_ordinal),
        "--resident-candidate-dir", str(candidate_dir),
        "--run-dir", str(session_dir),
    ]
    process = subprocess.Popen(command, cwd=str(ROOT))
    ordinal = start_ordinal
    sequence = 1
    try:
        while ordinal < end_ordinal:
            candidate = _candidate_for(config, rows, ordinal)
            path = session_dir / "attempts" / f"{sequence:04d}" / "attempt-result.json"
            while not (path.is_file() and path.with_name("seal.json").is_file()):
                if process.poll() is not None:
                    process.wait()
                    if path.is_file() and path.with_name("seal.json").is_file():
                        break
                    raise RuntimeError(f"resident owner exited without sealed ordinal {ordinal}")
                time.sleep(0.1)
            row = _score_item(path, candidate, ordinal, config)
            _append_ledger(ledger, row)
            rows.append(row)
            ordinal += 1
            sequence += 1
            summarize(campaign_dir, config)
            if ordinal >= end_ordinal or not row["safe_to_continue"] or process.poll() is not None:
                break
            next_candidate = _candidate_for(config, rows, ordinal)
            _atomic_json(
                candidate_dir / f"candidate-{sequence:04d}.json",
                candidate_payload(next_candidate, config),
            )
        process.wait()
    except BaseException:
        # A tuner-side exception must not orphan a resident TP/writer waiting
        # at Home. The existing public stop path owns the joint-Home closure.
        if process.poll() is None:
            subprocess.run([str(ROOT / "scripts/figure8.sh"), "--stop", "--run-dir",
                            str(session_dir)], cwd=str(ROOT), check=False)
            process.wait()
        raise
    summary = summarize(campaign_dir, config)
    summary["last_owner_returncode"] = process.returncode
    (campaign_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-new-attempts", type=int)
    args = parser.parse_args()
    print(json.dumps(run_campaign(args.config, args.campaign_dir, resume=args.resume,
                                  max_new_attempts=args.max_new_attempts),
                     sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
