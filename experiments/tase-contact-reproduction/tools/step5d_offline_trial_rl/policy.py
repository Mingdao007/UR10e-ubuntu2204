from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from statistics import NormalDist
from typing import Any, Iterable, Mapping

import numpy as np

from .canonical import canonical_sha256


ACTION_KEYS = ("force_p_gain", "force_i_gain", "force_damping", "orientation_ko")
FIXED_CONFIGURATION_FALLBACK = {
    "force_p_gain": 0.001,
    "force_i_gain": 0.00001,
    "force_damping": 7.0,
    "orientation_ko": 0.4,
}


@dataclass(frozen=True)
class PolicyConfig:
    seed: int = 20260720
    ensemble_members: int = 32
    ridge_alpha: float = 1e-3
    reward_lcb_level: float = 0.90
    constraint_confidence: float = 0.95
    constraint_upper_limit: float = 0.20
    minimum_oof_coverage: float = 0.80
    minimum_groups: int = 2
    absolute_minimum_records: int = 10


def _action_vector(record: Mapping[str, Any]) -> np.ndarray:
    values = [record["action"][key] for key in ACTION_KEYS]
    if any(value is None or float(value) <= 0 for value in values):
        raise ValueError("policy action must be complete and strictly positive")
    return np.log2(np.asarray(values, dtype=float))


def _design(records: Iterable[Mapping[str, Any]]) -> np.ndarray:
    rows = np.vstack([_action_vector(row) for row in records])
    return np.column_stack([np.ones(len(rows), dtype=float), rows])


def _ridge_fit(design: np.ndarray, targets: np.ndarray, alpha: float) -> np.ndarray:
    penalty = np.eye(design.shape[1], dtype=float) * alpha
    penalty[0, 0] = 0.0
    return np.linalg.solve(design.T @ design + penalty, design.T @ targets)


def _ensemble_predictions(
    train_records: list[Mapping[str, Any]],
    candidates: list[Mapping[str, Any]],
    config: PolicyConfig,
    seed_offset: int = 0,
) -> np.ndarray:
    design = _design(train_records)
    targets = np.asarray([float(row["reward"]) for row in train_records], dtype=float)
    candidate_design = _design(candidates)
    rng = np.random.default_rng(config.seed + seed_offset)
    predictions = []
    for _ in range(config.ensemble_members):
        sample = rng.integers(0, len(train_records), size=len(train_records))
        coefficients = _ridge_fit(design[sample], targets[sample], config.ridge_alpha)
        predictions.append(candidate_design @ coefficients)
    return np.asarray(predictions)


def wilson_upper(unsafe: int, total: int, confidence: float = 0.95) -> float:
    if total <= 0:
        return 1.0
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    proportion = unsafe / total
    denominator = 1.0 + z * z / total
    center = proportion + z * z / (2.0 * total)
    spread = z * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total))
    return (center + spread) / denominator


def _unique_candidates(records: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in records:
        key = canonical_sha256(row["action"])
        result.setdefault(key, row)
    return [result[key] for key in sorted(result)]


def _oof_coverage(records: list[Mapping[str, Any]], config: PolicyConfig) -> tuple[float, int]:
    groups = sorted({str(row["split_group"]) for row in records})
    covered = 0
    predicted = 0
    for index, group in enumerate(groups):
        train = [row for row in records if row["split_group"] != group]
        held_out = [row for row in records if row["split_group"] == group]
        if len(train) < 2:
            continue
        predictions = _ensemble_predictions(train, held_out, config, seed_offset=1000 + index)
        low = np.quantile(predictions, 0.05, axis=0)
        high = np.quantile(predictions, 0.95, axis=0)
        targets = np.asarray([float(row["reward"]) for row in held_out])
        covered += int(np.sum((targets >= low) & (targets <= high)))
        predicted += len(held_out)
    return (covered / predicted if predicted else 0.0), predicted


def build_offline_proposal(dataset: Mapping[str, Any], config: PolicyConfig = PolicyConfig()) -> dict[str, Any]:
    records = list(dataset["records"])
    reward_records = [row for row in records if row["reward_eligible"] and row["action_complete"]]
    constraint_records = [row for row in records if row["constraint_eligible"] and row["action_complete"]]
    groups = sorted({row["split_group"] for row in reward_records})
    design_rank = int(np.linalg.matrix_rank(_design(reward_records))) if reward_records else 0
    minimum_records = max(config.absolute_minimum_records, 2 * (design_rank + 1))
    unsafe_count = sum(row["constraint_unsafe"] is True for row in constraint_records)
    constraint_upper = wilson_upper(unsafe_count, len(constraint_records), config.constraint_confidence)
    gates = {
        "action_complete_reward_records": len(reward_records),
        "constraint_records": len(constraint_records),
        "split_groups": len(groups),
        "design_rank": design_rank,
        "minimum_records_required": minimum_records,
        "minimum_groups_required": config.minimum_groups,
        "constraint_unsafe_count": unsafe_count,
        "constraint_wilson_upper_95": constraint_upper,
        "constraint_upper_limit": config.constraint_upper_limit,
    }
    blockers: list[str] = []
    if len(reward_records) < minimum_records:
        blockers.append("insufficient_reward_eligible_records")
    if len(groups) < config.minimum_groups:
        blockers.append("insufficient_grouped_campaign_or_plant_epochs")
    if len(constraint_records) < minimum_records:
        blockers.append("insufficient_constraint_eligible_records")
    if constraint_upper > config.constraint_upper_limit:
        blockers.append("constraint_wilson_upper_exceeds_limit")

    validation: dict[str, Any] = {
        "split_definition": "leave-one-split_group-out",
        "oof_prediction_count": 0,
        "reward_interval_coverage_90": None,
        "minimum_coverage": config.minimum_oof_coverage,
    }
    proposal: dict[str, Any] | None = None
    ranking: list[dict[str, Any]] = []
    if not blockers:
        coverage, predicted = _oof_coverage(reward_records, config)
        validation.update({"oof_prediction_count": predicted, "reward_interval_coverage_90": coverage})
        if coverage < config.minimum_oof_coverage:
            blockers.append("oof_reward_interval_coverage_below_minimum")
        else:
            candidates = _unique_candidates(reward_records)
            predictions = _ensemble_predictions(reward_records, candidates, config)
            z = NormalDist().inv_cdf(config.reward_lcb_level)
            for index, candidate in enumerate(candidates):
                mean = float(np.mean(predictions[:, index]))
                std = float(np.std(predictions[:, index]))
                ranking.append({
                    "action": dict(candidate["action"]),
                    "predicted_reward_mean": mean,
                    "predicted_reward_std": std,
                    "reward_lcb_90": mean - z * std,
                    "constraint_wilson_upper_95": constraint_upper,
                    "support_distance": 0.0,
                    "support": "exact_observed_certified_action",
                })
            ranking.sort(key=lambda row: (-row["reward_lcb_90"], canonical_sha256(row["action"])))
            proposal = ranking[0]

    model_config = asdict(config)
    model_fingerprint = canonical_sha256({
        "algorithm": "bootstrap-ridge-ensemble-log2-action-v1",
        "dataset_sha256": dataset["dataset_sha256"],
        "config": model_config,
    })
    success = proposal is not None and not blockers
    return {
        "schema": "step5d.offline-trial-rl/offline-policy-proposal-v1",
        "result_status": "proposal_available" if success else "insufficient_valid_trials",
        "promotion_status": "offline_only",
        "usable_as": "next_campaign_warm_start",
        "current_v3_optimizer_eligible": False,
        "warm_start_eligible": success,
        "dataset_manifest_sha256": dataset["dataset_sha256"],
        "algorithm": "deterministic_bootstrap_ridge_ensemble",
        "model_fingerprint": model_fingerprint,
        "config": model_config,
        "gates": gates,
        "validation": validation,
        "blockers": blockers,
        "observed_action_bounds": _action_bounds([row for row in records if row["action_complete"]]),
        "candidate_ranking": ranking,
        "proposal": proposal,
        "fallback": {
            "kind": "configuration_fallback_not_policy",
            "action": FIXED_CONFIGURATION_FALLBACK,
            "current_v3_optimizer_eligible": False,
        },
    }


def _action_bounds(records: list[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    if not records:
        return {}
    return {
        key: {
            "minimum": min(float(row["action"][key]) for row in records),
            "maximum": max(float(row["action"][key]) for row in records),
        }
        for key in ACTION_KEYS
    }
