from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .guarded_contact_recovery_shadow import RUNS_ROOT, WORKSPACE_ROOT
from .step5a_cartesian_cycloid_motion import (
    DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M,
    DEFAULT_TRACE_MAX_SAMPLE_GAP_S,
    _empty_trace_alignment,
    _kunwei_artifact_ok,
    step5a_acceptance,
)


DEFAULT_EVIDENCE_RUN = RUNS_ROOT / "no_contact_test_20260617_143540"


def audit_gate_a_run(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_absolute():
        run_dir = WORKSPACE_ROOT / run_dir
    run_dir = run_dir.resolve()
    summary_path = run_dir / "step5a_cartesian_cycloid_motion.json"
    trace_path = run_dir / "step5a_cartesian_cycloid_motion_trace.csv"
    kunwei_path = run_dir / "kunwei_persistent_monitor.json"
    summary = _load_json(summary_path)
    kunwei = _load_json(kunwei_path) if kunwei_path.exists() else {}
    trace = _trace_diagnostics(trace_path)

    payload = dict(summary)
    payload["kunwei_monitor"] = kunwei or summary.get("kunwei_monitor", {})
    payload["kunwei_artifact_ok"] = _kunwei_artifact_ok(payload["kunwei_monitor"])
    payload.setdefault(
        "gate_a_thresholds",
        {
            "velocity_cap_m_s": float(payload.get("velocity_cap_m_s", 0.009)),
            "cartesian_position_error_limit_m": DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M,
            "trace_max_sample_gap_s": DEFAULT_TRACE_MAX_SAMPLE_GAP_S,
        },
    )
    if not isinstance(payload.get("trace_alignment"), dict):
        payload["trace_alignment"] = _empty_trace_alignment("legacy_trace_without_observed_sample_timing")
    if "observed_sample_t_rel_s" not in trace["header"]:
        payload["trace_alignment"] = _empty_trace_alignment("legacy_trace_without_observed_sample_timing")
    payload["acceptance"] = step5a_acceptance(payload)
    payload["gate_a_pass"] = bool(payload["acceptance"]["gate_a_pass"])
    payload["ok"] = bool(payload["gate_a_pass"])

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "role": "step5a_gate_a_readonly_audit",
        "run_dir": str(run_dir),
        "summary_path": str(summary_path),
        "trace_path": str(trace_path),
        "kunwei_path": str(kunwei_path),
        "gate_a_pass": bool(payload["gate_a_pass"]),
        "ok": bool(payload["ok"]),
        "acceptance": payload["acceptance"],
        "metrics": {
            "motion_kind": payload.get("motion_kind"),
            "rows": payload.get("rows"),
            "phase_final": payload.get("phase_final"),
            "sent_goal": payload.get("sent_goal"),
            "accepted": payload.get("accepted"),
            "result_error_code": payload.get("result_error_code"),
            "kunwei_artifact_ok": payload.get("kunwei_artifact_ok"),
            "velocity_cap_m_s": payload.get("velocity_cap_m_s"),
            "max_reference_speed_m_s": payload.get("max_reference_speed_m_s"),
            "max_commanded_fk_speed_m_s": payload.get("max_commanded_fk_speed_m_s"),
            "max_achieved_speed_m_s": payload.get("max_achieved_speed_m_s"),
            "max_cartesian_position_error_m": payload.get("max_cartesian_position_error_m"),
        },
        "trace_diagnostics": trace,
        "trace_alignment": payload["trace_alignment"],
        "judgement": "Step5a live motion executed, but Gate A failed"
        if not payload["gate_a_pass"] and payload.get("sent_goal") is True
        else ("Step5a Gate A passed" if payload["gate_a_pass"] else "Step5a Gate A not proven"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Gate A audit for a Step5a no-contact live run directory.")
    parser.add_argument("run_dir", nargs="?", type=Path, default=DEFAULT_EVIDENCE_RUN)
    args = parser.parse_args(argv)
    audit = audit_gate_a_run(args.run_dir)
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 0 if audit["gate_a_pass"] else 2


def _trace_diagnostics(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "header": [],
            "rows": 0,
            "achieved_speed_over_cap_rows": None,
            "cartesian_error_over_5mm_rows": None,
        }
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        header = list(reader.fieldnames or [])
    speeds = [_float_or_nan(row.get("achieved_speed_m_s")) for row in rows]
    errors = [_float_or_nan(row.get("cartesian_error_m")) for row in rows]
    finite_speeds = [value for value in speeds if math.isfinite(value)]
    finite_errors = [value for value in errors if math.isfinite(value)]
    return {
        "exists": True,
        "header": header,
        "rows": len(rows),
        "achieved_speed_over_cap_rows": sum(value > 0.009 for value in finite_speeds),
        "cartesian_error_over_5mm_rows": sum(value > DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M for value in finite_errors),
        "achieved_speed_max_m_s": max(finite_speeds) if finite_speeds else None,
        "cartesian_error_mean_m": sum(finite_errors) / len(finite_errors) if finite_errors else None,
        "cartesian_error_max_m": max(finite_errors) if finite_errors else None,
        "legacy_trace_without_observed_sample_timing": "observed_sample_t_rel_s" not in header,
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"required Step5a Gate A audit input is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _float_or_nan(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


if __name__ == "__main__":
    raise SystemExit(main())
