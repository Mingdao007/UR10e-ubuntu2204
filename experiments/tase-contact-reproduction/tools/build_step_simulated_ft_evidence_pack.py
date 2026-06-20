#!/usr/bin/env python3
"""Build per-stage canonical simulated-FT log evidence for Step5/6/7/8.

The pack is offline-only. It writes source-backed canonical wrench traces from
the Step5/6 simulation matrix so report-level audits can distinguish per-stage
`simulated_ft` evidence from global P1 evidence and from Gazebo contact physics.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE_ROOT = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from ur10e_example_controllers import canonical_wrench_contract as wrench_contract  # noqa: E402
from ur10e_example_controllers import step56_simulation_matrix as step56  # noqa: E402


LOG_SCHEMA = "ur10e_stage_canonical_simulated_ft_log_v1"
PACK_SCHEMA = "ur10e_step_simulated_ft_evidence_pack_v1"
CONTACT_STAGE_IDS = tuple(
    stage_id for stage_id, spec in step56.STAGE_REGISTRY.items() if spec.contact
)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve()))
    except ValueError:
        return str(path)


def trace_evidence_fields(trace: dict[str, Any], *, log_path: Path | None = None) -> dict[str, bool]:
    rows = trace.get("rows") or []
    first = rows[0] if rows else {}
    header = first.get("header") if isinstance(first, dict) else {}
    return {
        "stamp": isinstance(header, dict) and "stamp_s" in header,
        "frame_id": isinstance(header, dict) and bool(header.get("frame_id")),
        "source": bool(first.get("source")),
        "status": bool(first.get("status")),
        "baseline": bool(first.get("baseline_policy")),
        "log_evidence": bool(log_path is None or log_path.is_file()) and len(rows) > 0,
    }


def validate_trace(trace: dict[str, Any], *, log_path: Path | None = None) -> list[str]:
    issues: list[str] = []
    if trace.get("schema") != wrench_contract.TRACE_SCHEMA:
        issues.append("trace_schema:not_canonical_wrench_trace")
    if trace.get("force_source") != wrench_contract.SOURCE_SIMULATED_FT:
        issues.append("force_source:not_simulated_ft")
    if trace.get("claim_tier") != "simulated_ft":
        issues.append("claim_tier:not_simulated_ft")
    if trace.get("schema_issues"):
        issues.append("trace_schema_issues:not_empty")
    rows = trace.get("rows")
    if not isinstance(rows, list) or not rows:
        issues.append("rows:missing_or_empty")
        return issues

    fields = trace_evidence_fields(trace, log_path=log_path)
    for field, present in fields.items():
        if not present:
            issues.append(f"missing:{field}")

    for index, row in enumerate(rows):
        sample_issues = wrench_contract.validate_canonical_sample_row(row)
        if sample_issues:
            issues.append(f"row_{index}:" + ",".join(sample_issues))
        if row.get("source") != wrench_contract.SOURCE_SIMULATED_FT:
            issues.append(f"row_{index}:source:not_simulated_ft")
        if row.get("claim_tier") != "simulated_ft":
            issues.append(f"row_{index}:claim_tier:not_simulated_ft")
    return issues


def stage_log_payload(stage_id: str, *, generated_at: str) -> dict[str, Any]:
    artifact = step56.build_stage_artifact(stage_id)
    trace = artifact.get("simulated_force_evidence")
    if not isinstance(trace, dict):
        raise RuntimeError(f"{stage_id}: missing simulated_force_evidence")
    issues = validate_trace(trace)
    return {
        "schema": LOG_SCHEMA,
        "generated_at": generated_at,
        "stage_id": stage_id,
        "source_stage_id": artifact.get("stage", {}).get("source_stage_id"),
        "claim_tier": "simulated_ft" if not issues else "visual_only",
        "mode": "offline_source_backed_canonical_simulated_ft_log",
        "source_generator": rel(Path(step56.__file__)),
        "canonical_wrench_contract": rel(Path(wrench_contract.__file__)),
        "stage_summary_source": "step56.build_stage_artifact",
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "forbidden_claim": "physical Gazebo collision/contact physics; real bench/live contact; live bridge/TP/URScript/motion",
        "trace_validation_issues": issues,
        "evidence_fields_present": trace_evidence_fields(trace),
        "trace": trace,
    }


def write_pack(output_dir: Path, *, generated_at: str | None = None) -> Path:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    output_dir.mkdir(parents=True, exist_ok=True)

    stages: dict[str, dict[str, Any]] = {}
    for stage_id in CONTACT_STAGE_IDS:
        payload = stage_log_payload(stage_id, generated_at=generated)
        log_path = output_dir / f"{stage_id}_canonical_simulated_ft_log.json"
        log_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        trace = payload["trace"]
        rows = trace["rows"]
        fields = trace_evidence_fields(trace, log_path=log_path)
        issues = validate_trace(trace, log_path=log_path)
        stages[stage_id] = {
            "stage_id": stage_id,
            "log_path": rel(log_path),
            "claim_tier": "simulated_ft" if not issues else "visual_only",
            "valid": not issues,
            "validation_issues": issues,
            "sample_count": int(trace.get("sample_count") or len(rows)),
            "first_stamp_s": rows[0]["header"]["stamp_s"],
            "last_stamp_s": rows[-1]["header"]["stamp_s"],
            "frame_id": rows[0]["header"]["frame_id"],
            "source": rows[0]["source"],
            "status_values": sorted({str(row.get("status")) for row in rows}),
            "baseline_policy_values": sorted({str(row.get("baseline_policy")) for row in rows}),
            "evidence_fields_present": fields,
        }

    all_valid = bool(stages) and all(stage["valid"] for stage in stages.values())
    manifest = {
        "schema": PACK_SCHEMA,
        "generated_at": generated,
        "mode": "offline_per_stage_canonical_simulated_ft_evidence",
        "claim_tier": "simulated_ft" if all_valid else "visual_only",
        "source_generator": rel(Path(step56.__file__)),
        "canonical_wrench_contract": rel(Path(wrench_contract.__file__)),
        "stage_count": len(stages),
        "contact_stage_ids": list(CONTACT_STAGE_IDS),
        "all_contact_stages_valid": all_valid,
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "forbidden_claim": "physical Gazebo collision/contact physics; real bench/live contact; live bridge/TP/URScript/motion",
        "stages": stages,
    }
    manifest_path = output_dir / "step_simulated_ft_evidence_manifest.json"
    manifest["artifact_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_pack(args.output_dir, generated_at=args.generated_at)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
