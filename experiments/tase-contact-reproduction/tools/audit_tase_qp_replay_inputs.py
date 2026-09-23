#!/usr/bin/env python3
"""Audit replay inputs in one sealed rate400 TASE attempt; perform no solver run."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ATTEMPT = (
    ROOT / "runs/tase-resident-tune-rate400-b-20260923-01/confirmation-rate400-b-v1"
    / "session-02/attempts/0001"
)
DEFAULT_JSON = ROOT / "reports/tase_qp_replay_confirmation_a_missing_fields_20260923.json"
DEFAULT_MD = ROOT / "reports/tase_qp_replay_confirmation_a_missing_fields_20260923.md"
SEGMENTS = (
    "admission_robot_frames", "command_timeline", "published_packets",
    "raw_sensor", "rejected_robot_frames", "robot_frames",
)
SERVICE_OBSERVATIONS = "service_observations"
PROTOCOL_ID = "figure8_window60_r013_rate400_v1"
PER_TICK_RESULT_HISTORY_KEYS = {
    "per_tick_result_history", "per_tick_results", "tick_results", "step_results",
    "sample_results", "provider_results", "result_history", "results_by_tick",
}


class AuditError(RuntimeError):
    pass


def hash_and_count(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    rows = 0
    with path.open("rb") as source:
        for line in source:
            digest.update(line)
            rows += 1
    return digest.hexdigest(), rows


def verify_seal(attempt_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    seal = json.loads((attempt_dir / "seal.json").read_text(encoding="utf-8"))
    result = json.loads((attempt_dir / "attempt-result.json").read_text(encoding="utf-8"))
    sealed_copy = result.get("sealed_evidence", {})
    if (seal.get("schema") != "yield-live-entry/attempt-seal-v1"
            or seal.get("lifecycle", {}).get("sealed") is not True
            or sealed_copy.get("lifecycle", {}).get("sealed") is not True):
        raise AuditError("attempt does not carry the expected sealed lifecycle")
    if set(seal.get("segments", {})) != set(SEGMENTS):
        raise AuditError("sealed segment set differs from the expected current schema")
    verified: dict[str, Any] = {}
    for name in SEGMENTS:
        spec = seal["segments"][name]
        sealed_path = Path(spec["path"])
        if not sealed_path.is_absolute():
            sealed_path = attempt_dir / sealed_path
        if sealed_path.is_symlink():
            raise AuditError(f"sealed segment {name} is a symlink")
        expected_unresolved = attempt_dir / f"{name}.jsonl"
        if expected_unresolved.is_symlink():
            raise AuditError(f"selected attempt segment {name} is a symlink")
        path = sealed_path.resolve(strict=True)
        expected = expected_unresolved.resolve(strict=True)
        if path != expected:
            raise AuditError(f"segment {name} is outside the selected attempt")
        digest, rows = hash_and_count(path)
        if digest != spec.get("sha256") or rows != spec.get("count"):
            raise AuditError(f"seal digest/count mismatch for {name}")
        copy = sealed_copy.get("segments", {}).get(name, {})
        if copy.get("sha256") != digest or copy.get("count") != rows:
            raise AuditError(f"attempt-result seal copy mismatch for {name}")
        verified[name] = {"path": str(path), "sha256": digest, "rows": rows}
    spec = seal.get(SERVICE_OBSERVATIONS, {})
    service_path = Path(spec.get("path", ""))
    if not service_path.is_absolute():
        service_path = attempt_dir / service_path
    service_expected_unresolved = attempt_dir / f"{SERVICE_OBSERVATIONS}.jsonl"
    if service_path.is_symlink() or service_expected_unresolved.is_symlink():
        raise AuditError("service observations are symlinks")
    service_path = service_path.resolve(strict=True)
    if service_path != service_expected_unresolved.resolve(strict=True):
        raise AuditError("service observations are outside the selected attempt")
    digest, rows = hash_and_count(service_path)
    if digest != spec.get("sha256") or rows != spec.get("count"):
        raise AuditError("seal digest/count mismatch for service_observations")
    copy = sealed_copy.get(SERVICE_OBSERVATIONS, {})
    if copy.get("sha256") != digest or copy.get("count") != rows:
        raise AuditError("attempt-result seal copy mismatch for service_observations")
    verified[SERVICE_OBSERVATIONS] = {"path": str(service_path), "sha256": digest, "rows": rows}
    return seal, result, verified


def _read(path: Path):
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            yield json.loads(line)


def _per_tick_input_fields(value: Any, prefix: str = "$") -> list[str]:
    """Find outer-task, dt, or reference fields recursively in a raw row."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if ("twist" in lowered or "outer" in lowered or "reference" in lowered
                    or lowered in {"dt", "dt_s", "actual_dt_s", "sample_monotonic_s", "provider_monotonic_s"}
                    or lowered.endswith("_dt_s")):
                found.append(f"{prefix}.{key}")
            found.extend(_per_tick_input_fields(child, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_per_tick_input_fields(child, f"{prefix}[{index}]"))
    return found


def _per_tick_result_history(value: Any, prefix: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            is_history_key = (
                lowered in PER_TICK_RESULT_HISTORY_KEYS
                or ("history" in lowered and any(token in lowered for token in ("result", "tick", "sample", "provider")))
                or ("per_tick" in lowered and any(token in lowered for token in ("result", "output", "record")))
            )
            if is_history_key and child:
                found.append(f"{prefix}.{key}")
            found.extend(_per_tick_result_history(child, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_per_tick_result_history(child, f"{prefix}[{index}]"))
    return found


def inspect_attempt(attempt_dir: Path) -> dict[str, Any]:
    _, result, segments = verify_seal(attempt_dir)
    binding = result.get("parameter_binding", {})
    expected = {
        "candidate_id": "confirm-s02-b00-A",
        "Md_scalar": 9.565272137974492,
        "Bd_scalar": 693.6559295653944,
        "force_integral_limit_n_s": 1.0,
    }
    if binding.get("candidate_id") != expected["candidate_id"]:
        raise AuditError("selected attempt is not confirmation A")
    if binding.get("protocol_id") != PROTOCOL_ID:
        raise AuditError("confirmation-A parameter binding protocol_id differs")
    evidence = result.get("evidence", {})
    if not isinstance(evidence, dict):
        raise AuditError("confirmation-A evidence metrics mapping is required")
    metrics = evidence.get("metrics")
    if not isinstance(metrics, dict):
        raise AuditError("confirmation-A evidence.metrics mapping is required")
    if metrics.get("protocol_id") != PROTOCOL_ID:
        raise AuditError("confirmation-A metrics protocol_id differs")
    for name in ("Md_scalar", "Bd_scalar", "force_integral_limit_n_s"):
        if float(binding.get(name, math.nan)) != expected[name]:
            raise AuditError(f"confirmation-A parameter {name} differs")

    origin = float(result.get("timing", {}).get("path_started_monotonic_s", math.nan))
    if not math.isfinite(origin):
        raise AuditError("attempt omits formal PATH origin")
    path_packets = 0
    path_sequences: set[int] = set()
    packet_replay_fields_seen: set[str] = set()
    packet_has_joint_qdot = True
    for row in _read(Path(segments["published_packets"]["path"])):
        if not isinstance(row, list) or len(row) != 2:
            raise AuditError("published packet line is not [time, packet]")
        stamp, packet = float(row[0]), row[1]
        values = packet.get("double_values", [])
        if len(values) != 24:
            raise AuditError("published packet has an unexpected register layout")
        packet_fields = _per_tick_input_fields(packet)
        if packet_fields:
            packet_replay_fields_seen.update(packet_fields)
            raise AuditError("per-packet replay input fields are present: " + ", ".join(packet_fields[:4]))
        if packet.get("command_mode") == 2 and stamp >= origin:
            path_packets += 1
            path_sequences.add(int(packet["sequence"]))
            packet_has_joint_qdot &= len(values[13:19]) == 6

    rtde_sequences: set[int] = set()
    for row in _read(Path(segments["robot_frames"]["path"])):
        sequence = int(row.get("consumed_packet_sequence", -1))
        if sequence >= 0:
            rtde_sequences.add(sequence)
    sensor_sequences: set[int] = set()
    for row in _read(Path(segments["raw_sensor"]["path"])):
        sequence = int(row.get("packet_sequence", -1))
        if sequence >= 0:
            sensor_sequences.add(sequence)

    service_labels: dict[str, int] = {}
    service_rows_with_missing_inputs = 0
    service_replay_fields_seen: set[str] = set()
    for row in _read(Path(segments[SERVICE_OBSERVATIONS]["path"])):
        label = str(row.get("label", "unknown"))
        service_labels[label] = service_labels.get(label, 0) + 1
        service_fields = _per_tick_input_fields(row)
        service_history = _per_tick_result_history(row)
        if service_fields:
            service_replay_fields_seen.update(service_fields)
            raise AuditError("service observation replay input fields are present: " + ", ".join(service_fields[:4]))
        if service_history:
            raise AuditError("service observation per-tick result history is present: " + ", ".join(service_history[:4]))
        else:
            service_rows_with_missing_inputs += 1

    timeline_rows = segments["command_timeline"]["rows"]
    if timeline_rows != 0:
        raise AuditError("command_timeline contains per-command rows; refresh this audit contract")
    last = result.get("state", {}).get("last_result", {})
    final_outer = last.get("outer_output_feedback_pending", {})
    history_fields = _per_tick_result_history(result)
    if history_fields:
        raise AuditError("per-tick result history is present: " + ", ".join(history_fields[:4]))
    return {
        "schema": "tase.qp-replay-input-audit-v1",
        "claim_scope": "sealed_trace_completeness_audit_no_controller_replay",
        "offline_only": True,
        "hardware_or_endpoint_action": False,
        "attempt_dir": str(Path(attempt_dir).resolve()),
        "candidate_id": binding["candidate_id"],
        "protocol_id": PROTOCOL_ID,
        "parameters": {key: expected[key] for key in ("Md_scalar", "Bd_scalar", "force_integral_limit_n_s")},
        "seal_verified": True,
        "sealed_segments": segments,
        "formal_path_origin_host_monotonic_s": origin,
        "formal_path_packet_rows": path_packets,
        "formal_path_sequences_joined_to_rtde": len(path_sequences & rtde_sequences),
        "formal_path_sequences_joined_to_sensor": len(path_sequences & sensor_sequences),
        "command_timeline_rows": timeline_rows,
        "service_observation_labels": service_labels,
        "service_observation_rows_without_outer_dt_reference_fields": service_rows_with_missing_inputs,
        "service_observation_replay_fields_detected": sorted(service_replay_fields_seen),
        "packet_fields": {
            "qdot_register_slice_13_19_present": packet_has_joint_qdot,
            "replay_input_fields_detected": sorted(packet_replay_fields_seen),
            "per_sample_outer_twist_or_actual_dt_or_reference_time_present": bool(packet_replay_fields_seen),
            "note": "wire packet carries sensor registers and published qdot; requested outer twist and provider dt/reference time are not packet fields",
        },
        "attempt_result_state": {
            "last_result_sample_time_s": last.get("sample_time_s"),
            "last_result_actual_dt_s": last.get("actual_dt_s"),
            "terminal_requested_outer_twist_present": isinstance(final_outer.get("requested_twist"), list),
            "per_tick_result_history_fields_detected": history_fields,
            "per_tick_result_history_present": bool(history_fields),
        },
        "replay_status": "blocked_by_missing_sealed_per_tick_inputs",
        "missing_fields": [
            "requested outer twist for each PATH packet after live caps and fixed-Home orientation adaptation",
            "actual_dt_s supplied to each provider tick; only the terminal value is retained",
            "reference_time_s/provider monotonic sample time for each packet; command_timeline has zero rows",
            "RNN state at formal PATH origin and each force-preempt warm-start event",
            "per-tick solver and full-provider elapsed times",
        ],
        "not_computed": [
            "per-sample calibrated J and static joint-position/velocity bounds; both are reconstructible from sealed q with the current calibrated model and live runtime formula, but were outside this missing-input audit",
            "slew-adjusted live lower/upper vectors, because per-tick actual_dt_s is absent",
            "matched RNN-versus-QP infeasibility/rejection counts and equality/bound residuals",
            "QP solver timing and full-provider timing",
        ],
        "next_attempt_logging": [
            "Append one sealed row per successful PATH publish with sample_monotonic_s, actual_dt_s and reference_phase/time_s.",
            "Include requested outer twist, exact six lower/upper vectors, previous successfully published qdot and applied slew scale.",
            "Record PATH-origin RNN state and each warm-start transition, plus solver and full-provider elapsed times.",
        ],
    }


def write_report(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    with Path(json_path).open("w", encoding="utf-8") as target:
        json.dump(report, target, indent=2, sort_keys=True, allow_nan=False)
        target.write("\n")
    md = [
        "# TASE-QP replay input audit: confirmation A",
        "",
        "本次确认 A sealed attempt 的静态输入完整性已检查，但 trace 缺少逐周期 requested outer twist、`actual_dt_s`、reference clock 和 formal PATH 起点的 RNN state；封存的 `command_timeline.jsonl` 为 0 行。因此无法做公平的 matched RNN/strict Eq20 QP replay。",
        "",
        f"- Attempt: `{report['attempt_dir']}`，candidate `{report['candidate_id']}`。",
        f"- Frozen values: Md={report['parameters']['Md_scalar']}, Bd={report['parameters']['Bd_scalar']}, integral limit={report['parameters']['force_integral_limit_n_s']} N s。",
        f"- Protocol binding: `{report['protocol_id']}`（attempt parameter binding 与 metrics 均一致）。",
        f"- Seal verification: PASS；formal PATH published packet rows={report['formal_path_packet_rows']}；与 RTDE / sensor sequence 对上={report['formal_path_sequences_joined_to_rtde']} / {report['formal_path_sequences_joined_to_sensor']}。",
        "- Published packet rows preserve actual qdot and wrench/force registers; full RTDE rows preserve q/qd and pose/speed; raw sensor rows preserve corrected wrench. The service-observation sidecar also contains no per-row outer twist/dt/reference-time fields; `attempt-result.json` retains only its terminal provider result, not the per-tick task inputs.",
        "- Per-sample calibrated J and static joint-position/velocity bounds were not computed by this bounded input audit; they are reconstructible from sealed q with the current calibrated model and live runtime formula. Slew-adjusted bounds remain unavailable without per-tick actual_dt_s.",
        "- QP rejection/infeasible count, equality/bound residual, and QP/full-provider timing were not computed. No matched QP result is claimed.",
        "",
        "下一次采集应把每个成功 PATH publish 的 sample monotonic time、actual dt、reference phase/time、requested outer twist、六维 bounds、previous successful qdot/slew scale、PATH 起点 RNN state/warm-start events 与 solver/provider timing 一起纳入 seal。",
        "",
        "本审计只读本地封存文件；没有运行 solver、改 registry、访问网络或触碰设备。",
        "",
    ]
    with Path(md_path).open("w", encoding="utf-8") as target:
        target.write("\n".join(md))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, default=DEFAULT_ATTEMPT)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()
    report = inspect_attempt(args.attempt_dir)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, args.json, args.markdown)
    print(json.dumps({
        "json": str(args.json),
        "markdown": str(args.markdown),
        "seal_verified": report["seal_verified"],
        "path_packets": report["formal_path_packet_rows"],
        "matched_replay": False,
        "reason": report["replay_status"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
