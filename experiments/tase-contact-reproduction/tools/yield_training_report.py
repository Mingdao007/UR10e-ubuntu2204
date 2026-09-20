#!/usr/bin/env python3
"""Read-only bounded training-result report for one yield-fair campaign.

Does not launch training, validation, simulation, or hardware. Import and
help are inert. Never instantiates a writer ledger. One campaign root only.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from contact_yield_metrics import compare_pair, summarize_trial
from contact_yield_protocol import parse_scenario
from yield_contact_tuner import EI_UNITS, INITIAL_UNITS, METHODS, REPEAT_UNITS, TOTAL_UNITS


REPORT_SCHEMA = "ur10e.yield-training-report-v1"
CAMPAIGN_STATE_SCHEMA = "ur10e.yield-fair-campaign-state-v1"
STOP_BEFORE_EI_MIN_FEASIBLE = 3
CLAIM_SCOPE = (
    "prospective DEVELOPMENT-model comparison; no real precise-contact, "
    "safety, or physical qualification claim"
)
TRAINING_CONTEXT = (
    "training-only development snapshot; not independent validation or a "
    "physical result; no superiority claim"
)
LOW_LOAD_DEFINITION = (
    "duration of true_normal_load_n < 1 N on the declared sample grid; "
    "not geometric separation or physical contact loss"
)
SLOT_STATES = (
    "completed_feasible",
    "completed_nominal_infeasible",
    "completed_disturbed_guard_infeasible",
    "completed_nominal_and_disturbed_guard_infeasible",
    "failed",
    "incomplete_running_or_pending",
    "not_run",
    "stopped_before_ei",
)
TERMINAL_STATUSES = frozenset(("complete", "failed", "interrupted", "censored"))
FAILED_STATUSES = frozenset(("failed", "interrupted", "censored"))
CONDITIONS = ("nominal", "disturbed")
DESCRIPTOR_KEYS = (
    "force_mae_n",
    "force_rmse_n",
    "force_peak_n",
    "force_error_peak_n",
    "load_min_n",
    "path_rms_m",
    "path_peak_m",
    "progress_ratio",
    "actual_progress_m",
    "reference_progress_m",
    "attitude_rms_rad",
    "attitude_peak_rad",
    "low_load_duration_s",
    "saturation_ticks",
    "qp_intervention_ticks",
)


class YieldTrainingReportError(ValueError):
    """Invalid campaign snapshot, missing complete evidence, or output conflict."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return float(value)
    if isinstance(value, (int, float)):
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _scheduled_phase(unit: int) -> str:
    if unit < 0 or unit >= TOTAL_UNITS:
        raise YieldTrainingReportError(f"unit {unit} is outside the 24-slot schedule")
    if unit < INITIAL_UNITS:
        return "initial"
    if unit < INITIAL_UNITS + EI_UNITS:
        return "bayesian_ei"
    return "repeat_incumbent"


def _attempt_id(method: str, unit: int, condition: str) -> str:
    return f"{method}-{unit:02d}-{condition}"


def _artifact_path(campaign_root: Path, attempt_id: str) -> Path:
    return campaign_root / "attempts" / attempt_id / "artifact.json"


def _mechanical(candidate: Mapping[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(candidate, Mapping):
        return None
    try:
        m = float(candidate["m"])
        mu = float(candidate["mu"])
        g = float(candidate["g"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (m, mu, g)):
        return None
    return {"m": m, "mu": mu, "g": g}


def _mechanical_equal(left: Mapping[str, float] | None, right: Mapping[str, float] | None) -> bool:
    if left is None or right is None:
        return False
    return all(float(left[name]) == float(right[name]) for name in ("m", "mu", "g"))


def _parse_candidate(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise YieldTrainingReportError("registered unit candidate is not an object")
    return dict(payload)


def _parse_evidence(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise YieldTrainingReportError("attempt evidence is not an object")
    return dict(payload)


def _digest_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_campaign_state(campaign_root: Path) -> dict[str, Any]:
    campaign_root = Path(campaign_root).expanduser().resolve()
    state_path = campaign_root / "campaign.json"
    sqlite_path = campaign_root / "campaign.sqlite"
    if not state_path.is_file():
        raise YieldTrainingReportError(f"campaign state is missing: {state_path}")
    if not sqlite_path.is_file():
        raise YieldTrainingReportError(f"campaign ledger is missing: {sqlite_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, Mapping):
        raise YieldTrainingReportError("campaign state is not an object")
    if state.get("schema") != CAMPAIGN_STATE_SCHEMA:
        raise YieldTrainingReportError("campaign state schema differs")
    declared = state.get("ledger_path")
    if declared:
        declared_path = Path(str(declared))
        if declared_path.is_absolute() and declared_path.resolve() != sqlite_path:
            raise YieldTrainingReportError(
                "refusing to read a ledger path outside the campaign root"
            )
    return dict(state)


def read_readonly_snapshot(campaign_root: Path) -> dict[str, Any]:
    """Single read-only SQLite transaction. Closes before any artifact parse."""
    campaign_root = Path(campaign_root).expanduser().resolve()
    state = load_campaign_state(campaign_root)
    sqlite_path = campaign_root / "campaign.sqlite"
    uri = f"file:{sqlite_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("BEGIN")
        metadata = {
            str(key): value
            for key, value in connection.execute("SELECT key, value FROM metadata")
        }
        units = [
            (str(controller), int(number), candidate)
            for controller, number, candidate in connection.execute(
                "SELECT controller, number, candidate FROM units ORDER BY controller, number"
            )
        ]
        attempts = [
            (str(attempt_id), str(controller), int(unit), str(condition), str(status), evidence)
            for attempt_id, controller, unit, condition, status, evidence in connection.execute(
                "SELECT id, controller, unit, condition, status, evidence FROM attempts ORDER BY rowid"
            )
        ]
    finally:
        connection.close()
    observed = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "observed_utc": observed,
        "source": "transactionally consistent read-only SQLite snapshot",
        "campaign_root": str(campaign_root),
        "campaign_state": state,
        "metadata": metadata,
        "units": units,
        "attempts": attempts,
    }


def _compact_pair_member(artifact: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for row in artifact.get("rows") or ():
        if not isinstance(row, Mapping):
            continue
        rows.append(
            {
                "time_s": row.get("time_s"),
                "position_m": row.get("position_m"),
                "true_normal_load_n": row.get("true_normal_load_n"),
            }
        )
    metrics = artifact.get("metrics") if isinstance(artifact.get("metrics"), Mapping) else {}
    return {
        "method": artifact.get("method"),
        "material": artifact.get("material"),
        "dt_s": artifact.get("dt_s"),
        "duration_s": artifact.get("duration_s"),
        "preparation": artifact.get("preparation"),
        "timeline": artifact.get("timeline"),
        "identity": artifact.get("identity"),
        "scenario": artifact.get("scenario"),
        "metrics": dict(metrics) if isinstance(metrics, Mapping) else {"failed": False},
        "rows": rows,
    }


def _load_min(rows: Sequence[Mapping[str, Any]]) -> float | None:
    values = []
    for row in rows:
        try:
            value = float(row["true_normal_load_n"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    if not values:
        return None
    return float(min(values))


def extract_member_descriptors(artifact: Mapping[str, Any]) -> dict[str, Any]:
    rows = list(artifact.get("rows") or ())
    metrics = artifact.get("metrics") if isinstance(artifact.get("metrics"), Mapping) else {}
    failed = bool(metrics.get("failed")) if isinstance(metrics, Mapping) else False
    dt_s = artifact.get("dt_s")
    try:
        dt_s = float(dt_s)
    except (TypeError, ValueError):
        dt_s = 0.002
    summary = summarize_trial(
        rows,
        failed=failed,
        failure_message=metrics.get("failure_message") if isinstance(metrics, Mapping) else None,
        scenario=str(artifact.get("scenario") or "nominal"),
        material=str(artifact.get("material") or ""),
        method=str(artifact.get("method") or ""),
        dt_s=dt_s,
        kinematics_kind=str(artifact.get("kinematics_kind") or "unknown"),
        campaign_kind=str(artifact.get("campaign_kind") or "training"),
        timeline=str(artifact.get("timeline") or ""),
    )
    return {
        "force_mae_n": _json_number(summary.get("force_mae_n")),
        "force_rmse_n": _json_number(summary.get("force_rmse_n")),
        "force_peak_n": _json_number(summary.get("force_peak_n")),
        "force_error_peak_n": _json_number(summary.get("force_error_peak_n")),
        "load_min_n": None if failed else _load_min(rows),
        "path_rms_m": _json_number(summary.get("path_rmse_m")),
        "path_peak_m": _json_number(summary.get("path_peak_m")),
        "progress_ratio": _json_number(summary.get("progress_ratio")),
        "actual_progress_m": _json_number(summary.get("actual_progress_m")),
        "reference_progress_m": _json_number(summary.get("reference_progress_m")),
        "attitude_rms_rad": _json_number(summary.get("orientation_rmse_rad")),
        "attitude_peak_rad": _json_number(summary.get("orientation_peak_rad")),
        "low_load_duration_s": _json_number(summary.get("contact_loss_duration_s")),
        "low_load_duration_definition": LOW_LOAD_DEFINITION,
        "saturation_ticks": _json_number(summary.get("saturation_ticks")),
        "qp_intervention_ticks": _json_number(summary.get("qp_intervention_ticks")),
        "n_samples": _json_number(summary.get("n_samples")),
        "failed": bool(summary.get("failed")),
        "metric_source": "contact_yield_metrics.summarize_trial",
    }


def _postrelease_max_offset_m(
    nominal: Mapping[str, Any],
    disturbed: Mapping[str, Any],
    recovery: Mapping[str, Any],
) -> float | None:
    release = recovery.get("release_s")
    if release is None:
        try:
            spec = parse_scenario(str(disturbed["scenario"]), timeline=str(disturbed["timeline"]))
        except (KeyError, TypeError, ValueError):
            return None
        start = spec.get("start_s")
        if start is None:
            return None
        release = (
            float(start)
            + float(spec["width_s"])
            + float(spec["hold_s"])
            + float(spec["release_s"])
        )
    try:
        release_s = float(release)
    except (TypeError, ValueError):
        return None
    peak = None
    for n_row, d_row in zip(nominal.get("rows") or (), disturbed.get("rows") or ()):
        try:
            time_s = float(d_row["time_s"])
            if time_s < release_s:
                continue
            n_pos = n_row["position_m"]
            d_pos = d_row["position_m"]
            dx = float(d_pos[0]) - float(n_pos[0])
            dy = float(d_pos[1]) - float(n_pos[1])
            dz = float(d_pos[2]) - float(n_pos[2])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if peak is None or distance > peak:
            peak = distance
    return None if peak is None else float(peak)


def _pair_recovery(nominal: Mapping[str, Any], disturbed: Mapping[str, Any]) -> dict[str, Any]:
    try:
        recovery = compare_pair(nominal, disturbed)
    except (TypeError, ValueError, KeyError) as error:
        return {
            "eligible": False,
            "reason": f"recovery unavailable: {error}",
            "metric_source": "contact_yield_metrics.compare_pair",
        }
    payload = dict(recovery)
    payload["metric_source"] = "contact_yield_metrics.compare_pair"
    if payload.get("eligible") is True:
        payload["postrelease_max_offset_m"] = _postrelease_max_offset_m(
            nominal, disturbed, payload
        )
    else:
        payload["postrelease_max_offset_m"] = None
    return _json_safe(payload)


def load_complete_artifact(
    campaign_root: Path,
    attempt_id: str,
    evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], str, int]:
    expected = evidence.get("artifact_sha256")
    if not _digest_ok(expected):
        raise YieldTrainingReportError(
            f"complete member {attempt_id} is missing a 64-hex artifact_sha256"
        )
    path = _artifact_path(campaign_root, attempt_id)
    if not path.is_file():
        raise YieldTrainingReportError(
            f"complete member {attempt_id} raw artifact is missing: {path}"
        )
    digest = _sha256_file(path)
    if digest != expected:
        raise YieldTrainingReportError(
            f"complete member {attempt_id} raw digest differs from ledger artifact_sha256"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise YieldTrainingReportError(
            f"complete member {attempt_id} raw artifact is not an object"
        )
    return dict(payload), digest, path.stat().st_size


def _empty_member(attempt_id: str | None, status: str | None) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "status": status,
        "evidence_present": False,
        "reason": None,
        "artifact_sha256": None,
        "artifact_parsed": False,
        "descriptors": None,
    }


def _require_complete_bool(value: Any, role: str, label: str) -> bool:
    if type(value) is not bool:
        raise YieldTrainingReportError(
            f"complete pair {label}: {role} must be bool; missing evidence is not inferred as valid"
        )
    return value


def _checks_match_flag(checks: Any, expected: bool, role: str, label: str) -> None:
    if not isinstance(checks, Mapping) or not checks:
        raise YieldTrainingReportError(
            f"complete pair {label}: {role} missing; missing checks are not inferred as valid"
        )
    for name, value in checks.items():
        if type(value) is not bool:
            raise YieldTrainingReportError(
                f"complete pair {label}: {role}.{name} must be bool"
            )
    actual = all(value is True for value in checks.values())
    if actual is not expected:
        raise YieldTrainingReportError(
            f"complete pair {label}: {role} contradict the corresponding feasibility flag"
        )


def complete_pair_flags(
    *,
    method: str,
    unit: int,
    nominal_evidence: Mapping[str, Any] | None,
    disturbed_evidence: Mapping[str, Any] | None,
) -> dict[str, bool]:
    """Require consistent complete-pair flags. Do not override true/false or infer missing."""
    label = f"{method}-{unit:02d}"
    if not isinstance(nominal_evidence, Mapping) or not isinstance(disturbed_evidence, Mapping):
        raise YieldTrainingReportError(
            f"complete pair {label}: nominal and disturbed evidence are required"
        )
    nominal_feasible = _require_complete_bool(
        nominal_evidence.get("nominal_feasible"), "nominal_feasible", label
    )
    guards = _require_complete_bool(
        disturbed_evidence.get("disturbed_guards_ok"), "disturbed_guards_ok", label
    )
    pair_feasible = _require_complete_bool(
        disturbed_evidence.get("pair_feasible"), "pair_feasible", label
    )
    computed = bool(nominal_feasible and guards)
    if pair_feasible is not computed:
        raise YieldTrainingReportError(
            f"complete pair {label}: pair_feasible contradicts nominal_feasible "
            "and disturbed_guards_ok"
        )
    reported = disturbed_evidence.get("nominal_feasible_reported")
    if reported is not None and reported != nominal_feasible:
        raise YieldTrainingReportError(
            f"complete pair {label}: nominal_feasible_reported contradicts nominal_feasible"
        )
    nominal_checks = nominal_evidence.get("nominal_checks")
    if nominal_checks is None:
        nominal_checks = disturbed_evidence.get("nominal_checks")
    _checks_match_flag(nominal_checks, nominal_feasible, "nominal_checks", label)
    disturbed_checks = disturbed_evidence.get("disturbed_checks")
    if disturbed_checks is None and "disturbed_checks" not in disturbed_evidence:
        raise YieldTrainingReportError(
            f"complete pair {label}: disturbed_checks missing; missing checks are not inferred as valid"
        )
    _checks_match_flag(disturbed_checks, guards, "disturbed_checks", label)
    extra_nominal = disturbed_evidence.get("nominal_checks")
    if extra_nominal is not None and extra_nominal is not nominal_checks:
        _checks_match_flag(extra_nominal, nominal_feasible, "disturbed.nominal_checks", label)
    return {
        "nominal_feasible": nominal_feasible,
        "disturbed_guards_ok": guards,
        "pair_feasible": pair_feasible,
    }


def _classify_complete_pair(
    *,
    nominal_feasible: bool,
    disturbed_guards_ok: bool,
    pair_feasible: bool,
) -> str:
    if pair_feasible is True:
        if nominal_feasible is not True or disturbed_guards_ok is not True:
            raise YieldTrainingReportError(
                "pair_feasible=True cannot override a false nominal or disturbed-guard flag"
            )
        return "completed_feasible"
    if nominal_feasible is False and disturbed_guards_ok is False:
        return "completed_nominal_and_disturbed_guard_infeasible"
    if nominal_feasible is False:
        return "completed_nominal_infeasible"
    return "completed_disturbed_guard_infeasible"


def _bool_or_none(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    return None


def _finite_objective(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        return None
    return parsed


def build_report_from_snapshot(snapshot: Mapping[str, Any], campaign_root: Path) -> dict[str, Any]:
    campaign_root = Path(campaign_root).expanduser().resolve()
    if str(snapshot.get("campaign_root")) != str(campaign_root):
        raise YieldTrainingReportError("snapshot campaign root differs from the supplied campaign")
    state = snapshot["campaign_state"]
    units_by_method: dict[str, dict[int, dict[str, Any]]] = {method: {} for method in METHODS}
    for controller, number, candidate_raw in snapshot["units"]:
        if controller not in units_by_method:
            continue
        units_by_method[controller][int(number)] = {
            "candidate": _parse_candidate(candidate_raw),
            "members": {condition: None for condition in CONDITIONS},
        }
    inflight: list[str] = []
    for attempt_id, controller, unit, condition, status, evidence_raw in snapshot["attempts"]:
        if controller not in units_by_method:
            continue
        unit_entry = units_by_method[controller].setdefault(
            int(unit), {"candidate": None, "members": {item: None for item in CONDITIONS}}
        )
        evidence = _parse_evidence(evidence_raw)
        unit_entry["members"][condition] = {
            "attempt_id": attempt_id,
            "status": status,
            "evidence": evidence,
        }
        if status == "running":
            inflight.append(attempt_id)

    slots: dict[str, list[dict[str, Any]]] = {method: [] for method in METHODS}
    digest_manifest: list[dict[str, Any]] = []
    for method in METHODS:
        registered = units_by_method[method]
        initial_terminal = 0
        initial_feasible = 0
        for unit in range(INITIAL_UNITS):
            entry = registered.get(unit)
            if entry is None:
                continue
            members = entry["members"]
            if all(
                members.get(condition) is not None
                and members[condition]["status"] in TERMINAL_STATUSES
                for condition in CONDITIONS
            ):
                initial_terminal += 1
                successful = all(
                    members[condition]["status"] == "complete" for condition in CONDITIONS
                )
                if not successful:
                    continue
                flags = complete_pair_flags(
                    method=method,
                    unit=unit,
                    nominal_evidence=(members.get("nominal") or {}).get("evidence"),
                    disturbed_evidence=(members.get("disturbed") or {}).get("evidence"),
                )
                if flags["pair_feasible"] is True:
                    initial_feasible += 1
        max_registered = max(registered) if registered else -1
        stopped = (
            initial_terminal == INITIAL_UNITS
            and initial_feasible < STOP_BEFORE_EI_MIN_FEASIBLE
            and max_registered < INITIAL_UNITS
        )
        for unit in range(TOTAL_UNITS):
            phase = _scheduled_phase(unit)
            entry = registered.get(unit)
            slot = {
                "method": method,
                "unit": unit,
                "ordinal": unit,
                "scheduled_phase": phase,
                "literal_repeat": phase == "repeat_incumbent",
                "parameters": None,
                "mechanical": None,
                "slot_state": "not_run",
                "budget_consumed": False,
                "members": {condition: _empty_member(None, None) for condition in CONDITIONS},
                "nominal_feasible": None,
                "disturbed_guards_ok": None,
                "pair_feasible": None,
                "objective": None,
                "objective_components": None,
                "nominal_checks": None,
                "disturbed_checks": None,
                "recovery": None,
                "shared_initial_triple": False,
                "coefficient_matched_pair": False,
                "ei_is_method_specific_trajectory": phase == "bayesian_ei",
            }
            if entry is None:
                if stopped and unit >= INITIAL_UNITS:
                    slot["slot_state"] = "stopped_before_ei"
                slots[method].append(slot)
                continue
            candidate = entry.get("candidate")
            slot["parameters"] = dict(candidate) if isinstance(candidate, Mapping) else None
            slot["mechanical"] = _mechanical(candidate)
            slot["budget_consumed"] = True
            members = entry["members"]
            compact: dict[str, dict[str, Any]] = {}
            statuses = []
            for condition in CONDITIONS:
                recorded = members.get(condition)
                attempt_id = (
                    recorded["attempt_id"] if recorded else _attempt_id(method, unit, condition)
                )
                status = recorded["status"] if recorded else None
                evidence = (recorded or {}).get("evidence")
                member = _empty_member(attempt_id if recorded else None, status)
                member["evidence_present"] = evidence is not None
                if evidence:
                    member["reason"] = evidence.get("reason")
                    member["artifact_sha256"] = evidence.get("artifact_sha256")
                    member["objective_eligible"] = evidence.get("objective_eligible")
                    if condition == "nominal":
                        member["nominal_feasible"] = _bool_or_none(evidence.get("nominal_feasible"))
                        slot["nominal_feasible"] = member["nominal_feasible"]
                        if evidence.get("nominal_checks") is not None:
                            slot["nominal_checks"] = dict(evidence.get("nominal_checks") or {})
                    else:
                        member["pair_feasible"] = _bool_or_none(evidence.get("pair_feasible"))
                        member["disturbed_guards_ok"] = _bool_or_none(
                            evidence.get("disturbed_guards_ok")
                        )
                        slot["pair_feasible"] = member["pair_feasible"]
                        slot["disturbed_guards_ok"] = member["disturbed_guards_ok"]
                        slot["objective"] = _finite_objective(evidence.get("objective"))
                        components = evidence.get("objective_components")
                        if isinstance(components, Mapping):
                            slot["objective_components"] = {
                                str(key): _finite_objective(value)
                                for key, value in components.items()
                            }
                        if evidence.get("disturbed_checks") is not None:
                            slot["disturbed_checks"] = dict(evidence.get("disturbed_checks") or {})
                        if evidence.get("nominal_checks") is not None and slot["nominal_checks"] is None:
                            slot["nominal_checks"] = dict(evidence.get("nominal_checks") or {})
                if status == "running" or recorded is None:
                    member["artifact_parsed"] = False
                elif status == "complete":
                    artifact, digest, size = load_complete_artifact(
                        campaign_root, attempt_id, evidence or {}
                    )
                    try:
                        descriptors = extract_member_descriptors(artifact)
                        compact[condition] = _compact_pair_member(artifact)
                    finally:
                        artifact.clear()
                        del artifact
                    member["artifact_parsed"] = True
                    member["artifact_sha256"] = digest
                    member["descriptors"] = descriptors
                    digest_manifest.append(
                        {
                            "attempt_id": attempt_id,
                            "method": method,
                            "unit": unit,
                            "condition": condition,
                            "status": status,
                            "path": str(_artifact_path(campaign_root, attempt_id).relative_to(campaign_root)),
                            "artifact_sha256": digest,
                            "bytes": size,
                            "parsed": True,
                        }
                    )
                else:
                    member["artifact_parsed"] = False
                    if _digest_ok((evidence or {}).get("artifact_sha256")):
                        digest_manifest.append(
                            {
                                "attempt_id": attempt_id,
                                "method": method,
                                "unit": unit,
                                "condition": condition,
                                "status": status,
                                "path": None,
                                "artifact_sha256": evidence.get("artifact_sha256"),
                                "bytes": None,
                                "parsed": False,
                                "note": "failed/interrupted evidence is labeled without assuming a successful artifact shape",
                            }
                        )
                slot["members"][condition] = member
                statuses.append(status)
            if any(status == "running" for status in statuses) or any(
                status is None for status in statuses
            ):
                slot["slot_state"] = "incomplete_running_or_pending"
            elif any(status in FAILED_STATUSES for status in statuses):
                slot["slot_state"] = "failed"
            elif all(status == "complete" for status in statuses):
                flags = complete_pair_flags(
                    method=method,
                    unit=unit,
                    nominal_evidence=(members.get("nominal") or {}).get("evidence"),
                    disturbed_evidence=(members.get("disturbed") or {}).get("evidence"),
                )
                slot["nominal_feasible"] = flags["nominal_feasible"]
                slot["disturbed_guards_ok"] = flags["disturbed_guards_ok"]
                slot["pair_feasible"] = flags["pair_feasible"]
                slot["slot_state"] = _classify_complete_pair(
                    nominal_feasible=flags["nominal_feasible"],
                    disturbed_guards_ok=flags["disturbed_guards_ok"],
                    pair_feasible=flags["pair_feasible"],
                )
                nominal_failed = bool((slot["members"]["nominal"].get("descriptors") or {}).get("failed"))
                disturbed_failed = bool(
                    (slot["members"]["disturbed"].get("descriptors") or {}).get("failed")
                )
                if (
                    "nominal" in compact
                    and "disturbed" in compact
                    and not nominal_failed
                    and not disturbed_failed
                ):
                    slot["recovery"] = _pair_recovery(compact["nominal"], compact["disturbed"])
            else:
                slot["slot_state"] = "incomplete_running_or_pending"
            compact.clear()
            slots[method].append(slot)

    for unit in range(INITIAL_UNITS):
        mechanicals = [slots[method][unit]["mechanical"] for method in METHODS]
        registered = [
            slots[method][unit]["slot_state"] not in {"not_run", "stopped_before_ei"}
            for method in METHODS
        ]
        matched = all(registered) and all(
            _mechanical_equal(mechanicals[0], item) for item in mechanicals[1:]
        )
        for method in METHODS:
            slots[method][unit]["shared_initial_triple"] = bool(matched)
            slots[method][unit]["coefficient_matched_pair"] = bool(matched)

    best_so_far: dict[str, list[dict[str, Any]]] = {}
    for method in METHODS:
        series = []
        best = None
        best_unit = None
        for slot in slots[method]:
            state_name = slot["slot_state"]
            if state_name in {"not_run", "stopped_before_ei", "incomplete_running_or_pending"}:
                break
            observed = slot["objective"]
            feasible = state_name == "completed_feasible"
            infeasible_complete = (
                state_name
                in {
                    "completed_nominal_infeasible",
                    "completed_disturbed_guard_infeasible",
                    "completed_nominal_and_disturbed_guard_infeasible",
                }
                and observed is not None
            )
            if feasible and observed is not None:
                if best is None or observed < best:
                    best = observed
                    best_unit = slot["unit"]
            series.append(
                {
                    "method": method,
                    "unit": slot["unit"],
                    "scheduled_phase": slot["scheduled_phase"],
                    "literal_repeat": slot["literal_repeat"],
                    "slot_state": state_name,
                    "observed_objective": None if not (
                        feasible or infeasible_complete
                    ) else observed,
                    "feasible_completed": feasible,
                    "infeasible_objective_sample": infeasible_complete,
                    "best_so_far_j": best,
                    "best_so_far_unit": best_unit,
                    "independent_confidence_sample": False,
                }
            )
        best_so_far[method] = series

    methods_summary = {}
    for method in METHODS:
        method_slots = slots[method]
        consumed = [slot for slot in method_slots if slot["budget_consumed"]]
        completed = [
            slot
            for slot in method_slots
            if slot["slot_state"].startswith("completed_")
        ]
        feasible = [slot for slot in method_slots if slot["slot_state"] == "completed_feasible"]
        failed = [slot for slot in method_slots if slot["slot_state"] == "failed"]
        stopped = any(slot["slot_state"] == "stopped_before_ei" for slot in method_slots)
        methods_summary[method] = {
            "registered_units": len(consumed),
            "budget_spent_pairs": len(consumed),
            "completed_pairs": len(completed),
            "feasible_pairs": len(feasible),
            "failed_pairs": len(failed),
            "incomplete_pairs": sum(
                1 for slot in method_slots if slot["slot_state"] == "incomplete_running_or_pending"
            ),
            "not_run_slots": sum(1 for slot in method_slots if slot["slot_state"] == "not_run"),
            "stopped_before_ei_slots": sum(
                1 for slot in method_slots if slot["slot_state"] == "stopped_before_ei"
            ),
            "stopped_before_ei": stopped,
            "full_24_budget_spent": len(consumed) == TOTAL_UNITS,
            "slots": method_slots,
        }

    frozen_raw = snapshot["metadata"].get("frozen")
    equal_budget_freeze = frozen_raw is not None
    initial_triples = []
    for unit in range(INITIAL_UNITS):
        row = {
            "unit": unit,
            "shared_mechanical_triple": all(
                slots[method][unit]["shared_initial_triple"] for method in METHODS
            ),
            "methods": {},
        }
        for method in METHODS:
            slot = slots[method][unit]
            row["methods"][method] = {
                "slot_state": slot["slot_state"],
                "parameters": slot["parameters"],
                "mechanical": slot["mechanical"],
                "nominal_feasible": slot["nominal_feasible"],
                "disturbed_guards_ok": slot["disturbed_guards_ok"],
                "pair_feasible": slot["pair_feasible"],
                "objective": slot["objective"],
                "objective_components": slot["objective_components"],
                "nominal_descriptors": slot["members"]["nominal"].get("descriptors"),
                "disturbed_descriptors": slot["members"]["disturbed"].get("descriptors"),
                "dropped_because_infeasible": False,
            }
        initial_triples.append(row)

    protocol = state.get("campaign_protocol") if isinstance(state.get("campaign_protocol"), Mapping) else {}
    report = {
        "schema": REPORT_SCHEMA,
        "version": 1,
        "snapshot": {
            "observed_utc": snapshot["observed_utc"],
            "source": snapshot["source"],
            "campaign_root": str(campaign_root),
            "campaign_schema": state.get("schema"),
            "campaign_protocol_sha256": state.get("campaign_protocol_sha256"),
            "campaign_protocol": protocol,
            "config_sha256": state.get("config_sha256"),
            "config_path_recorded": state.get("config_path"),
            "ledger_bindings": state.get("ledger_bindings"),
            "training_cell_id": protocol.get("training_cell_id"),
            "selection_contract_id": protocol.get("selection_contract_id"),
            "source_hashes_recorded": protocol.get("source_hashes"),
            "execution_identities_recorded": protocol.get("execution_identities"),
            "inflight": inflight,
            "frozen": frozen_raw,
            "claim_scope": state.get("claim_scope") or CLAIM_SCOPE,
            "formal_campaign_complete": bool(state.get("formal_campaign_complete")),
            "holdout_implemented": bool(state.get("holdout_implemented")),
            "hardware_qualified": bool(state.get("hardware_qualified")),
            "validation_executed": False,
            "physical_executed": False,
            "cross_campaign_pooling": False,
        },
        "schedule": {
            "total_units_per_method": TOTAL_UNITS,
            "initial_units": INITIAL_UNITS,
            "bayesian_ei_units": EI_UNITS,
            "repeat_incumbent_units": REPEAT_UNITS,
            "stop_before_ei_if_initial_feasible_lt": STOP_BEFORE_EI_MIN_FEASIBLE,
            "unit_definition": "one_nominal_disturbed_pair",
            "literal_repeats": True,
            "failed_unit_consumes_ordinal": True,
        },
        "methods": methods_summary,
        "initial_matched_triples": initial_triples,
        "best_so_far": best_so_far,
        "artifact_digest_manifest": digest_manifest,
        "equal_budget_freeze": equal_budget_freeze,
        "full_budget_three_method_freeze": equal_budget_freeze
        and all(methods_summary[method]["full_24_budget_spent"] for method in METHODS),
        "interpretation": {
            "finite_objective_is_not_feasible_incumbent": True,
            "failed_or_interrupted_consumes_budget": True,
            "no_placeholder_zero_for_missing_data": True,
            "initial_units_are_shared_triples_only_if_mechanical_parameters_match": True,
            "ei_rows_are_not_coefficient_matched_pairs": True,
            "literal_repeats_are_not_independent_confidence_samples": True,
            "best_so_far_uses_feasible_completed_pairs_only": True,
            "best_so_far_stops_at_actual_observations": True,
            "incomplete_running_or_pending_does_not_extend_best_so_far": True,
            "contradictory_complete_flags_fail_closed": True,
            "infeasible_objective_samples_are_marked_and_not_selected": True,
            "low_load_duration_is_load_below_1N_not_geometric_contact_loss": True,
            "ledger_J_is_reported_alongside_descriptors_not_as_precision_or_safety": True,
            "no_ci_winner_p_value_pooled_validation_or_superiority_claim": True,
            "plots_are_not_independent_validation_or_physical_results": True,
            "asymmetric_completion_is_not_proposal_superiority": True,
            "training_context": TRAINING_CONTEXT,
            "claim_scope": state.get("claim_scope") or CLAIM_SCOPE,
            "main_adjudication": "report/yield-round8-main-review-v1/main-adjudication.md",
        },
    }
    return _json_safe(report)


def _slot_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for method in METHODS:
        for slot in report["methods"][method]["slots"]:
            components = slot.get("objective_components") or {}
            recovery = slot.get("recovery") or {}
            rows.append(
                {
                    "method": method,
                    "unit": slot["unit"],
                    "scheduled_phase": slot["scheduled_phase"],
                    "literal_repeat": slot["literal_repeat"],
                    "slot_state": slot["slot_state"],
                    "budget_consumed": slot["budget_consumed"],
                    "shared_initial_triple": slot["shared_initial_triple"],
                    "coefficient_matched_pair": slot["coefficient_matched_pair"],
                    "nominal_status": slot["members"]["nominal"]["status"],
                    "disturbed_status": slot["members"]["disturbed"]["status"],
                    "nominal_feasible": slot["nominal_feasible"],
                    "disturbed_guards_ok": slot["disturbed_guards_ok"],
                    "pair_feasible": slot["pair_feasible"],
                    "objective": slot["objective"],
                    "J": components.get("J", slot["objective"]),
                    "Jload": components.get("Jload"),
                    "Jpath": components.get("Jpath"),
                    "Jatt": components.get("Jatt"),
                    "recovery_s": recovery.get("recovery_s"),
                    "residual_displacement_m": recovery.get("residual_displacement_m"),
                    "postrelease_max_offset_m": recovery.get("postrelease_max_offset_m"),
                    "m": (slot.get("mechanical") or {}).get("m"),
                    "mu": (slot.get("mechanical") or {}).get("mu"),
                    "g": (slot.get("mechanical") or {}).get("g"),
                }
            )
    return rows


def _member_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for method in METHODS:
        for slot in report["methods"][method]["slots"]:
            for condition in CONDITIONS:
                member = slot["members"][condition]
                descriptors = member.get("descriptors") or {}
                row = {
                    "method": method,
                    "unit": slot["unit"],
                    "condition": condition,
                    "scheduled_phase": slot["scheduled_phase"],
                    "slot_state": slot["slot_state"],
                    "attempt_id": member.get("attempt_id"),
                    "status": member.get("status"),
                    "artifact_parsed": member.get("artifact_parsed"),
                    "artifact_sha256": member.get("artifact_sha256"),
                    "reason": member.get("reason"),
                }
                for key in DESCRIPTOR_KEYS:
                    row[key] = descriptors.get(key)
                row["low_load_duration_definition"] = descriptors.get(
                    "low_load_duration_definition"
                )
                rows.append(row)
    return rows


def _triple_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for triple in report["initial_matched_triples"]:
        for method in METHODS:
            item = triple["methods"][method]
            row = {
                "unit": triple["unit"],
                "method": method,
                "shared_mechanical_triple": triple["shared_mechanical_triple"],
                "slot_state": item["slot_state"],
                "nominal_feasible": item["nominal_feasible"],
                "disturbed_guards_ok": item["disturbed_guards_ok"],
                "pair_feasible": item["pair_feasible"],
                "objective": item["objective"],
                "dropped_because_infeasible": item["dropped_because_infeasible"],
                "m": (item.get("mechanical") or {}).get("m"),
                "mu": (item.get("mechanical") or {}).get("mu"),
                "g": (item.get("mechanical") or {}).get("g"),
            }
            for prefix, blob in (
                ("nominal", item.get("nominal_descriptors") or {}),
                ("disturbed", item.get("disturbed_descriptors") or {}),
            ):
                for key in DESCRIPTOR_KEYS:
                    row[f"{prefix}_{key}"] = blob.get(key)
            rows.append(row)
    return rows


def _best_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for method in METHODS:
        rows.extend(report["best_so_far"][method])
    return rows


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) if row.get(key) is not None else "" for key in fieldnames})


def _bool_label(value: Any) -> str:
    if value is True:
        return "feasible"
    if value is False:
        return "infeasible"
    return "unobserved"


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float):
            return f"{value:.9g}"
        return str(value)

    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(cell(item) for item in row) + " |")
    return "\n".join(lines)


def render_markdown(report: Mapping[str, Any]) -> str:
    snap = report["snapshot"]
    lines = [
        "# Yield training-result report",
        "",
        TRAINING_CONTEXT + ".",
        "",
        snap.get("claim_scope") or CLAIM_SCOPE,
        "",
        "Main adjudication in `report/yield-round8-main-review-v1/main-adjudication.md` governs interpretation. "
        "A finite objective is not a feasible incumbent. Failed or interrupted units consume budget. "
        "SFC stopping before EI is a registered campaign outcome, not an equal-budget freeze or a proposal-superiority result.",
        "",
        "## Snapshot identity",
        "",
        _markdown_table(
            ("field", "value"),
            [
                ("observed_utc", snap["observed_utc"]),
                ("source", snap["source"]),
                ("campaign_root", snap["campaign_root"]),
                ("campaign_protocol_sha256", snap["campaign_protocol_sha256"]),
                ("config_sha256", snap["config_sha256"]),
                ("training_cell_id", snap["training_cell_id"]),
                ("selection_contract_id", snap["selection_contract_id"]),
                ("inflight", ", ".join(snap["inflight"]) if snap["inflight"] else ""),
                ("frozen", snap["frozen"] is not None),
                ("equal_budget_freeze", report["equal_budget_freeze"]),
                ("validation_executed", snap["validation_executed"]),
                ("physical_executed", snap["physical_executed"]),
            ],
        ),
        "",
        "## Budget and terminal states",
        "",
        "Each method has 24 ordinal pair slots. Unspent SFC ordinals after an initial-feasibility stop are `stopped_before_ei`, not a carried best-so-far line.",
        "",
    ]
    budget_rows = []
    for method in METHODS:
        summary = report["methods"][method]
        budget_rows.append(
            (
                method,
                summary["budget_spent_pairs"],
                summary["completed_pairs"],
                summary["feasible_pairs"],
                summary["failed_pairs"],
                summary["incomplete_pairs"],
                summary["stopped_before_ei_slots"],
                summary["not_run_slots"],
                summary["stopped_before_ei"],
                summary["full_24_budget_spent"],
            )
        )
    lines.append(
        _markdown_table(
            (
                "method",
                "budget spent",
                "completed",
                "feasible",
                "failed",
                "incomplete",
                "stopped-before-EI slots",
                "not run",
                "stopped before EI",
                "full 24 spent",
            ),
            budget_rows,
        )
    )
    lines.extend(["", "## All 72 ordinal slots", ""])
    slot_table = []
    for method in METHODS:
        for slot in report["methods"][method]["slots"]:
            slot_table.append(
                (
                    method,
                    slot["unit"],
                    slot["scheduled_phase"],
                    slot["slot_state"],
                    slot["pair_feasible"],
                    slot["nominal_feasible"],
                    slot["disturbed_guards_ok"],
                    slot["objective"],
                    slot["literal_repeat"],
                    slot["shared_initial_triple"],
                )
            )
    lines.append(
        _markdown_table(
            (
                "method",
                "unit",
                "phase",
                "state",
                "pair feasible",
                "nominal feasible",
                "disturbed guards",
                "J (ledger)",
                "literal repeat",
                "shared initial triple",
            ),
            slot_table,
        )
    )
    lines.extend(
        [
            "",
            "Ledger J components are reported with absolute descriptors. They are not asserted to be directly optimized precision or safety.",
            "",
            "## Initial units 0–7",
            "",
            "Shared-triple comparison only where the registered mechanical `(m, mu, g)` values actually match. Infeasible rows are retained.",
            "",
        ]
    )
    triple_table = []
    for triple in report["initial_matched_triples"]:
        for method in METHODS:
            item = triple["methods"][method]
            nom = item.get("nominal_descriptors") or {}
            dist = item.get("disturbed_descriptors") or {}
            triple_table.append(
                (
                    triple["unit"],
                    method,
                    item["slot_state"],
                    _bool_label(item["nominal_feasible"]),
                    _bool_label(item["disturbed_guards_ok"]),
                    item["objective"],
                    nom.get("force_mae_n"),
                    nom.get("path_rms_m"),
                    nom.get("progress_ratio"),
                    dist.get("force_mae_n"),
                    dist.get("path_rms_m"),
                    dist.get("progress_ratio"),
                    dist.get("low_load_duration_s"),
                    triple["shared_mechanical_triple"],
                )
            )
    lines.append(
        _markdown_table(
            (
                "unit",
                "method",
                "state",
                "nominal",
                "disturbed guards",
                "J",
                "nom force MAE (N)",
                "nom path RMS (m)",
                "nom progress",
                "dist force MAE (N)",
                "dist path RMS (m)",
                "dist progress",
                "dist low-load <1N (s)",
                "shared triple",
            ),
            triple_table,
        )
    )
    lines.extend(
        [
            "",
            f"Low-load duration: {LOW_LOAD_DEFINITION}.",
            "",
            "## Best-so-far J (feasible completed pairs only)",
            "",
            "Infeasible complete objectives are listed as samples and do not update the incumbent curve. "
            "Literal repeats are labeled and are not independent confidence samples. No CI, winner, or p-value.",
            "",
        ]
    )
    best_table = []
    for method in METHODS:
        for row in report["best_so_far"][method]:
            best_table.append(
                (
                    method,
                    row["unit"],
                    row["scheduled_phase"],
                    row["slot_state"],
                    row["observed_objective"],
                    row["feasible_completed"],
                    row["infeasible_objective_sample"],
                    row["best_so_far_j"],
                    row["literal_repeat"],
                )
            )
    lines.append(
        _markdown_table(
            (
                "method",
                "unit",
                "phase",
                "state",
                "observed J",
                "feasible",
                "infeasible sample",
                "best-so-far J",
                "literal repeat",
            ),
            best_table,
        )
    )
    lines.extend(
        [
            "",
            "## Artifact digest manifest",
            "",
            "Complete members are hashed before parse. Running members are never read.",
            "",
        ]
    )
    manifest_rows = [
        (
            item.get("attempt_id"),
            item.get("status"),
            item.get("artifact_sha256"),
            item.get("bytes"),
            item.get("parsed"),
        )
        for item in report["artifact_digest_manifest"]
    ]
    lines.append(
        _markdown_table(("attempt", "status", "sha256", "bytes", "parsed"), manifest_rows)
    )
    lines.extend(
        [
            "",
            "## Limits",
            "",
            "- No cross-campaign pooling, reserved validation, or physical trial is included.",
            "- EI rows are method-specific training trajectories, not coefficient-matched pairs.",
            "- Plots and tables in this directory are training-only descriptors.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _method_color(method: str) -> str:
    return {"SFC": "#4c4c4c", "DSFC": "#2878a0", "MSFC": "#b66c32"}[method]



def require_matplotlib():
    """Import matplotlib for PNG/PDF. Fail before any output write if unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as error:
        raise YieldTrainingReportError(
            "matplotlib and numpy are required for PNG/PDF output; "
            f"unavailable: {error}"
        ) from error
    if not hasattr(plt, "subplots") or not hasattr(np, "asarray"):
        raise YieldTrainingReportError(
            "matplotlib and numpy are required for PNG/PDF output"
        )
    return plt


def write_plots(report: Mapping[str, Any], plot_dir: Path) -> list[str]:
    plt = require_matplotlib()
    plot_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    fig, ax = plt.subplots(figsize=(10.5, 5.5), constrained_layout=True)
    for method in METHODS:
        series = report["best_so_far"][method]
        if not series:
            continue
        xs = [row["unit"] for row in series]
        best = [row["best_so_far_j"] if row["best_so_far_j"] is not None else float("nan") for row in series]
        ax.plot(
            xs,
            best,
            color=_method_color(method),
            label=f"{method} best-so-far (feasible completed)",
            linewidth=2,
        )
        infeasible_x = [row["unit"] for row in series if row["infeasible_objective_sample"]]
        infeasible_y = [row["observed_objective"] for row in series if row["infeasible_objective_sample"]]
        if infeasible_x:
            ax.scatter(
                infeasible_x, infeasible_y, facecolors="none", edgecolors=_method_color(method),
                marker="o", s=36, label=f"{method} infeasible complete J", zorder=3,
            )
        repeats_x = [
            row["unit"] for row in series
            if row["literal_repeat"] and row["observed_objective"] is not None
        ]
        repeats_y = [
            row["observed_objective"] for row in series
            if row["literal_repeat"] and row["observed_objective"] is not None
        ]
        if repeats_x:
            ax.scatter(
                repeats_x, repeats_y, color=_method_color(method), marker="s", s=28,
                label=f"{method} literal repeat (not an independent CI sample)", zorder=4,
            )
        feasible_x = [
            row["unit"] for row in series
            if row["feasible_completed"] and row["observed_objective"] is not None
        ]
        feasible_y = [
            row["observed_objective"] for row in series
            if row["feasible_completed"] and row["observed_objective"] is not None
        ]
        if feasible_x:
            ax.scatter(feasible_x, feasible_y, color=_method_color(method), marker=".", s=40, zorder=3)
        failed_x = [row["unit"] for row in series if row["slot_state"] == "failed"]
        failed_y = [row["best_so_far_j"] for row in series if row["slot_state"] == "failed"]
        if any(value is not None for value in failed_y):
            ax.scatter(
                [x for x, y in zip(failed_x, failed_y) if y is not None],
                [y for y in failed_y if y is not None],
                color=_method_color(method), marker="x", s=36,
                label=f"{method} failed consumed unit", zorder=3,
            )
    ax.set_xlabel("ordinal pair unit (0-23 schedule; line stops at observed units)")
    ax.set_ylabel("J (ledger objective, seconds)")
    ax.set_title("Best-so-far J from feasible completed training pairs\n" + TRAINING_CONTEXT)
    ax.set_xticks(list(range(0, TOTAL_UNITS, 2)))
    ax.set_xlim(-0.5, TOTAL_UNITS - 0.5)
    ax.grid(True, alpha=0.25)
    handles, labels = ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    ax.legend(unique.values(), unique.keys(), fontsize=8, loc="best")
    ax.text(
        0.0, -0.18,
        "Unspent and incomplete/running units are omitted from this curve. Infeasible J is plotted and not selected.",
        transform=ax.transAxes, fontsize=8,
    )
    for ext in ("png", "pdf"):
        path = plot_dir / f"best_so_far_j.{ext}"
        fig.savefig(path, dpi=140)
        written.append(str(path))
    plt.close(fig)

    panels = (
        ("force_mae_n", "force MAE (N)"),
        ("path_rms_m", "path RMS (m)"),
        ("progress_ratio", "progress ratio"),
        ("attitude_rms_rad", "attitude RMS (rad)"),
        ("load_min_n", "load minimum (N)"),
        ("low_load_duration_s", "low-load duration load<1N (s)"),
    )
    for condition, filename in (("nominal", "initial_triples_nominal"), ("disturbed", "initial_triples_disturbed")):
        fig, axes = plt.subplots(2, 3, figsize=(12.5, 7.2), constrained_layout=True)
        for axis, (key, title) in zip(axes.flat, panels):
            width = 0.24
            for index, method in enumerate(METHODS):
                xs = []
                ys = []
                infeasible = []
                for triple in report["initial_matched_triples"]:
                    item = triple["methods"][method]
                    descriptors = item.get(f"{condition}_descriptors") or {}
                    value = descriptors.get(key)
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        continue
                    parsed = float(value)
                    if not math.isfinite(parsed):
                        continue
                    xs.append(triple["unit"] + (index - 1) * width)
                    ys.append(parsed)
                    feasible = (
                        item["nominal_feasible"] if condition == "nominal" else item["disturbed_guards_ok"]
                    )
                    infeasible.append(feasible is False)
                bars = axis.bar(xs, ys, width=width, label=method, color=_method_color(method))
                for bar, flagged in zip(bars, infeasible):
                    if flagged:
                        bar.set_edgecolor("#6b1d1d")
                        bar.set_linewidth(1.2)
                        bar.set_hatch("///")
            axis.set_title(title, fontsize=10)
            axis.set_xticks(range(INITIAL_UNITS))
            axis.set_xlabel("initial unit")
            axis.grid(axis="y", alpha=0.25)
        axes[0, 0].legend(fontsize=8)
        fig.suptitle(
            f"Initial units 0-7 {condition} descriptors (infeasible rows kept, dark edge)\n" + TRAINING_CONTEXT,
            fontsize=11,
        )
        for ext in ("png", "pdf"):
            path = plot_dir / f"{filename}.{ext}"
            fig.savefig(path, dpi=140)
            written.append(str(path))
        plt.close(fig)
    return written


def _refuse_output(campaign_root: Path, output: Path) -> Path:
    campaign_root = campaign_root.resolve()
    output = Path(output).expanduser()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output directory: {output}")
    parent = output.parent.resolve()
    target = (parent / output.name).resolve()
    if target == campaign_root or campaign_root in target.parents:
        raise YieldTrainingReportError("refusing to write inside the campaign root")
    return output


def write_training_report(campaign: Path | str, output: Path | str) -> dict[str, Any]:
    campaign_root = Path(campaign).expanduser().resolve()
    output_root = _refuse_output(campaign_root, Path(output))
    snapshot = read_readonly_snapshot(campaign_root)
    report = build_report_from_snapshot(snapshot, campaign_root)
    require_matplotlib()
    output_root.mkdir(parents=False)
    (output_root / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (output_root / "report.md").write_text(render_markdown(report), encoding="utf-8")
    _write_csv(output_root / "slots.csv", _slot_rows(report))
    _write_csv(output_root / "members.csv", _member_rows(report))
    _write_csv(output_root / "initial_triples.csv", _triple_rows(report))
    _write_csv(output_root / "best_so_far.csv", _best_rows(report))
    _write_csv(output_root / "artifact_digest_manifest.csv", report["artifact_digest_manifest"])
    plots = write_plots(report, output_root / "plots")
    report["output_files"] = {
        "report_json": "report.json",
        "report_markdown": "report.md",
        "plots": [str(Path(path).relative_to(output_root)) for path in plots],
    }
    (output_root / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only bounded training-result report. Does not launch training, "
            "validation, simulation, or hardware."
        )
    )
    parser.add_argument(
        "--campaign",
        type=Path,
        help="campaign root with campaign.json and campaign.sqlite (read-only)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="new output directory; refused if it already exists",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.campaign is None and args.output is None:
        parser.print_help()
        return 0
    if args.campaign is None or args.output is None:
        parser.error("both --campaign and --output are required")
        return 2
    write_training_report(args.campaign, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
