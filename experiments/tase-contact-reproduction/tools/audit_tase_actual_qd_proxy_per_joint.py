#!/usr/bin/env python3
"""Split fixed-observation command-to-historical-actual_qd proxies by joint.

This audit reuses the five sealed rate460 A source attempts from the R32a
proxy reports. Candidate commands remain counterfactual; no candidate force
MAE or live motion evidence is produced.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

from audit_tase_qp_replay_inputs import verify_seal
from replay_tase_qp_normal_priority import (
    _fixed_observation_actual_qd_proxy,
    replay_attempt,
)


SCHEMA = "tase.actual-qd-fixed-observation-per-joint-proxy.v1"
METRICS = (
    "source_published_qdot_vs_historical_actual_qd_pearson",
    "candidate_qdot_vs_historical_actual_qd_pearson",
    "source_published_qdot_vs_historical_actual_qd_mae_rad_s",
    "candidate_qdot_vs_historical_actual_qd_mae_rad_s",
    "candidate_vs_source_qdot_mae_rad_s",
)


class AuditError(RuntimeError):
    """The source receipts or candidate traces do not satisfy the audit contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _rnn_candidate_qdot_by_sequence(
    *,
    root: Path,
    trace_path_value: str,
    expected_sha256: str,
    path_commands: list[dict[str, Any]],
) -> dict[int, list[float]]:
    trace_path = _resolve(root, trace_path_value).resolve(strict=True)
    if _sha256(trace_path) != expected_sha256:
        raise AuditError(f"RNN+SFC candidate trace SHA mismatch: {trace_path}")
    rows: list[dict[str, Any]] = []
    with gzip.open(trace_path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AuditError(f"invalid candidate trace JSON at line {line_number}") from exc
    if len(rows) != len(path_commands):
        raise AuditError(
            f"RNN+SFC trace row count {len(rows)} != source PATH rows {len(path_commands)}"
        )
    by_sequence: dict[int, list[float]] = {}
    for command, candidate in zip(path_commands, rows, strict=True):
        source_time = float(command["reference_time_s"])
        candidate_time = float(candidate["reference_time_s"])
        if not math.isfinite(candidate_time) or abs(source_time - candidate_time) > 1e-9:
            raise AuditError(
                f"RNN+SFC trace time mismatch at PATH t={source_time}: {candidate_time}"
            )
        qdot = [float(value) for value in candidate["rnn_candidate_qdot_rad_s"]]
        if len(qdot) != 6 or not all(math.isfinite(value) for value in qdot):
            raise AuditError("RNN+SFC candidate qdot must have six finite joint values")
        by_sequence[int(command["packet_sequence"])] = qdot
    return by_sequence


def _load_source(
    report_fold: dict[str, Any],
    expected_path_rows: int,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    attempt_dir = Path(report_fold["source_path"]).resolve(strict=True)
    _seal, attempt, verified = verify_seal(attempt_dir)
    if (attempt.get("lifecycle") or {}).get("path_complete") is not True:
        raise AuditError(f"source PATH is incomplete: {attempt_dir}")
    if (attempt.get("lifecycle") or {}).get("home_verified") is not True:
        raise AuditError(f"source Home is not verified: {attempt_dir}")
    timeline = _read_jsonl(Path(verified["command_timeline"]["path"]))
    frames = _read_jsonl(Path(verified["robot_frames"]["path"]))
    if verified["command_timeline"]["sha256"] != report_fold["source_timeline_sha256"]:
        raise AuditError(f"source timeline identity mismatch: {attempt_dir}")
    path_commands = [row for row in timeline if row.get("reference_phase") == "path"]
    if len(path_commands) != expected_path_rows:
        raise AuditError(
            f"source PATH row count {len(path_commands)} != report rows {expected_path_rows}"
        )
    rates = attempt["evidence"]["metrics"]["timing_evidence"]["layer_rates_hz"]
    source_rate_hz = float(rates["tp_consumed_packet_echoes"])
    if abs(source_rate_hz - float(report_fold["source_tp_consumed_echo_rate_hz"])) > 1e-9:
        raise AuditError(f"source TP rate differs from report: {attempt_dir}")
    if source_rate_hz < 460.0:
        raise AuditError(f"source TP rate is below rate460: {source_rate_hz}")
    return attempt_dir, attempt, path_commands, frames


def _summarize(values: list[float | None]) -> dict[str, float | None]:
    finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not finite:
        return {"median": None, "min": None, "max": None}
    return {
        "median": float(statistics.median(finite)),
        "min": min(finite),
        "max": max(finite),
    }


def run_audit(
    *,
    root: Path,
    rnn_report_path: Path,
    qp_report_path: Path,
    sfc_qp_report_path: Path,
) -> dict[str, Any]:
    reports = {
        "rnn_sfc": json.loads(rnn_report_path.read_text(encoding="utf-8")),
        "tase_qp": json.loads(qp_report_path.read_text(encoding="utf-8")),
        "sfc_tase_qp": json.loads(sfc_qp_report_path.read_text(encoding="utf-8")),
    }
    if any(report.get("live_eligible") is not False for report in reports.values()):
        raise AuditError("input command-proxy reports must remain offline-only")
    fold_counts = {len(report.get("folds", [])) for report in reports.values()}
    if fold_counts != {5}:
        raise AuditError(f"expected five folds per report, found {fold_counts}")
    identities = [
        [(fold["source_path"], fold["source_timeline_sha256"]) for fold in report["folds"]]
        for report in reports.values()
    ]
    if not identities[0] == identities[1] == identities[2]:
        raise AuditError("the three reports do not use the same five source attempts")

    methods: list[dict[str, Any]] = []
    source_rates: list[float] = []
    for name, report in reports.items():
        method_folds: list[dict[str, Any]] = []
        for fold in report["folds"]:
            expected_path_rows = int(fold.get("candidate_rows", fold.get("path_rows", 0)))
            attempt_dir, attempt, path_commands, frames = _load_source(fold, expected_path_rows)
            source_rates.append(
                float(attempt["evidence"]["metrics"]["timing_evidence"]["layer_rates_hz"]["tp_consumed_packet_echoes"])
            )
            if name == "rnn_sfc":
                candidate_by_sequence = _rnn_candidate_qdot_by_sequence(
                    root=root,
                    trace_path_value=fold["candidate_command_trace"],
                    expected_sha256=fold["candidate_command_trace_sha256"],
                    path_commands=path_commands,
                )
                commands = {int(row["packet_sequence"]): row for row in path_commands}
                proxy = _fixed_observation_actual_qd_proxy(
                    frames=frames,
                    commands_by_sequence=commands,
                    candidate_qdot_by_sequence=candidate_by_sequence,
                )
                expected = float(fold["candidate_rnn_sfc_qdot_historical_actual_qd_pearson"])
            else:
                profile = report["solver_profile"]
                twist_path = None
                if name == "sfc_tase_qp":
                    trace = fold["candidate_twist_trace"]
                    path = _resolve(root, trace["path"]).resolve(strict=True)
                    if _sha256(path) != trace["sha256"]:
                        raise AuditError(f"SFC+QP twist trace SHA mismatch: {path}")
                    twist_path = path
                replay = replay_attempt(
                    attempt_dir,
                    osqp_rho=float(profile["osqp_rho"]),
                    osqp_scaling=int(profile["osqp_scaling"]),
                    desired_twist_trace=twist_path,
                )
                if replay["candidate_solver_path_complete"] is not True:
                    raise AuditError(f"QP candidate replay incomplete: {attempt_dir}")
                proxy = replay["fixed_observation_actual_qd_proxy"]
                expected = float(fold["historical_actual_qd_proxy"]["candidate_qdot_vs_historical_actual_qd_pearson"])
            actual_corr = proxy["candidate_qdot_vs_historical_actual_qd_pearson"]
            if actual_corr is None or abs(float(actual_corr) - expected) > 1e-11:
                raise AuditError(f"candidate full-vector proxy changed for {name}, fold {fold['fold']}")
            if proxy["candidate_force_mae"] is not None:
                raise AuditError("candidate fixed-observation proxy must not contain a force MAE")
            method_folds.append({
                "fold": int(fold["fold"]),
                "source_attempt": str(attempt_dir),
                "source_timeline_sha256": fold["source_timeline_sha256"],
                "source_tp_consumed_echo_rate_hz": source_rates[-1],
                "matched_consumed_frames": int(proxy["candidate_matched_consumed_frames"]),
                "candidate_force_mae_n": None,
                "per_joint": proxy["per_joint"],
            })
        joint_summary = []
        for joint in range(1, 7):
            values = [
                row["per_joint"][joint - 1]
                for row in method_folds
            ]
            summary = {"joint_1_based": joint}
            for metric in METRICS:
                summary[metric] = _summarize([row[metric] for row in values])
            joint_summary.append(summary)
        methods.append({
            "report_method_id": report.get("method_id", name),
            "analysis_method": name,
            "solver_profile": report.get("solver_profile"),
            "folds": method_folds,
            "per_joint_summary_across_five_fixed_sources": joint_summary,
        })
    return {
        "schema": SCHEMA,
        "evidence_class": "offline_fixed_observation_actual_qd_per_joint_proxy",
        "protocol_id": "figure8_window60_r013_rate460_v1",
        "source_count": 5,
        "source_tp_consumed_echo_rate_hz": {"min": min(source_rates), "max": max(source_rates)},
        "formal_bins_per_source": 550,
        "diagnostic_bins_per_source": 600,
        "force_source": "Kunwei KWR75 feedback from historical A traces; not independent task-force truth",
        "candidate_force_mae_computed": False,
        "live_eligible": False,
        "interpretation": (
            "Per-joint candidate commands are compared with historical source-A RTDE actual_qd "
            "on the same consumed packet joins. actual_qd belongs to the historical controller; "
            "this is not candidate motion, closed-loop force tracking, or a live gate."
        ),
        "methods": methods,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--rnn-report", type=Path, required=True)
    parser.add_argument("--qp-report", type=Path, required=True)
    parser.add_argument("--sfc-qp-report", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    result = run_audit(
        root=root,
        rnn_report_path=args.rnn_report.resolve(strict=True),
        qp_report_path=args.qp_report.resolve(strict=True),
        sfc_qp_report_path=args.sfc_qp_report.resolve(strict=True),
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "json": str(args.json_out.resolve()),
        "source_count": result["source_count"],
        "source_tp_consumed_echo_rate_hz": result["source_tp_consumed_echo_rate_hz"],
        "methods": [method["analysis_method"] for method in result["methods"]],
        "candidate_force_mae_computed": result["candidate_force_mae_computed"],
        "live_eligible": result["live_eligible"],
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
