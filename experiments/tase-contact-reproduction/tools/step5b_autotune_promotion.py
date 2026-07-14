#!/usr/bin/env python3
"""Build a fingerprinted Step5b-to-Step5d outer-loop parameter overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from step5b_autotune_contract import (
    EXPERIMENT_ROOT,
    OBJECTIVE_NAME,
    OBJECTIVE_UNIT,
    Candidate,
)


EVALUATION_SCHEMA = "step5b_autotune_evaluation_v3"
PROMOTION_SCHEMA = "step5b_to_step5d_outer_loop_v1"
REPEATABILITY_LIMIT = 0.15
FINGERPRINT_PATHS = (
    "UR_FORCE_FRAME_CONTRACT.md",
    "config/step5b_autotune_loop_v2.json",
    "tools/contact_semantics.py",
    "tools/step5b_autotune_contract.py",
    "tools/step5b_autotune_evaluator.py",
    "tools/step5b_autotune_optimizer.py",
    "tools/step5b_autotune_supervisor.py",
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def read_evaluations(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    evaluations: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt observation JSONL at {path}:{line_number}: {exc}") from exc
        if payload.get("schema_version") != EVALUATION_SCHEMA:
            raise ValueError(f"unsupported observation schema at {path}:{line_number}")
        if payload.get("objective_name") != OBJECTIVE_NAME or payload.get("objective_unit") != OBJECTIVE_UNIT:
            raise ValueError(f"unsupported observation objective at {path}:{line_number}")
        candidate = Candidate(**payload["candidate"])
        candidate.validate(tier2_unlocked=True)
        evaluations.append(payload)
    return evaluations


def candidate_key(payload: dict[str, Any]) -> str:
    return canonical_bytes(payload).decode("utf-8")


def source_fingerprints() -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in FINGERPRINT_PATHS:
        path = EXPERIMENT_ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"promotion fingerprint source missing: {path}")
        result[relative] = sha256_bytes(path.read_bytes())
    return result


def build_promotion_payload(observations_path: Path) -> dict[str, Any]:
    evaluations = read_evaluations(observations_path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for payload in evaluations:
        metrics = payload.get("metrics", {})
        objective = payload.get("objective")
        full_trial = (
            float(metrics.get("stage25_duration_s", 0.0)) >= 55.0
            and float(metrics.get("max_path_progress_s", 0.0)) >= 59.9
        )
        if not payload.get("feasible") or not full_trial or objective is None:
            continue
        objective = float(objective)
        if not math.isfinite(objective):
            continue
        groups.setdefault(candidate_key(payload["candidate"]), []).append(payload)

    summaries_by_key: dict[str, dict[str, Any]] = {}
    candidate_summaries: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        values = [float(item["objective"]) for item in group]
        latest_two = values[-2:]
        relative_delta = None
        if len(latest_two) == 2:
            relative_delta = abs(latest_two[1] - latest_two[0]) / max(1e-9, min(latest_two))
        summary = {
            "candidate": group[-1]["candidate"],
            "feasible_full_trials": len(group),
            "latest_two_force_mae_n": latest_two,
            "latest_two_relative_delta": relative_delta,
            "source_runs": [str(item.get("run_dir", "")) for item in group],
        }
        candidate_summaries.append(summary)
        summaries_by_key[key] = summary

    fingerprints = source_fingerprints()
    base: dict[str, Any] = {
        "schema_version": PROMOTION_SCHEMA,
        "objective_name": OBJECTIVE_NAME,
        "objective_unit": OBJECTIVE_UNIT,
        "target_force_n": 12.0,
        "repeatability_limit": REPEATABILITY_LIMIT,
        "source_observations": str(observations_path.resolve()),
        "source_observations_sha256": sha256_bytes(observations_path.read_bytes()) if observations_path.is_file() else sha256_bytes(b""),
        "source_fingerprints": fingerprints,
        "evaluations_seen": len(evaluations),
        "candidate_summaries": candidate_summaries,
        "live_authorization": False,
        "step5d_auto_apply": False,
    }
    eligible_observations = [item for group in groups.values() for item in group]
    if not eligible_observations:
        return {
            **base,
            "status": "candidate_not_promotable",
            "reason": "no feasible full 60 s force-MAE observation exists",
        }

    incumbent_observation = min(eligible_observations, key=lambda item: float(item["objective"]))
    selected = summaries_by_key[candidate_key(incumbent_observation["candidate"])]
    relative_delta = selected["latest_two_relative_delta"]
    if (
        selected["feasible_full_trials"] < 2
        or relative_delta is None
        or float(relative_delta) > REPEATABILITY_LIMIT
    ):
        return {
            **base,
            "status": "candidate_not_promotable",
            "incumbent_candidate": selected["candidate"],
            "reason": "the 12 N force-MAE incumbent lacks two full trials within 15% repeatability",
        }

    selected = {
        **selected,
        "selection_score_mean_latest_two_force_mae_n": float(
            sum(selected["latest_two_force_mae_n"]) / 2.0
        ),
    }
    candidate = Candidate(**selected["candidate"])
    mapping = {
        "STEP5D_FORCE_P_GAIN": candidate.force_p_gain,
        "STEP5D_FORCE_I_GAIN": candidate.force_i_gain,
        "STEP5D_FORCE_DAMPING": candidate.force_damping,
        "STEP5D_NORMAL_FILTER_ALPHA": candidate.normal_filter_alpha,
    }
    promotion_core = {
        "candidate": candidate.payload(),
        "selection_score_mean_latest_two_force_mae_n": selected[
            "selection_score_mean_latest_two_force_mae_n"
        ],
        "source_runs": selected["source_runs"],
        "step5d_env": mapping,
        "source_observations_sha256": base["source_observations_sha256"],
        "source_fingerprints": fingerprints,
    }
    return {
        **base,
        "status": "promotable",
        "promotion_id": sha256_bytes(canonical_bytes(promotion_core)),
        "selection_rule": "lowest observed 12 N force-MAE incumbent with latest-two repeatability",
        **promotion_core,
    }


def env_text(payload: dict[str, Any]) -> str:
    if payload.get("status") != "promotable":
        raise ValueError("cannot render Step5d env for a non-promotable candidate")
    lines = [
        "# Generated Step5b -> Step5d outer-loop overlay; does not authorize live motion.",
        f"# promotion_id={payload['promotion_id']}",
    ]
    for key, value in payload["step5d_env"].items():
        lines.append(f"export {key}={float(value):.10g}")
    return "\n".join(lines) + "\n"


def write_promotion_artifacts(observations_path: Path, output_dir: Path) -> dict[str, Any]:
    payload = build_promotion_payload(observations_path)
    latest_json = output_dir / "step5b_to_step5d_outer_loop_latest.json"
    if payload.get("status") != "promotable":
        atomic_write(latest_json, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return {**payload, "latest_json": str(latest_json)}

    short_id = str(payload["promotion_id"])[:16]
    immutable_json = output_dir / f"step5b_to_step5d_outer_loop_{short_id}.json"
    immutable_env = output_dir / f"step5b_to_step5d_outer_loop_{short_id}.env"
    payload = {
        **payload,
        "immutable_json": str(immutable_json),
        "immutable_env": str(immutable_env),
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if immutable_json.exists() and immutable_json.read_text(encoding="utf-8") != text:
        raise RuntimeError(f"immutable promotion artifact collision: {immutable_json}")
    if immutable_env.exists() and immutable_env.read_text(encoding="utf-8") != env_text(payload):
        raise RuntimeError(f"immutable promotion env collision: {immutable_env}")
    atomic_write(immutable_json, text)
    atomic_write(immutable_env, env_text(payload))
    atomic_write(latest_json, text)
    return {**payload, "latest_json": str(latest_json)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or args.observations.parent
    payload = write_promotion_artifacts(args.observations, output_dir)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
