"""Deterministic equal-budget offline replay for the six contact laws.

The campaign uses one generated observation tape, one fixed Jacobian and the
real native law + common outer loop + native QP composition.  A tiny explicit
contact-plant proxy closes the force loop so candidate responses can be
compared without pretending that a prescribed-input replay is a robot result.
The ledger remains the authority for paired budgets, failure consumption and
parameter freeze.  No transport, robot, bridge, or live optimizer is opened.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from contact_benchmark_freshness import replay_freshness_sensitivity
from contact_benchmark_kernel import ContactKernel
from contact_benchmark_ledger import ContactLedger
from contact_benchmark_protocol import CONTROLLERS, Task, holdout_schedule, protocol
from contact_benchmark_tuner import ContactBenchmarkTuner, ContactLawCandidate
from contact_laws import ContactLaw
from step5c_calibrated_kinematics_audit import rotvec_to_matrix


CAMPAIGN_SCHEMA = "ur10e.contact-six-offline-campaign-v1"
TRIAL_SCHEMA = "ur10e.contact-six-offline-trial-v1"
DEFAULT_HORIZON_TICKS = 256
DEFAULT_SEED = 20260918
_DT_S = 0.002


@dataclass(frozen=True)
class CampaignConfig:
    horizon_ticks: int = DEFAULT_HORIZON_TICKS
    seed: int = DEFAULT_SEED
    disturbance_amplitude_n: float = 0.5

    def __post_init__(self) -> None:
        if type(self.horizon_ticks) is not int or self.horizon_ticks < 32:
            raise ValueError("campaign horizon_ticks must be an integer >= 32")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("campaign seed must be a non-negative integer")
        if not math.isfinite(self.disturbance_amplitude_n) or self.disturbance_amplitude_n < 0.0:
            raise ValueError("disturbance amplitude must be finite and non-negative")


def _sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _mean(values: list[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _rms(values: list[float]) -> float | None:
    return math.sqrt(math.fsum(value * value for value in values) / len(values)) if values else None


def _age_for_tick(index: int) -> float:
    return 0.010 if index < 100 else 0.050


def _external_normal_delta(condition: str, index: int, *, horizon_ticks: int, amplitude_n: float) -> float:
    if condition == "nominal" or amplitude_n == 0.0:
        return 0.0
    start = int(0.35 * horizon_ticks)
    end = int(0.55 * horizon_ticks)
    if not start <= index < end:
        return 0.0
    phase = (index - start) / max(1.0, float(end - start))
    return amplitude_n * _DT_S * (0.5 + 0.5 * math.sin(math.pi * phase))


def _candidate_parameters(candidate: ContactLawCandidate | Mapping[str, Any]) -> tuple[str, dict[str, float]]:
    if isinstance(candidate, ContactLawCandidate):
        return candidate.law, candidate.parameters
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate must be typed or mapping")
    law = str(candidate.get("law"))
    parameters = candidate.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {key: value for key, value in candidate.items() if key != "law"}
    return law, {str(key): float(value) for key, value in parameters.items()}


def _evaluate_candidate(
    *,
    candidate: ContactLawCandidate | Mapping[str, Any],
    condition: str,
    config: CampaignConfig,
    experiment_root: Path,
    capture_session_id: str,
    home: Mapping[str, Any],
    protocol_sha256: str,
    return_rows: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    law_name, parameters = _candidate_parameters(candidate)
    anchor = np.asarray(home["home_pose"][:3], dtype=float)
    target_rotvec = np.asarray(home["home_pose"][3:], dtype=float)
    target_rotation = rotvec_to_matrix(target_rotvec)
    task = Task()
    qp_library = experiment_root / "build" / "contact-qp" / "libcontact_qp.so"
    law_build_root = experiment_root / "build" / "contact-six-laws"
    actual_position = anchor.copy()
    normal_force = 5.0
    previous_qdot = np.zeros(6, dtype=float)
    force_errors: list[float] = []
    path_errors: list[float] = []
    vibration: list[float] = []
    rows: list[dict[str, Any]] = []
    stale_stop_count = 0
    geometric_latency_reject_count = 0
    error: str | None = None

    try:
        with ContactLaw(
            law_name,
            parameters,
            dimension=3,
            dt_s=_DT_S,
            build_root=law_build_root,
        ) as law:
            kernel = ContactKernel(
                law=law,
                qp_library=qp_library,
                anchor_m=anchor,
                task_basis=np.eye(3),
                target_rotation=target_rotation,
                raw_force_limit_n=20.0,
                raw_torque_limit_nm=2.0,
                qp_deadline_s=None,
                kernel_deadline_s=None,
            )
            for index in range(config.horizon_ticks):
                path_time_s = index * _DT_S
                reference = task.reference(path_time_s)
                age_s = _age_for_tick(index)
                try:
                    result = kernel.step(
                        jacobian=np.eye(6),
                        joint_velocity_lower=np.full(6, -0.05),
                        joint_velocity_upper=np.full(6, 0.05),
                        time_s=(index + 1) * _DT_S,
                        dt_s=_DT_S,
                        phase="path",
                        path_time_s=path_time_s,
                        position_m=actual_position,
                        rotation=target_rotation,
                        raw_force_base_n=(0.0, 0.0, normal_force),
                        raw_torque_base_nm=(0.0, 0.0, 0.0),
                        state_age_s=age_s,
                    )
                except ValueError as exc:
                    message = str(exc)
                    if "latency uncertainty" in message:
                        geometric_latency_reject_count += 1
                    if "stale" in message.lower():
                        stale_stop_count += 1
                    raise
                qdot = np.asarray(result["qdot_rad_s"], dtype=float)
                force_error = float(result["force_error_n"])
                path_error = float(np.linalg.norm(result["path_error_task_m"]))
                vibration_metric = float(np.linalg.norm(qdot - previous_qdot))
                force_errors.append(force_error)
                path_errors.append(path_error)
                vibration.append(vibration_metric)
                row = {
                    "controller": law_name,
                    "condition": condition,
                    "capture_session_id": capture_session_id,
                    "time_s": path_time_s,
                    "observation_age_s": age_s,
                    "age_band": "fresh" if age_s < 0.020 else "held",
                    "force_error_n": force_error,
                    "path_error_m": path_error,
                    "vibration_metric": vibration_metric,
                }
                rows.append(row)
                previous_qdot = qdot
                # Explicitly named deterministic plant proxy.  It is only a
                # source of offline response variation; it is not a UR10e
                # model or hardware evidence.
                normal_force += _DT_S * (
                    -120.0 * float(result["twist_base"][2])
                    - 0.4 * (normal_force - 5.0)
                )
                normal_force += _external_normal_delta(
                    condition,
                    index,
                    horizon_ticks=config.horizon_ticks,
                    amplitude_n=config.disturbance_amplitude_n,
                )
                normal_force = float(np.clip(normal_force, 1.0, 10.0))
                actual_position = actual_position + _DT_S * np.asarray(result["twist_base"][:3], dtype=float)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    complete = error is None and len(rows) == config.horizon_ticks
    nominal_feasible = bool(complete and all(math.isfinite(value) for value in (*force_errors, *path_errors, *vibration)))
    evidence: dict[str, Any] = {
        "schema": TRIAL_SCHEMA,
        "version": 1,
        "controller": law_name,
        "condition": condition,
        "candidate": candidate.as_dict() if isinstance(candidate, ContactLawCandidate) else dict(candidate),
        "capture_session_id": capture_session_id,
        "protocol_sha256": protocol_sha256,
        "horizon_ticks": config.horizon_ticks,
        "dt_s": _DT_S,
        "nominal_feasible": nominal_feasible,
        "force_mae_n": _mean([abs(value) for value in force_errors]) if complete else None,
        "force_rmse_n": _rms(force_errors) if complete else None,
        "path_rms_m": _rms(path_errors) if complete else None,
        "path_peak_m": max(path_errors) if path_errors else None,
        "vibration_rms": _rms(vibration) if complete else None,
        "stale_stop_count": stale_stop_count,
        "geometric_latency_reject_count": geometric_latency_reject_count,
        "held_fraction": sum(row["age_band"] == "held" for row in rows) / len(rows) if rows else 0.0,
        "objective": _rms(force_errors) if complete else None,
        "objective_eligible": nominal_feasible,
        "error": error,
        "claim_scope": "offline fixed-Jacobian replay with an explicit synthetic contact-plant proxy; no physical acceptance",
    }
    return evidence, rows if return_rows else []


def _ledger_evidence(evidence: Mapping[str, Any], *, attempt_id: str) -> dict[str, Any]:
    payload = dict(evidence)
    payload["attempt_id"] = attempt_id
    payload["artifact_sha256"] = _sha256(payload)
    if payload.get("objective_eligible") is not True:
        payload["objective_eligible"] = False
        payload["objective"] = None
    return payload


def _best_candidates(rows_by_controller: Mapping[str, Iterable[Any]]) -> dict[str, Mapping[str, Any]]:
    selected: dict[str, Mapping[str, Any]] = {}
    for controller in CONTROLLERS:
        usable = [
            row for row in rows_by_controller[controller]
            if row.status == "completed" and row.nominal_feasible and row.objective is not None
        ]
        if not usable:
            raise RuntimeError(f"no measured feasible candidate for {controller}")
        best = min(usable, key=lambda row: (float(row.objective), row.candidate.key if hasattr(row.candidate, "key") else json.dumps(row.candidate, sort_keys=True)))
        selected[controller] = best.candidate.as_dict() if isinstance(best.candidate, ContactLawCandidate) else dict(best.candidate)
    return selected


def run_equal_budget_campaign(
    *,
    experiment_root: Path,
    output_dir: Path,
    config: CampaignConfig = CampaignConfig(),
) -> dict[str, Any]:
    """Complete the six-law 24-unit paired offline campaign and holdout replay."""

    root = Path(experiment_root).resolve()
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ledger_path = out / "campaign.sqlite"
    if ledger_path.exists():
        raise FileExistsError(f"refusing to overwrite campaign ledger: {ledger_path}")
    protocol_payload = protocol()
    protocol_sha256 = str(protocol_payload["sha256"])
    home = json.loads((root / "report" / "contact-six-qp-20260917" / "preserved-home.json").read_text(encoding="utf-8"))
    capture_session_id = _sha256({
        "schema": "ur10e.contact-six-capture-session-v1",
        "protocol_sha256": protocol_sha256,
        "seed": config.seed,
        "horizon_ticks": config.horizon_ticks,
        "home_pose": home["home_pose"],
        "raw_stream": "deterministic_force_capture_v1",
    })
    tuner = ContactBenchmarkTuner(seed=config.seed)
    ledger = ContactLedger(ledger_path, protocol_sha256=protocol_sha256)
    try:
        for controller in CONTROLLERS:
            for unit_index in range(24):
                observations = ledger.training_observations(controller) if unit_index else []
                proposal = tuner.propose(controller, observations, unit_index)
                candidate = proposal.candidate.as_dict()
                nominal_id = f"{capture_session_id[:12]}-{controller}-{unit_index:02d}-nominal"
                unit = ledger.begin(
                    attempt_id=nominal_id,
                    controller=controller,
                    candidate=candidate,
                    condition="nominal",
                )
                nominal, _ = _evaluate_candidate(
                    candidate=proposal.candidate,
                    condition="nominal",
                    config=config,
                    experiment_root=root,
                    capture_session_id=capture_session_id,
                    home=home,
                    protocol_sha256=protocol_sha256,
                )
                nominal_payload = _ledger_evidence(nominal, attempt_id=nominal_id)
                ledger.seal(
                    nominal_id,
                    status="complete" if nominal_payload["objective_eligible"] else "failed",
                    evidence=nominal_payload,
                )
                disturbed_id = f"{capture_session_id[:12]}-{controller}-{unit_index:02d}-disturbed"
                ledger.begin(
                    attempt_id=disturbed_id,
                    controller=controller,
                    candidate=candidate,
                    condition="disturbed",
                    unit=unit,
                )
                disturbed, _ = _evaluate_candidate(
                    candidate=proposal.candidate,
                    condition="normal_pulse",
                    config=config,
                    experiment_root=root,
                    capture_session_id=capture_session_id,
                    home=home,
                    protocol_sha256=protocol_sha256,
                )
                disturbed["condition"] = "disturbed"
                disturbed_payload = _ledger_evidence(disturbed, attempt_id=disturbed_id)
                ledger.seal(
                    disturbed_id,
                    status="complete" if disturbed_payload["objective_eligible"] else "failed",
                    evidence=disturbed_payload,
                )
        training = {controller: ledger.training_observations(controller) for controller in CONTROLLERS}
        selected = _best_candidates(training)
        freeze_sha256 = ledger.freeze(selected)
        progress = ledger.progress()
    finally:
        ledger.close()

    holdout_rows: list[dict[str, Any]] = []
    holdout_summary: dict[str, Any] = {}
    for controller in CONTROLLERS:
        candidate = selected[controller]
        results: list[dict[str, Any]] = []
        for item in holdout_schedule(config.seed + 1, repeats=5):
            if item["controller"] != controller:
                continue
            condition = "nominal" if item["scenario"] == "nominal" else "normal_pulse"
            evidence, rows = _evaluate_candidate(
                candidate=candidate,
                condition=condition,
                config=config,
                experiment_root=root,
                capture_session_id=capture_session_id,
                home=home,
                protocol_sha256=protocol_sha256,
                return_rows=True,
            )
            evidence["holdout_ordinal"] = item["ordinal"]
            evidence["scenario"] = item["scenario"]
            results.append(evidence)
            for row in rows:
                holdout_rows.append({**row, "scenario": item["scenario"], "holdout_ordinal": item["ordinal"]})
        holdout_summary[controller] = {
            "trial_count": len(results),
            "complete_count": sum(item["objective_eligible"] is True for item in results),
            "force_rmse_mean_n": _mean([float(item["force_rmse_n"]) for item in results if item["force_rmse_n"] is not None]),
            "path_rms_mean_m": _mean([float(item["path_rms_m"]) for item in results if item["path_rms_m"] is not None]),
            "claim_scope": "offline randomized holdout replay; no physical acceptance",
        }

    sensitivity = replay_freshness_sensitivity(holdout_rows)
    (out / "holdout-rows.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in holdout_rows),
        encoding="utf-8",
    )
    (out / "freshness-sensitivity.json").write_text(
        json.dumps(sensitivity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result = {
        "schema": CAMPAIGN_SCHEMA,
        "protocol_sha256": protocol_sha256,
        "capture_session_id": capture_session_id,
        "seed": config.seed,
        "horizon_ticks": config.horizon_ticks,
        "dt_s": _DT_S,
        "controllers": list(CONTROLLERS),
        "rpsfc_selectable": False,
        "budget": {"per_controller_units": 24, "paired_trials_per_unit": 2},
        "progress": progress,
        "freeze_sha256": freeze_sha256,
        "selected_candidates": selected,
        "holdout": holdout_summary,
        "freshness_sensitivity": "freshness-sensitivity.json",
        "network_used": False,
        "device_io": False,
        "motion_authorized": False,
        "live_qualified": False,
        "hardware_qualified": False,
        "claim_scope": "offline equal-budget native-law/QP replay with explicit synthetic contact-plant proxy; no live or physical acceptance",
    }
    (out / "campaign-summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


__all__ = ["CAMPAIGN_SCHEMA", "CampaignConfig", "run_equal_budget_campaign"]
