#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import canonical_wrench_contract as contract  # noqa: E402


REQUIRED_CONTACT_ROW_FIELDS = (
    "stamp_s",
    "collision1",
    "collision2",
    "position_m",
    "normal",
    "contact_count",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _wrench_trace(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {
            "present": False,
            "source": None,
            "claim_tier": None,
            "rows": [],
            "evidence_fields_present": {},
            "adapter_verified_gazebo_contact_wrench": False,
        }
    if payload.get("schema") == contract.TRACE_SCHEMA:
        return {
            "present": True,
            "source": payload.get("force_source"),
            "claim_tier": payload.get("claim_tier"),
            "rows": payload.get("rows") or [],
            "evidence_fields_present": _trace_evidence_fields(payload),
            "adapter_verified_gazebo_contact_wrench": False,
        }
    if isinstance(payload.get("wrench_trace"), dict):
        trace = payload["wrench_trace"]
        return {
            "present": True,
            "source": trace.get("force_source"),
            "claim_tier": trace.get("claim_tier"),
            "rows": trace.get("rows") or [],
            "evidence_fields_present": _trace_evidence_fields(trace),
            "adapter_verified_gazebo_contact_wrench": _adapter_verified_gazebo_contact_wrench(payload, trace),
        }
    return {
        "present": True,
        "source": payload.get("force_source"),
        "claim_tier": payload.get("claim_tier"),
        "rows": [],
        "evidence_fields_present": payload.get("evidence_fields_present") or {},
        "observed_counts": payload.get("observed_counts") or {},
        "adapter_verified_gazebo_contact_wrench": False,
    }


def _adapter_verified_gazebo_contact_wrench(payload: dict[str, Any], trace: dict[str, Any]) -> bool:
    return (
        payload.get("schema") == "ur10e_gazebo_contact_wrench_adapter_report_v1"
        and payload.get("trace_written") is True
        and payload.get("claim_tier") == "physical Gazebo collision/contact physics"
        and payload.get("force_source") == contract.SOURCE_GAZEBO_CONTACT
        and int(payload.get("native_wrench_row_count") or 0) > 0
        and int(payload.get("verified_native_wrench_row_count") or 0) > 0
        and not payload.get("blockers")
        and trace.get("force_source") == contract.SOURCE_GAZEBO_CONTACT
        and trace.get("claim_tier") == "physical Gazebo collision/contact physics"
        and bool(trace.get("rows"))
    )


def _trace_evidence_fields(trace: dict[str, Any]) -> dict[str, bool]:
    rows = trace.get("rows") or []
    first = rows[0] if rows else {}
    header = first.get("header") if isinstance(first.get("header"), dict) else {}
    return {
        "stamp": "stamp_s" in header or "t_s" in first,
        "frame_id": "frame_id" in header,
        "source": bool(first.get("source") or trace.get("force_source")),
        "status": "status" in first,
        "baseline": bool(first.get("baseline_policy") or trace.get("baseline_policy")),
        "log_evidence": bool(rows),
    }


def _contact_collision_names(p2_inventory_payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for candidate in p2_inventory_payload.get("collision_candidates") or []:
        for name in candidate.get("contact_collision_names") or []:
            names.add(str(name))
    return names


def _validate_contact_pair_log(
    contact_pair_payload: dict[str, Any] | None,
    *,
    contact_collision_names: set[str],
) -> dict[str, Any]:
    if not contact_pair_payload:
        return {
            "present": False,
            "schema": None,
            "row_count": 0,
            "matching_row_count": 0,
            "required_fields": list(REQUIRED_CONTACT_ROW_FIELDS),
            "issues": ["missing_contact_pair_log"],
            "evidence": False,
        }

    rows = contact_pair_payload.get("rows") or []
    issues: list[str] = []
    matching_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        missing = [field for field in REQUIRED_CONTACT_ROW_FIELDS if field not in row]
        if missing:
            issues.append(f"row_{index}_missing:" + ",".join(missing))
            continue
        if _contact_row_matches(row, contact_collision_names=contact_collision_names):
            matching_rows.append(row)
    if contact_pair_payload.get("schema") != "ur10e_gazebo_contact_pair_log_v1":
        issues.append("schema:not_ur10e_gazebo_contact_pair_log_v1")
    if not matching_rows:
        issues.append("no_matching_eoat_surface_contact_pair")

    return {
        "present": True,
        "schema": contact_pair_payload.get("schema"),
        "source": contact_pair_payload.get("source"),
        "row_count": len(rows),
        "matching_row_count": len(matching_rows),
        "required_fields": list(REQUIRED_CONTACT_ROW_FIELDS),
        "issues": issues,
        "evidence": not issues,
        "first_matching_row": matching_rows[0] if matching_rows else None,
    }


def _contact_row_matches(row: dict[str, Any], *, contact_collision_names: set[str]) -> bool:
    collisions = (str(row.get("collision1") or ""), str(row.get("collision2") or ""))
    joined = " ".join(collisions)
    has_eoat = any(name in joined for name in contact_collision_names)
    has_surface = "contact_surface" in joined or "surface::collision" in joined
    try:
        has_count = int(row.get("contact_count") or 0) > 0
    except (TypeError, ValueError):
        has_count = False
    return has_eoat and has_surface and has_count


def _correlate_wrench_to_contact(wrench: dict[str, Any], contact_pair: dict[str, Any]) -> dict[str, Any]:
    if wrench.get("source") != contract.SOURCE_GAZEBO_CONTACT:
        return {
            "evidence": False,
            "status": "blocked_wrench_source_not_gazebo_contact",
            "matched_row_count": 0,
            "max_time_delta_s": None,
        }
    if not wrench.get("adapter_verified_gazebo_contact_wrench"):
        return {
            "evidence": False,
            "status": "blocked_wrench_not_adapter_verified_gazebo_contact",
            "matched_row_count": 0,
            "max_time_delta_s": None,
        }
    if not contact_pair.get("evidence"):
        return {
            "evidence": False,
            "status": "blocked_contact_pair_log_missing_or_invalid",
            "matched_row_count": 0,
            "max_time_delta_s": None,
        }

    contact_stamp = float(contact_pair["first_matching_row"]["stamp_s"])
    matches = []
    for row in wrench.get("rows") or []:
        stamp = _row_stamp(row)
        if stamp is None:
            continue
        normal_load_n = float(row.get("normal_load_n") or 0.0)
        status = str(row.get("status") or "")
        contact_state = str(row.get("contact_state") or "")
        delta = abs(stamp - contact_stamp)
        if delta <= 0.02 and normal_load_n > 0.0 and status == "valid" and contact_state == "contact":
            matches.append({"stamp_s": stamp, "time_delta_s": delta, "normal_load_n": normal_load_n})

    return {
        "evidence": bool(matches),
        "status": "correlated" if matches else "blocked_no_timestamped_wrench_contact_overlap",
        "matched_row_count": len(matches),
        "max_time_delta_s": max((match["time_delta_s"] for match in matches), default=None),
        "first_match": matches[0] if matches else None,
    }


def _row_stamp(row: dict[str, Any]) -> float | None:
    header = row.get("header")
    if isinstance(header, dict) and "stamp_s" in header:
        return float(header["stamp_s"])
    if "t_s" in row:
        return float(row["t_s"])
    return None


def build_audit(
    *,
    p2_inventory_payload: dict[str, Any],
    wrench_payload: dict[str, Any] | None,
    contact_pair_payload: dict[str, Any] | None,
    generated_at: str | None = None,
    p2_inventory_path: str | None = None,
    wrench_path: str | None = None,
    contact_pair_path: str | None = None,
) -> dict[str, Any]:
    wrench = _wrench_trace(wrench_payload)
    contact_pair = _validate_contact_pair_log(
        contact_pair_payload,
        contact_collision_names=_contact_collision_names(p2_inventory_payload),
    )
    correlation = _correlate_wrench_to_contact(wrench, contact_pair)
    eoat_collision_count = int(p2_inventory_payload.get("current_eoat_collision_count") or 0)
    eoat_collision_body_audit_passed = eoat_collision_count > 0
    contact_pair_evidence = bool(contact_pair["evidence"])
    wrench_contact_correlation = bool(correlation["evidence"])
    force_contact_physics_proven = (
        eoat_collision_body_audit_passed
        and contact_pair_evidence
        and wrench_contact_correlation
        and wrench.get("source") == contract.SOURCE_GAZEBO_CONTACT
        and wrench.get("adapter_verified_gazebo_contact_wrench")
    )
    blockers = _known_blockers(
        eoat_collision_body_audit_passed=eoat_collision_body_audit_passed,
        contact_pair_evidence=contact_pair_evidence,
        wrench_contact_correlation=wrench_contact_correlation,
        wrench_source=wrench.get("source"),
        adapter_verified_gazebo_contact_wrench=bool(wrench.get("adapter_verified_gazebo_contact_wrench")),
    )
    return {
        "schema": "ur10e_gazebo_p2_contact_correlation_audit_v1",
        "generated_at": generated_at or _now_iso(),
        "mode": "offline_no_live_evidence_audit",
        "goal_lineage": "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md",
        "claim_tier": (
            "physical Gazebo collision/contact physics" if force_contact_physics_proven else "visual_only"
        ),
        "referenced_wrench_claim_tier": wrench.get("claim_tier"),
        "allowed_claim": (
            "physical Gazebo collision/contact physics"
            if force_contact_physics_proven
            else "visual_only contact-correlation readiness audit only"
        ),
        "forbidden_claim": (
            "real bench/live contact; physical Gazebo contact physics unless all gate booleans are true"
        ),
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "inputs": {
            "p2_inventory_path": p2_inventory_path,
            "wrench_path": wrench_path,
            "contact_pair_path": contact_pair_path,
        },
        "p2_collision_inventory": {
            "claim_tier": p2_inventory_payload.get("claim_tier"),
            "current_eoat_collision_count": eoat_collision_count,
            "contact_collision_names": sorted(_contact_collision_names(p2_inventory_payload)),
            "force_contact_physics_proven": bool(p2_inventory_payload.get("force_contact_physics_proven")),
        },
        "wrench_evidence": wrench,
        "contact_pair_log_evidence": contact_pair,
        "wrench_contact_correlation": correlation,
        "physical_gazebo_contact_gate": {
            "eoat_collision_count": eoat_collision_count,
            "eoat_collision_body_audit_passed": eoat_collision_body_audit_passed,
            "collision_count_proven": contact_pair_evidence,
            "contact_pair_log_evidence": contact_pair_evidence,
            "wrench_contact_correlation": wrench_contact_correlation,
            "wrench_source_is_gazebo_contact": wrench.get("source") == contract.SOURCE_GAZEBO_CONTACT,
            "adapter_verified_gazebo_contact_wrench": bool(wrench.get("adapter_verified_gazebo_contact_wrench")),
            "force_contact_physics_proven": force_contact_physics_proven,
            "status": "proven" if force_contact_physics_proven else "blocked_not_proven",
        },
        "required_contact_log_schema": {
            "schema": "ur10e_gazebo_contact_pair_log_v1",
            "row_fields": list(REQUIRED_CONTACT_ROW_FIELDS),
            "timestamp_unit": "seconds",
            "required_pair": "one EOAT contact collision name paired with one contact_surface collision name",
        },
        "known_blockers": blockers,
    }


def _known_blockers(
    *,
    eoat_collision_body_audit_passed: bool,
    contact_pair_evidence: bool,
    wrench_contact_correlation: bool,
    wrench_source: Any,
    adapter_verified_gazebo_contact_wrench: bool,
) -> list[str]:
    blockers: list[str] = []
    if not eoat_collision_body_audit_passed:
        blockers.append("eoat_collision_count=0")
    if not contact_pair_evidence:
        blockers.append("no_eoat_contact_pair_log_evidence")
    if not wrench_contact_correlation:
        blockers.append("no_wrench_contact_correlation")
    if wrench_source != contract.SOURCE_GAZEBO_CONTACT:
        blockers.append("wrench_source_not_gazebo_contact")
    elif not adapter_verified_gazebo_contact_wrench:
        blockers.append("wrench_not_adapter_verified_gazebo_contact")
    if blockers:
        blockers.append("force_contact_physics_proven=false")
    return blockers


def write_audit(
    output_dir: Path,
    *,
    p2_inventory_path: Path,
    wrench_path: Path | None,
    contact_pair_path: Path | None,
    generated_at: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "p2_contact_correlation_audit.json"
    payload = build_audit(
        p2_inventory_payload=_load_json(p2_inventory_path) or {},
        wrench_payload=_load_json(wrench_path),
        contact_pair_payload=_load_json(contact_pair_path),
        generated_at=generated_at,
        p2_inventory_path=str(p2_inventory_path),
        wrench_path=str(wrench_path) if wrench_path is not None else None,
        contact_pair_path=str(contact_pair_path) if contact_pair_path is not None else None,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build fail-closed P2 contact-pair/wrench correlation audit.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--p2-inventory", type=Path, required=True)
    parser.add_argument("--wrench", type=Path, default=None)
    parser.add_argument("--contact-pair-log", type=Path, default=None)
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args(argv)
    path = write_audit(
        args.output_dir,
        p2_inventory_path=args.p2_inventory,
        wrench_path=args.wrench,
        contact_pair_path=args.contact_pair_log,
        generated_at=args.generated_at,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
