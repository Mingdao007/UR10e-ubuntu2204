"""Offline, event-aligned scoring for apparatus collision trials.

This protocol is separate from the 60 s no-disturbance TASE objective. It
never issues a robot command or treats apparatus force as a human safety claim.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any, Mapping


PROTOCOL_ID = "tase_joint_ee_apparatus_collision_v1"
TRIAL_SCHEMA = "tase.collision-trial-v1"
RESULT_SCHEMA = "tase.collision-diagnostic-v1"
WINDOWS_S = {"impact": (0.0, 0.5), "swing": (0.5, 5.0), "recovery": (5.0, 10.0)}
FORCE_TARGET_N = 5.0
CONTACT_LOSS_THRESHOLD_N = 1.0
QUALIFIED_RATE_HZ = 460.0


class CollisionTrialError(ValueError):
    """Input is insufficient or inconsistent for an apparatus trial."""


def _sha256_label(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _number(value: Any, name: str, *, nonnegative: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CollisionTrialError(f"{name} must be finite") from exc
    if not math.isfinite(result) or (nonnegative and result < 0.0):
        raise CollisionTrialError(f"{name} must be finite and nonnegative" if nonnegative
                                  else f"{name} must be finite")
    return result


def _integral(samples: list[dict[str, float]], field: str) -> float:
    return sum(
        (right["t_s"] - left["t_s"]) * (left[field] + right[field]) / 2.0
        for left, right in zip(samples, samples[1:])
    )


def _window(samples: list[dict[str, float]], start: float, end: float) -> dict[str, Any]:
    rows = [row for row in samples if start <= row["relative_s"] < end]
    covered = bool(
        len(rows) >= 2
        and rows[0]["relative_s"] <= start + .01
        and rows[-1]["relative_s"] >= end - .01
        and all(right["t_s"] - left["t_s"] <= .02
                for left, right in zip(rows, rows[1:]))
    )
    if not covered:
        return {"samples": len(rows), "covered": False}
    load_slip = [{"t_s": row["t_s"], "value":
                  max(0.0, row["normal_force_n"]) * abs(row["tangent_speed_m_s"])}
                 for row in rows]
    force = [row["normal_force_n"] for row in rows]
    error = [value - FORCE_TARGET_N for value in force]
    return {
        "samples": len(rows), "covered": True,
        "pusher_peak_n": max(abs(row["pusher_force_n"]) for row in rows),
        "pusher_impulse_n_s": _integral(
            [{"t_s": row["t_s"], "value": abs(row["pusher_force_n"])} for row in rows],
            "value",
        ),
        "normal_peak_n": max(force),
        "normal_force_mae_n": sum(abs(value) for value in error) / len(error),
        "normal_force_rmse_n": math.sqrt(sum(value * value for value in error) / len(error)),
        "contact_loss_fraction": sum(value < CONTACT_LOSS_THRESHOLD_N for value in force) / len(force),
        "contact_loss_duration_s": _integral(
            [{"t_s": row["t_s"], "value": float(row["normal_force_n"] < CONTACT_LOSS_THRESHOLD_N)}
             for row in rows], "value"),
        "tangent_slip_m": _integral(
            [{"t_s": row["t_s"], "value": abs(row["tangent_speed_m_s"])}
             for row in rows], "value"),
        "loaded_tangent_slip_n_m": _integral(load_slip, "value"),
        "max_lateral_error_m": max(abs(row["lateral_error_m"]) for row in rows),
        "max_orientation_error_rad": max(abs(row["orientation_error_rad"]) for row in rows),
    }


def score_trial(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Score one offline apparatus record; retain safety outcomes if performance is invalid."""
    if trial.get("schema") != TRIAL_SCHEMA or trial.get("protocol_id") != PROTOCOL_ID:
        raise CollisionTrialError("collision trial identity differs")
    if trial.get("source_kind") != "instrumented_apparatus" or trial.get("human_present") is not False:
        raise CollisionTrialError("apparatus protocol cannot claim human contact")
    if trial.get("contact_site") not in {"link", "tool"}:
        raise CollisionTrialError("contact_site must be link or tool")
    if trial.get("direction") not in {"tangent", "normal_inward"}:
        raise CollisionTrialError("direction identity differs")
    if trial.get("controller_arm") not in {"tase_baseline", "end_effector_only", "joint_only", "dual"}:
        raise CollisionTrialError("controller arm identity differs")
    if not isinstance(trial.get("trial_id"), str) or not trial["trial_id"].strip():
        raise CollisionTrialError("trial_id is required")
    if not isinstance(trial.get("pair_id"), str) or not trial["pair_id"].strip():
        raise CollisionTrialError("pair_id is required")
    apparatus = trial.get("apparatus")
    if (not isinstance(apparatus, Mapping)
            or not _sha256_label(apparatus.get("calibration_sha256"))
            or not isinstance(apparatus.get("waveform_id"), str)
            or not apparatus["waveform_id"].strip()):
        raise CollisionTrialError("apparatus calibration and waveform identity required")
    alignment_s = _number(apparatus.get("clock_alignment_error_s"), "clock alignment", nonnegative=True)
    trigger_threshold_n = _number(apparatus.get("trigger_threshold_n"),
                                  "apparatus trigger threshold", nonnegative=True)
    if trigger_threshold_n <= 0:
        raise CollisionTrialError("apparatus trigger threshold must be positive")
    event_at_s = _number(trial.get("event_at_monotonic_s"), "collision event time")
    raw_samples = trial.get("samples")
    if not isinstance(raw_samples, list) or len(raw_samples) < 2:
        raise CollisionTrialError("at least two synchronized samples required")
    samples = []
    previous = -math.inf
    for index, row in enumerate(raw_samples):
        if not isinstance(row, Mapping):
            raise CollisionTrialError(f"sample {index} must be a mapping")
        stamp = _number(row.get("monotonic_s"), "sample timestamp")
        if stamp <= previous:
            raise CollisionTrialError("sample timestamps must increase")
        previous = stamp
        parsed = {"t_s": stamp, "relative_s": stamp - event_at_s}
        for key in ("pusher_force_n", "normal_force_n", "tangent_speed_m_s",
                    "lateral_error_m", "orientation_error_rad"):
            parsed[key] = _number(row.get(key), key)
        samples.append(parsed)
    if not samples[0]["t_s"] <= event_at_s <= samples[-1]["t_s"]:
        raise CollisionTrialError("collision event is outside samples")

    windows = {name: _window(samples, *bounds) for name, bounds in WINDOWS_S.items()}
    cadence = trial.get("cadence") or {}
    if not isinstance(cadence, Mapping):
        raise CollisionTrialError("cadence must be a mapping")
    observed_rates = {}
    for key in ("rtde_path_hz", "tp_path_hz", "rtde_event_hz", "tp_event_hz"):
        value = cadence.get(key)
        observed_rates[key] = None if value is None else _number(value, key, nonnegative=True)
    rate_valid = all(value is not None and value >= QUALIFIED_RATE_HZ
                     for value in observed_rates.values())
    lifecycle = trial.get("lifecycle") or {}
    if not isinstance(lifecycle, Mapping):
        raise CollisionTrialError("lifecycle must be a mapping")
    for key in ("single_writer", "home_verified", "guard_trip", "recovery_exhausted"):
        if type(lifecycle.get(key)) is not bool:
            raise CollisionTrialError(f"lifecycle {key} must be bool")
    def duration_from_event(field: str) -> float | None:
        stamp = lifecycle.get(field)
        if stamp is None:
            return None
        value = _number(stamp, field)
        if value < event_at_s:
            raise CollisionTrialError(f"{field} precedes collision event")
        return value - event_at_s
    task_recovery_s = duration_from_event("task_restored_at_monotonic_s")
    home_recovery_s = duration_from_event("home_verified_at_monotonic_s")
    image = trial.get("mark_evidence") or {}
    if not isinstance(image, Mapping):
        raise CollisionTrialError("mark_evidence must be a mapping")
    admission = trial.get("corridor_admission") or {}
    if not isinstance(admission, Mapping):
        raise CollisionTrialError("corridor_admission must be a mapping")
    mask_matches = bool(image.get("corridor_mask_sha256_basis") == "raw_file_bytes"
                        and image.get("corridor_mask_sha256")
                        and image.get("corridor_mask_sha256") == admission.get("mask_sha256"))
    mark_qualified = bool(
        image.get("schema_version") == "contact-board-marks.v1"
        and image.get("qualified") is True
        and isinstance(image.get("registration"), Mapping)
        and image["registration"].get("qualified") is True
        and isinstance(image.get("detectability"), Mapping)
        and image["detectability"].get("qualified") is True
    )
    if mark_qualified:
        area = _number(image.get("outside_corridor_area_px2"),
                       "outside_corridor_area_px2", nonnegative=True)
        distance = image.get("max_outside_distance_px")
        if distance is None and area != 0:
            raise CollisionTrialError("visible outside mark requires maximum distance")
        if distance is not None:
            _number(distance, "max_outside_distance_px", nonnegative=True)
    trial_started_at_s = _number(trial.get("trial_started_at_s"), "trial wall start")
    admitted_at_s = admission.get("admitted_at_s")
    corridor_qualified = bool(
        admission.get("schema") == "tase.board-corridor-admission-v1"
        and isinstance(admission.get("unperturbed_runs"), int)
        and not isinstance(admission.get("unperturbed_runs"), bool)
        and admission["unperturbed_runs"] >= 10
        and admission.get("detectable") is True
        and admission.get("frozen_before_candidate") is True
        and _sha256_label(admission.get("mask_sha256"))
        and isinstance(admitted_at_s, (int, float))
        and not isinstance(admitted_at_s, bool)
        and math.isfinite(admitted_at_s)
        and admitted_at_s < trial_started_at_s
    )
    reasons = []
    if alignment_s > .002:
        reasons.append("clock_alignment_over_2ms")
    if not all(window["covered"] for window in windows.values()):
        reasons.append("event_window_incomplete")
    if (windows["impact"].get("pusher_peak_n") is None
            or windows["impact"]["pusher_peak_n"] < trigger_threshold_n):
        reasons.append("perturbation_not_observed")
    if not rate_valid:
        reasons.append("rate460_not_observed")
    if not lifecycle["single_writer"]:
        reasons.append("writer_identity_unverified")
    if not lifecycle["home_verified"] or lifecycle["recovery_exhausted"]:
        reasons.append("home_recovery_unverified")
    if lifecycle["guard_trip"]:
        reasons.append("guard_trip")
    if not mark_qualified:
        reasons.append("surface_mark_not_detectable")
    if not mask_matches:
        reasons.append("corridor_mask_identity_differs")
    if not corridor_qualified:
        reasons.append("corridor_baseline_unqualified")
    return {
        "schema": RESULT_SCHEMA, "protocol_id": PROTOCOL_ID,
        "trial_id": trial["trial_id"], "contact_site": trial["contact_site"],
        "pair_id": trial["pair_id"],
        "direction": trial["direction"], "controller_arm": trial["controller_arm"],
        "apparatus_waveform_id": apparatus["waveform_id"],
        "apparatus_trigger_threshold_n": trigger_threshold_n,
        "human_safety_claim": False,
        "claim_scope": "offline apparatus diagnostic; not a qualified live or human-contact result",
        "visible_no_extra_mark_finding": not reasons and
            image["outside_corridor_area_px2"] == 0,
        "performance_eligible": not reasons,
        "ineligible_reasons": reasons,
        "safety_outcome": dict(lifecycle),
        "task_recovery_after_event_s": task_recovery_s,
        "home_verified_after_event_s": home_recovery_s,
        "clock_alignment_error_s": alignment_s,
        "cadence": observed_rates,
        "windows": windows,
        "mark_evidence": dict(image),
        "corridor_admission": dict(admission),
        "legacy_5_60_mae_scope": "separate no-disturbance protocol only",
    }


def compare_paired_results(results: list[Mapping[str, Any]], *, candidate_arm: str) -> dict[str, Any]:
    """Compare matched apparatus pulses without promoting ineligible rows."""
    if candidate_arm not in {"end_effector_only", "joint_only", "dual"}:
        raise CollisionTrialError("candidate_arm identity differs")
    pairs: dict[str, dict[str, Mapping[str, Any]]] = {}
    for row in results:
        if row.get("schema") != RESULT_SCHEMA or row.get("protocol_id") != PROTOCOL_ID:
            raise CollisionTrialError("paired result identity differs")
        pair_id = row.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id:
            raise CollisionTrialError("paired result lacks pair_id")
        arm = row.get("controller_arm")
        if arm not in {"tase_baseline", candidate_arm}:
            continue
        if arm in pairs.setdefault(pair_id, {}):
            raise CollisionTrialError(f"duplicate {arm} in pair {pair_id}")
        pairs[pair_id][arm] = row
    if not pairs:
        raise CollisionTrialError("no candidate/baseline apparatus pairs")
    fields = {
        "outside_corridor_area_px2": lambda r: r["mark_evidence"]["outside_corridor_area_px2"],
        "max_outside_distance_px": lambda r: (
            0 if r["mark_evidence"]["max_outside_distance_px"] is None
            and r["mark_evidence"]["outside_corridor_area_px2"] == 0
            else r["mark_evidence"]["max_outside_distance_px"]),
        "impact_pusher_peak_n": lambda r: r["windows"]["impact"]["pusher_peak_n"],
        "impact_pusher_impulse_n_s": lambda r: r["windows"]["impact"]["pusher_impulse_n_s"],
        "swing_loaded_tangent_slip_n_m": lambda r: r["windows"]["swing"]["loaded_tangent_slip_n_m"],
        "swing_max_lateral_error_m": lambda r: r["windows"]["swing"]["max_lateral_error_m"],
    }
    differences = {name: [] for name in fields}
    excluded = []
    included = []
    candidate_mark_areas = []
    identities = set()
    for pair_id, arms in sorted(pairs.items()):
        if set(arms) != {"tase_baseline", candidate_arm}:
            excluded.append({"pair_id": pair_id, "reason": "pair_incomplete"})
            continue
        base, candidate = arms["tase_baseline"], arms[candidate_arm]
        if (base.get("contact_site") != candidate.get("contact_site")
                or base.get("direction") != candidate.get("direction")
                or base.get("apparatus_waveform_id") != candidate.get("apparatus_waveform_id")
                or base.get("apparatus_trigger_threshold_n")
                != candidate.get("apparatus_trigger_threshold_n")
                or base.get("corridor_admission", {}).get("mask_sha256")
                != candidate.get("corridor_admission", {}).get("mask_sha256")):
            excluded.append({"pair_id": pair_id, "reason": "perturbation_identity_differs"})
            continue
        if base.get("performance_eligible") is not True or candidate.get("performance_eligible") is not True:
            excluded.append({"pair_id": pair_id, "reason": "trial_performance_ineligible"})
            continue
        try:
            values = {name: (
                _number(getter(candidate), name, nonnegative=True)
                - _number(getter(base), name, nonnegative=True)
            ) for name, getter in fields.items()}
        except (KeyError, TypeError) as exc:
            raise CollisionTrialError(f"paired metric missing in {pair_id}") from exc
        for name, value in values.items():
            differences[name].append(value)
        candidate_mark_areas.append(candidate["mark_evidence"]["outside_corridor_area_px2"])
        included.append(pair_id)
        identities.add((base["contact_site"], base["direction"],
                        base["apparatus_waveform_id"]))
    if len(identities) > 1:
        raise CollisionTrialError("comparison mixes contact site, direction, or waveform")

    def interval(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"median_difference": None, "bootstrap_95_low": None, "bootstrap_95_high": None}
        rng = random.Random(0)
        estimates = sorted(statistics.median(rng.choices(values, k=len(values)))
                           for _ in range(2000))
        return {"median_difference": statistics.median(values),
                "bootstrap_95_low": estimates[49],
                "bootstrap_95_high": estimates[1949]}

    metrics = {name: interval(values) for name, values in differences.items()}
    qualified = len(included) >= 10
    damage_interval = metrics["outside_corridor_area_px2"]
    return {
        "schema": "tase.collision-paired-comparison-v1", "protocol_id": PROTOCOL_ID,
        "candidate_arm": candidate_arm, "matched_pairs": len(included),
        "perturbation_identity": (dict(zip(("contact_site", "direction", "waveform_id"),
                                           next(iter(identities)))) if identities else None),
        "minimum_pairs_for_claim": 10, "comparison_eligible": qualified,
        "included_pair_ids": included, "excluded_pairs": excluded,
        "safety_incidents": sum(
            row.get("safety_outcome", {}).get("guard_trip") is True
            or row.get("safety_outcome", {}).get("recovery_exhausted") is True
            or row.get("safety_outcome", {}).get("home_verified") is not True
            for row in results
        ),
        "candidate_zero_extra_marks_all_eligible": bool(candidate_mark_areas)
            and all(value == 0 for value in candidate_mark_areas),
        "damage_improvement_supported": bool(
            qualified and damage_interval["bootstrap_95_high"] is not None
            and damage_interval["bootstrap_95_high"] < 0
            and all(value == 0 for value in candidate_mark_areas)
        ),
        "human_safety_claim": False,
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--trial", type=Path)
    choice.add_argument("--results", type=Path, help="JSON list of scored trial diagnostics")
    parser.add_argument("--candidate-arm", choices=("end_effector_only", "joint_only", "dual"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.trial is not None:
        result = score_trial(json.loads(args.trial.read_text(encoding="utf-8")))
    else:
        if args.candidate_arm is None:
            parser.error("--candidate-arm is required with --results")
        result = compare_paired_results(
            json.loads(args.results.read_text(encoding="utf-8")),
            candidate_arm=args.candidate_arm,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(result, target, indent=2, sort_keys=True, allow_nan=False)
        target.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
