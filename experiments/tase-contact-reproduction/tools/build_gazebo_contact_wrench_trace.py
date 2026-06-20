#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import canonical_wrench_contract as contract  # noqa: E402


REPORT_SCHEMA = "ur10e_gazebo_contact_wrench_adapter_report_v1"
REPORT_FILENAME = "p2_gazebo_contact_wrench_adapter_report.json"
TRACE_FILENAME = "p2_gazebo_contact_wrench_trace.json"
DEFAULT_SOURCE_TOPIC = "/ur10e/contact/gazebo/p2_contact_witness/wrench"
BLOCKED_CLAIM_TIER = "visual_only"
PHYSICAL_GAZEBO_CLAIM_TIER = "physical Gazebo collision/contact physics"
ALLOWED_NATIVE_WRENCH_SOURCE = "gazebo_contact_message_wrench"
ALLOWED_NATIVE_WRENCH_SCHEMAS = {
    "ignition.msgs.Contact.contact.wrench",
    "gz.msgs.Contact.contact.wrench",
}
REQUIRED_NATIVE_FIELDS = (
    "native_gazebo_contact_wrench.force_n",
    "native_gazebo_contact_wrench.source=gazebo_contact_message_wrench",
    "native_gazebo_contact_wrench.source_schema in ignition.msgs.Contact.contact.wrench,gz.msgs.Contact.contact.wrench",
    "native_gazebo_contact_wrench.force_source_class=gazebo_contact",
    "native_gazebo_contact_wrench.measured_contact_wrench=true",
    "native_gazebo_contact_wrench.commanded_force=false",
    "native_gazebo_contact_wrench.frame_id=base",
    "native_gazebo_contact_wrench.frame_transform_evidence.source/from_frame/to_frame/stamp_s/artifact_path",
    "native_gazebo_contact_wrench.status=valid",
    "native_gazebo_contact_wrench.wrench_stamp_s",
    "native_gazebo_contact_wrench.wrench_stamp_evidence=true",
    "stamp_evidence=true",
    "stamp_s",
    "normal",
    "contact_count",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _vec3(value: Any, *, label: str) -> tuple[float, float, float]:
    if isinstance(value, dict):
        return (
            float(value.get("x") or 0.0),
            float(value.get("y") or 0.0),
            float(value.get("z") or 0.0),
        )
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three values")
    return (float(value[0]), float(value[1]), float(value[2]))


def _dot3(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _raw_native_wrench(row: dict[str, Any]) -> dict[str, Any] | None:
    native = row.get("native_gazebo_contact_wrench")
    return native if isinstance(native, dict) else None


def _verified_native_wrench(
    row: dict[str, Any],
) -> tuple[tuple[float, float, float], tuple[float, float, float], dict[str, Any]] | None:
    native = _raw_native_wrench(row)
    if native is None or _native_wrench_blockers(row):
        return None
    try:
        force = _vec3(native["force_n"], label="force_n")
        torque = _vec3(native.get("torque_nm") or [0.0, 0.0, 0.0], label="torque_nm")
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (*force, *torque)):
        return None
    return force, torque, native


def _contact_count(row: dict[str, Any]) -> int:
    try:
        return int(row.get("contact_count") or 0)
    except (TypeError, ValueError):
        return 0


def _native_wrench_blockers(row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if "native_wrench" in row or "force_n" in row:
        blockers.append("legacy_force_field_contamination")
    native = _raw_native_wrench(row)
    if native is None:
        if "native_wrench" in row or "force_n" in row:
            blockers.append("missing_verified_gazebo_contact_wrench_provenance")
            return _dedupe(blockers)
        blockers.append("missing_native_gazebo_wrench_or_force_vector")
        return _dedupe(blockers)
    if (
        native.get("source") != ALLOWED_NATIVE_WRENCH_SOURCE
        or native.get("source_schema") not in ALLOWED_NATIVE_WRENCH_SCHEMAS
        or native.get("force_source_class") != contract.SOURCE_GAZEBO_CONTACT
    ):
        blockers.append("missing_verified_gazebo_contact_wrench_provenance")
    if native.get("measured_contact_wrench") is not True or native.get("commanded_force") is not False:
        blockers.append("commanded_or_unverified_wrench_force")
    expected_body, expected_collision = _expected_eoat_wrench_side(row)
    if expected_body is None:
        blockers.append("no_matching_eoat_surface_contact_pair")
    if (
        native.get("selected_body_role") != "eoat"
        or native.get("selected_body") != expected_body
        or native.get("selected_body_collision") != expected_collision
    ):
        blockers.append("missing_eoat_wrench_body_selection")
        blockers.append("wrench_body_collision_mismatch")
    if native.get("frame_id") != "base" or not _valid_transform_evidence(native):
        blockers.append("missing_base_frame_transform_evidence")
    if native.get("status") != "valid":
        blockers.append("native_wrench_status_not_valid")
    if native.get("baseline_policy") != "gazebo_contact_zero_no_contact_baseline":
        blockers.append("missing_valid_gazebo_contact_baseline")
    try:
        stamp_s = float(row["stamp_s"])
        wrench_stamp_s = float(native["wrench_stamp_s"])
    except (KeyError, TypeError, ValueError):
        blockers.append("missing_timestamped_native_wrench")
    else:
        if row.get("stamp_evidence") is not True or native.get("wrench_stamp_evidence") is not True:
            blockers.append("missing_timestamped_native_wrench")
        if abs(stamp_s - wrench_stamp_s) > 0.02:
            blockers.append("wrench_stamp_not_contact_aligned")
    try:
        _vec3(native["force_n"], label="force_n")
        _vec3(native.get("torque_nm") or [0.0, 0.0, 0.0], label="torque_nm")
        reaction_normal = _vec3(row.get("normal") or [0.0, 0.0, 1.0], label="normal")
        force = _vec3(native["force_n"], label="force_n")
    except (KeyError, TypeError, ValueError):
        blockers.append("invalid_native_wrench_vector")
    else:
        if _dot3(force, reaction_normal) <= 0.0:
            blockers.append("no_positive_normal_load_from_native_gazebo_wrench")
    if _contact_count(row) <= 0:
        blockers.append("missing_contact_count")
    return _dedupe(blockers)


def _expected_eoat_wrench_side(row: dict[str, Any]) -> tuple[str | None, str | None]:
    collision1 = str(row.get("collision1") or "")
    collision2 = str(row.get("collision2") or "")
    if _is_eoat_collision(collision1) and _is_surface_collision(collision2):
        return "body_1_wrench", "collision1"
    if _is_eoat_collision(collision2) and _is_surface_collision(collision1):
        return "body_2_wrench", "collision2"
    return None, None


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def _valid_transform_evidence(native: dict[str, Any]) -> bool:
    evidence = native.get("frame_transform_evidence")
    if not isinstance(evidence, dict):
        return False
    if not evidence.get("source") or not evidence.get("artifact_path"):
        return False
    if evidence.get("from_frame") != "gazebo_contact_message_native_frame":
        return False
    if evidence.get("to_frame") != "base":
        return False
    try:
        transform_stamp_s = float(evidence["stamp_s"])
        wrench_stamp_s = float(native["wrench_stamp_s"])
    except (KeyError, TypeError, ValueError):
        return False
    return abs(transform_stamp_s - wrench_stamp_s) <= 0.02


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _sample_from_row(row: dict[str, Any], *, sequence: int) -> contract.CanonicalWrenchSample | None:
    native = _verified_native_wrench(row)
    if native is None or _contact_count(row) <= 0:
        return None
    force, torque, native_payload = native
    reaction_normal = _vec3(row.get("normal") or [0.0, 0.0, 1.0], label="normal")
    if _dot3(force, reaction_normal) <= 0.0:
        return None
    flags = (
        "gazebo_contact_wrench_adapter",
        str(native_payload["source"]),
        str(native_payload["source_schema"]),
        "frame_transform_evidence_provided",
        str(row.get("normal_source") or "normal_source_unspecified"),
    )
    return contract.CanonicalWrenchSample(
        stamp_s=float(native_payload["wrench_stamp_s"]),
        frame_id="base",
        force_n=force,
        torque_nm=torque,
        source=contract.SOURCE_GAZEBO_CONTACT,
        valid=True,
        quality="gazebo_contact_native_wrench",
        status="valid",
        baseline_policy=str(native_payload["baseline_policy"]),
        latency_s=0.0,
        stale_after_s=0.1,
        diagnostic_flags=flags,
        sequence=sequence,
        contact_state="contact",
        reaction_normal=reaction_normal,
        approach_normal=(-reaction_normal[0], -reaction_normal[1], -reaction_normal[2]),
    )


def build_wrench_trace_or_report(
    contact_pair_payload: dict[str, Any],
    *,
    generated_at: str | None = None,
    source_topic: str = DEFAULT_SOURCE_TOPIC,
) -> dict[str, Any]:
    rows = contact_pair_payload.get("rows") or []
    native_wrench_row_count = sum(1 for row in rows if isinstance(row, dict) and _raw_native_wrench(row) is not None)
    verified_native_wrench_row_count = sum(
        1 for row in rows if isinstance(row, dict) and _verified_native_wrench(row) is not None
    )
    blockers: list[str] = []
    samples: list[contract.CanonicalWrenchSample] = []
    if contact_pair_payload.get("parse_issues"):
        blockers.append("contact_pair_parse_issues_present")
    for sequence, row in enumerate(rows):
        if not isinstance(row, dict):
            blockers.append("malformed_contact_pair_row")
            continue
        row_blockers = _native_wrench_blockers(row)
        blockers.extend(row_blockers)
        if not row_blockers:
            sample = _sample_from_row(row, sequence=sequence)
            if sample is None:
                blockers.append("verified_native_wrench_sample_build_failed")
            else:
                samples.append(sample)
    blockers = _dedupe(blockers)
    if not rows:
        blockers = ["missing_contact_pair_rows"]
    trace = contract.trace_payload(samples, source_topic=source_topic) if samples and not blockers else None
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": generated_at or _now_iso(),
        "mode": "offline_no_live_gazebo_contact_wrench_adapter",
        "goal_lineage": "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md",
        "trace_written": bool(trace),
        "claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER if trace else BLOCKED_CLAIM_TIER,
        "target_claim_tier": PHYSICAL_GAZEBO_CLAIM_TIER,
        "allowed_claim": (
            "physical Gazebo collision/contact physics with native gazebo_contact wrench correlation"
            if trace
            else "visual_only blocked/not_proven; contact pair evidence cannot be upgraded into force evidence"
        ),
        "forbidden_claim": (
            "real bench/live contact; simulated_ft; inferred force from contact position/normal/depth"
        ),
        "force_source": contract.SOURCE_GAZEBO_CONTACT if trace else None,
        "native_wrench_source_class": contract.SOURCE_GAZEBO_CONTACT if native_wrench_row_count > 0 else None,
        "source_topic": source_topic,
        "source_contact_pair_log_schema": contact_pair_payload.get("schema"),
        "source_contact_pair_row_count": len(rows),
        "native_wrench_row_count": native_wrench_row_count,
        "verified_native_wrench_row_count": verified_native_wrench_row_count,
        "required_native_fields": list(REQUIRED_NATIVE_FIELDS),
        "blockers": blockers,
        "claim_boundary_gate": {
            "visual_only_inputs_do_not_prove_force": True,
            "contact_pair_only_does_not_prove_wrench": True,
            "simulated_ft_is_not_physical_gazebo_contact": True,
            "real_bench_live_contact_authorized": False,
        },
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "wrench_trace": trace,
    }


def write_wrench_trace_or_report(
    output_dir: Path,
    *,
    contact_pair_path: Path,
    generated_at: str | None = None,
    source_topic: str = DEFAULT_SOURCE_TOPIC,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    contact_pair_payload = _load_json(contact_pair_path)
    report = build_wrench_trace_or_report(
        contact_pair_payload,
        generated_at=generated_at,
        source_topic=source_topic,
    )
    report["inputs"] = {"contact_pair_path": str(contact_pair_path)}
    if isinstance(report.get("wrench_trace"), dict):
        trace_path = output_dir / TRACE_FILENAME
        trace_path.write_text(json.dumps(report["wrench_trace"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["wrench_trace_path"] = str(trace_path)
    else:
        report["wrench_trace_path"] = None
    report_path = output_dir / REPORT_FILENAME
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--contact-pair", required=True, type=Path)
    parser.add_argument("--source-topic", default=DEFAULT_SOURCE_TOPIC)
    args = parser.parse_args()

    report_path = write_wrench_trace_or_report(
        args.output_dir,
        contact_pair_path=args.contact_pair,
        source_topic=args.source_topic,
    )
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
