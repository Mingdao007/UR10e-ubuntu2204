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


def _finite_vector(value: Any, size: int, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != size:
        raise AuditError(f"trace field {field} must have {size} values")
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise AuditError(f"trace field {field} is not numeric") from exc
    if not all(math.isfinite(item) for item in vector):
        raise AuditError(f"trace field {field} contains nonfinite values")
    return vector


def inspect_captured_trace(
    *, attempt_dir: Path, seal: dict[str, Any], result: dict[str, Any],
    segments: dict[str, Any], metrics: dict[str, Any],
    parameters: dict[str, float], candidate_id: str, integral_policy: str,
    origin: float, path_packets: int, path_sequences: set[int],
    path_packet_qdot: dict[int, tuple[float, ...]],
    rtde_sequences: set[int], sensor_sequences: set[int],
) -> dict[str, Any]:
    """Validate sealed RNN inputs for a future offline QP solve only."""

    replay = seal.get("replay_evidence")
    if not isinstance(replay, dict):
        raise AuditError("command_timeline rows lack a sealed replay-evidence receipt")
    if replay.get("complete") is not True or replay.get("packet_sequence_join_passed") is not True:
        raise AuditError("sealed replay-evidence receipt is incomplete")
    expected = int(replay.get("expected_formal_path_publish_count", -1))
    recorded = int(replay.get("record_count", -1))
    rows_count = int(segments["command_timeline"]["rows"])
    if expected != recorded or recorded != rows_count:
        raise AuditError("sealed replay-evidence row count differs from the segment")
    for key in (
        "dropped_record_count", "missing_capture_count",
        "missing_packet_sequence_joins", "duplicate_published_packet_sequences",
    ):
        if int(replay.get(key, -1)) != 0:
            raise AuditError(f"sealed replay-evidence {key} is nonzero")
    if result.get("lifecycle", {}).get("replay_evidence_complete") is not True:
        raise AuditError("attempt lifecycle does not mark replay evidence complete")
    if seal.get("lifecycle", {}).get("path_complete") is not True:
        raise AuditError("trace attempt did not complete its formal PATH")
    if seal.get("lifecycle", {}).get("home_verified") is not True:
        raise AuditError("trace attempt has no verified joint Home")
    duration = float(metrics.get("path_duration_s", math.nan))
    if duration != 60.0 or int(metrics.get("complete_bins", -1)) != 550 \
            or int(metrics.get("required_bins", -1)) != 550:
        raise AuditError("trace attempt is not a complete 60 s / 550-bin rate400 PATH")
    implementation = result.get("state", {}).get("last_result", {}).get("implementation")
    if implementation != "mature_local_tase_rnn":
        raise AuditError("captured trace is not bound to the mature TASE-RNN implementation")

    trace_sequences: set[int] = set()
    path_origin_events = 0
    transition_event_count = 0
    first_time = None
    last_time = None
    previous_sequence = -1
    previous_reference = -math.inf
    previous_host = -math.inf
    previous_published = -math.inf
    for row in _read(Path(segments["command_timeline"]["path"])):
        if not isinstance(row, dict) or row.get("schema") != "tase.qp-replay-command-timeline-v1":
            raise AuditError("command_timeline row schema differs")
        if int(row.get("attempt_sequence", -1)) != int(result.get("sequence", -2)):
            raise AuditError("command_timeline attempt sequence differs")
        if row.get("reference_phase") != "path":
            raise AuditError("command_timeline contains a non-PATH row")
        sequence = int(row.get("packet_sequence", -1))
        if sequence <= previous_sequence or sequence in trace_sequences:
            raise AuditError("command_timeline packet sequence is duplicate or regressed")
        if sequence not in path_packet_qdot:
            raise AuditError("command_timeline sequence is absent from published PATH packets")
        reference_time = float(row.get("reference_time_s", math.nan))
        host_time = float(row.get("host_monotonic_s", math.nan))
        published_time = float(row.get("published_monotonic_s", math.nan))
        actual_dt = float(row.get("actual_dt_s", math.nan))
        solver_elapsed = float(row.get("solver_elapsed_s", math.nan))
        provider_elapsed = float(row.get("provider_elapsed_s", math.nan))
        for name, value in (
            ("reference_time_s", reference_time), ("host_monotonic_s", host_time),
            ("published_monotonic_s", published_time), ("actual_dt_s", actual_dt),
            ("solver_elapsed_s", solver_elapsed), ("provider_elapsed_s", provider_elapsed),
        ):
            if not math.isfinite(value):
                raise AuditError(f"command_timeline {name} is nonfinite")
        if not 0.0 <= reference_time < duration or actual_dt <= 0.0 \
                or solver_elapsed < 0.0 or provider_elapsed < 0.0:
            raise AuditError("command_timeline clock or compute-time value is out of range")
        if reference_time < previous_reference or host_time < previous_host \
                or published_time <= previous_published:
            raise AuditError("command_timeline clocks regress or published time repeats")
        twist = _finite_vector(row.get("requested_outer_twist_m_s_rad_s"), 6, "requested_outer_twist")
        lower = _finite_vector(row.get("solver_qdot_lower_rad_s"), 6, "solver_qdot_lower")
        upper = _finite_vector(row.get("solver_qdot_upper_rad_s"), 6, "solver_qdot_upper")
        jacobian = row.get("jacobian_6x6")
        if not isinstance(jacobian, list) or len(jacobian) != 6:
            raise AuditError("trace field jacobian_6x6 must be 6 by 6")
        for index, jacobian_row in enumerate(jacobian):
            _finite_vector(jacobian_row, 6, f"jacobian_6x6[{index}]")
        previous_qdot = _finite_vector(row.get("previous_published_qdot_rad_s"), 6, "previous_published_qdot")
        slew_lower = _finite_vector(row.get("slew_adjusted_qdot_lower_rad_s"), 6, "slew_adjusted_qdot_lower")
        slew_upper = _finite_vector(row.get("slew_adjusted_qdot_upper_rad_s"), 6, "slew_adjusted_qdot_upper")
        packet_qdot = _finite_vector(row.get("published_packet_qdot_rad_s"), 6, "published_packet_qdot")
        if any(lo > hi for lo, hi in zip(lower, upper, strict=True)) \
                or any(lo > hi for lo, hi in zip(slew_lower, slew_upper, strict=True)):
            raise AuditError("command_timeline contains inverted joint-velocity bounds")
        if any(value < lo - 1e-9 or value > hi + 1e-9
               for value, lo, hi in zip(packet_qdot, slew_lower, slew_upper, strict=True)):
            raise AuditError("published packet qdot is outside its captured slew-adjusted bounds")
        sent_qdot = path_packet_qdot[sequence]
        if any(not math.isclose(actual, sent, rel_tol=0.0, abs_tol=1e-12)
               for actual, sent in zip(packet_qdot, sent_qdot, strict=True)):
            raise AuditError("command_timeline qdot differs from its sent packet")
        scale = float(row.get("host_slew_scale", math.nan))
        if not math.isfinite(scale) or not 0.0 <= scale <= 1.0 + 1e-12:
            raise AuditError("command_timeline host_slew_scale is out of range")
        events = row.get("transition_events")
        if not isinstance(events, list):
            raise AuditError("command_timeline transition_events must be a list")
        for event in events:
            if not isinstance(event, dict):
                raise AuditError("command_timeline transition event is malformed")
            kind = event.get("event_type")
            if kind not in {"path_origin", "force_preempt_warm_start", "force_preempt_direction_retry"}:
                raise AuditError("command_timeline contains an unknown RNN state transition")
            _finite_vector(event.get("lambda_state"), 6, "transition.lambda_state")
            _finite_vector(event.get("theta_dot_state"), 6, "transition.theta_dot_state")
            transition_event_count += 1
            path_origin_events += int(kind == "path_origin")
        trace_sequences.add(sequence)
        previous_sequence = sequence
        previous_reference = reference_time
        previous_host = host_time
        previous_published = published_time
        first_time = reference_time if first_time is None else first_time
        last_time = reference_time
    if path_origin_events != 1:
        raise AuditError("command_timeline must contain exactly one PATH-origin RNN state")
    if not trace_sequences.issubset(path_sequences):
        raise AuditError("command_timeline sequence is absent from sealed host PATH packets")

    return {
        "schema": "tase.qp-replay-input-audit-v1",
        "claim_scope": "sealed_rnn_inputs_ready_for_offline_qp_solver_only",
        "offline_only": True,
        "hardware_or_endpoint_action": False,
        "attempt_dir": str(Path(attempt_dir).resolve()),
        "candidate_id": candidate_id,
        "method_identity": implementation,
        "protocol_id": PROTOCOL_ID,
        "parameters": parameters,
        "force_integral_policy": integral_policy,
        "seal_verified": True,
        "replay_evidence_complete": True,
        "formal_path_origin_host_monotonic_s": origin,
        "formal_path_packet_rows": path_packets,
        "formal_path_packet_rows_with_trace": len(trace_sequences),
        "formal_path_sequences_joined_to_rtde": len(trace_sequences & rtde_sequences),
        "formal_path_sequences_joined_to_sensor": len(trace_sequences & sensor_sequences),
        "command_timeline_rows": len(trace_sequences),
        "trace_reference_time_s": {"first": first_time, "last": last_time},
        "transition_event_count": transition_event_count,
        "metric_window": {
            "path_duration_s": duration,
            "complete_bins": int(metrics["complete_bins"]),
            "required_bins": int(metrics["required_bins"]),
            "normal_force_mae_n": metrics.get("normal_force_mae_n"),
        },
        "offline_qp_solver_run": False,
        "replay_status": "ready_for_offline_qp_solver_comparison",
        "not_computed": [
            "QP outputs/infeasibility count and equality or bound residuals",
            "QP solver timing and comparison against RNN solver timing",
            "any live QP eligibility or safety decision",
        ],
        "next_step": "run the existing strict offline QP comparison; keep all unapproved QP numbers out of live runtime",
    }


def inspect_attempt(attempt_dir: Path) -> dict[str, Any]:
    seal, result, segments = verify_seal(attempt_dir)
    binding = result.get("parameter_binding", {})
    candidate_id = binding.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise AuditError("attempt parameter candidate_id is required")
    if binding.get("protocol_id") != PROTOCOL_ID:
        raise AuditError("attempt parameter binding protocol_id differs")
    evidence = result.get("evidence", {})
    if not isinstance(evidence, dict):
        raise AuditError("attempt evidence metrics mapping is required")
    metrics = evidence.get("metrics")
    if not isinstance(metrics, dict):
        raise AuditError("attempt evidence.metrics mapping is required")
    if metrics.get("protocol_id") != PROTOCOL_ID:
        raise AuditError("attempt metrics protocol_id differs")
    parameters: dict[str, float] = {}
    for name in ("Md_scalar", "Bd_scalar", "force_integral_limit_n_s"):
        value = float(binding.get(name, math.nan))
        if not math.isfinite(value):
            raise AuditError(f"attempt parameter {name} is missing or nonfinite")
        parameters[name] = value
    integral_policy = binding.get("force_integral_policy")
    if not isinstance(integral_policy, str) or not integral_policy:
        raise AuditError("attempt force integral policy is missing")

    origin = float(result.get("timing", {}).get("path_started_monotonic_s", math.nan))
    if not math.isfinite(origin):
        raise AuditError("attempt omits formal PATH origin")
    path_packets = 0
    path_sequences: set[int] = set()
    path_packet_qdot: dict[int, tuple[float, ...]] = {}
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
        if packet.get("command_mode") == 2:
            sequence = int(packet["sequence"])
            if sequence in path_sequences:
                raise AuditError("published PATH packet sequence is duplicated")
            path_sequences.add(sequence)
            packet_has_joint_qdot &= len(values[13:19]) == 6
            path_packet_qdot[sequence] = tuple(float(value) for value in values[13:19])
            if stamp >= origin:
                path_packets += 1

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
    if timeline_rows:
        return inspect_captured_trace(
            attempt_dir=Path(attempt_dir), seal=seal, result=result,
            segments=segments, metrics=metrics, parameters=parameters,
            candidate_id=candidate_id, integral_policy=integral_policy,
            origin=origin, path_packets=path_packets,
            path_sequences=path_sequences, path_packet_qdot=path_packet_qdot,
            rtde_sequences=rtde_sequences, sensor_sequences=sensor_sequences,
        )
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
        "candidate_id": candidate_id,
        "protocol_id": PROTOCOL_ID,
        "parameters": parameters,
        "force_integral_policy": integral_policy,
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
    if report["replay_status"] == "ready_for_offline_qp_solver_comparison":
        window = report["metric_window"]
        times = report["trace_reference_time_s"]
        md = [
            "# TASE-QP offline replay input audit",
            "",
            "这条 sealed RNN reference trace 的逐周期输入已完整，可进入离线求解器比较；本审计没有运行 QP solver，也不构成 live QP qualification。",
            "",
            f"- Attempt: `{report['attempt_dir']}`；candidate `{report['candidate_id']}`；implementation `{report['method_identity']}`。",
            f"- RNN parameters: Md={report['parameters']['Md_scalar']}, Bd={report['parameters']['Bd_scalar']}, integral={report['parameters']['force_integral_limit_n_s']} N s ({report['force_integral_policy']})。",
            f"- Protocol: `{report['protocol_id']}`；PATH {window['path_duration_s']} s，正式 bins {window['complete_bins']}/{window['required_bins']}，feedback normal-force MAE={window['normal_force_mae_n']} N。",
            f"- Trace: {report['command_timeline_rows']} sealed rows，reference clock [{times['first']}, {times['last']}] s；PATH origin and RNN warm-start states are retained.",
            f"- Packet joins: trace rows match {report['formal_path_packet_rows_with_trace']} published PATH rows; {report['formal_path_sequences_joined_to_rtde']} join RTDE and {report['formal_path_sequences_joined_to_sensor']} join sensor observations.",
            "- The rows retain requested twist, measured dt, calibrated Jacobian, exact solver bounds, prior published qdot, slew-adjusted bounds, sent qdot, and solver/provider times.",
            "- Not computed: QP output, infeasibility, equality/bound residuals, solver timing comparison, or live eligibility.",
            "",
            "本审计只读本地 sealed files；未访问设备、运行 solver 或修改控制器。",
            "",
        ]
    else:
        md = [
            "# TASE-QP replay input audit",
            "",
            "这条 sealed attempt 缺少逐周期 replay inputs，不能做 matched RNN/QP replay。",
            "",
            f"- Attempt: `{report['attempt_dir']}`，candidate `{report['candidate_id']}`。",
            f"- Frozen values: Md={report['parameters']['Md_scalar']}, Bd={report['parameters']['Bd_scalar']}, integral limit={report['parameters']['force_integral_limit_n_s']} N s ({report['force_integral_policy']})。",
            f"- Protocol binding: `{report['protocol_id']}`；formal PATH published packet rows={report['formal_path_packet_rows']}；与 RTDE / sensor sequence 对上={report['formal_path_sequences_joined_to_rtde']} / {report['formal_path_sequences_joined_to_sensor']}。",
            f"- Sealed command_timeline rows: {report['command_timeline_rows']}.",
            "- QP rejection/infeasibility, equality/bound residual, and QP timing were not computed. No matched QP result is claimed.",
            "",
            "本审计只读本地 sealed files；未访问设备或运行 solver。",
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
