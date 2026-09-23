import hashlib
import json

import pytest

from audit_tase_qp_replay_inputs import (
    AuditError,
    PROTOCOL_ID,
    SEGMENTS,
    inspect_attempt,
    verify_seal,
)


def _make_attempt(root):
    root.mkdir()
    segment_manifest = {}
    for name in SEGMENTS:
        path = root / f"{name}.jsonl"
        if name == "command_timeline":
            path.write_text("", encoding="utf-8")
            rows = 0
        elif name == "published_packets":
            packet = {"command_mode": 2, "sequence": 1, "double_values": [0.0] * 24}
            path.write_text(json.dumps([10.0, packet]) + "\n", encoding="utf-8")
            rows = 1
        elif name == "robot_frames":
            path.write_text(json.dumps({"consumed_packet_sequence": 1}) + "\n", encoding="utf-8")
            rows = 1
        elif name == "raw_sensor":
            path.write_text(json.dumps({"packet_sequence": 1}) + "\n", encoding="utf-8")
            rows = 1
        else:
            path.write_text('{"row":1}\n', encoding="utf-8")
            rows = 1
        segment_manifest[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "count": rows,
        }
    service_path = root / "service_observations.jsonl"
    service_path.write_text('{"row":1}\n', encoding="utf-8")
    service_manifest = {
        "path": str(service_path),
        "sha256": hashlib.sha256(service_path.read_bytes()).hexdigest(),
        "count": 1,
    }
    seal = {
        "schema": "yield-live-entry/attempt-seal-v1",
        "lifecycle": {"sealed": True},
        "segments": segment_manifest,
        "service_observations": service_manifest,
    }
    (root / "seal.json").write_text(json.dumps(seal), encoding="utf-8")
    result = {
        "parameter_binding": {
            "candidate_id": "confirm-s02-b00-A",
            "Md_scalar": 9.565272137974492,
            "Bd_scalar": 693.6559295653944,
            "force_integral_limit_n_s": 1.0,
            "protocol_id": "figure8_window60_r013_rate400_v1",
        },
        "evidence": {"metrics": {"protocol_id": PROTOCOL_ID}},
        "timing": {"path_started_monotonic_s": 5.0},
        "state": {"last_result": {"actual_dt_s": 0.002,
            "outer_output_feedback_pending": {"requested_twist": [0.0] * 6}}},
        "sealed_evidence": {
            "schema": seal["schema"],
            "lifecycle": {"sealed": True},
            "segments": segment_manifest,
            "service_observations": service_manifest,
        },
    }
    (root / "attempt-result.json").write_text(json.dumps(result), encoding="utf-8")
    return root


def test_verify_seal_checks_all_segment_hashes_and_counts(tmp_path):
    seal, _, verified = verify_seal(_make_attempt(tmp_path / "attempt"))
    assert seal["lifecycle"]["sealed"] is True
    assert set(verified) == set(SEGMENTS) | {"service_observations"}
    assert verified["command_timeline"]["rows"] == 0


def test_input_audit_refuses_matched_replay_when_per_tick_fields_are_absent(tmp_path):
    report = inspect_attempt(_make_attempt(tmp_path / "attempt"))
    assert report["formal_path_packet_rows"] == 1
    assert report["formal_path_sequences_joined_to_rtde"] == 1
    assert report["formal_path_sequences_joined_to_sensor"] == 1
    assert report["command_timeline_rows"] == 0
    assert report["replay_status"] == "blocked_by_missing_sealed_per_tick_inputs"
    assert report["packet_fields"]["per_sample_outer_twist_or_actual_dt_or_reference_time_present"] is False


def test_verify_seal_rejects_changed_source_bytes(tmp_path):
    attempt = _make_attempt(tmp_path / "attempt")
    (attempt / "robot_frames.jsonl").write_text('{"row":2}\n', encoding="utf-8")
    with pytest.raises(AuditError, match="digest/count"):
        verify_seal(attempt)
    linked_attempt = _make_attempt(tmp_path / "linked-attempt")
    external = tmp_path / "external.jsonl"
    external.write_text('{"row":1}\n', encoding="utf-8")
    segment = linked_attempt / "robot_frames.jsonl"
    segment.unlink()
    segment.symlink_to(external)
    with pytest.raises(AuditError, match="symlink"):
        verify_seal(linked_attempt)


def _rewrite_seal_copy(attempt, name, *, service=False):
    path = attempt / f"{name}.jsonl"
    manifest = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "count": sum(1 for _ in path.open("rb")),
    }
    seal_path = attempt / "seal.json"
    result_path = attempt / "attempt-result.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if service:
        seal["service_observations"] = manifest
        result["sealed_evidence"]["service_observations"] = manifest
    else:
        seal["segments"][name] = manifest
        result["sealed_evidence"]["segments"][name] = manifest
    seal_path.write_text(json.dumps(seal), encoding="utf-8")
    result_path.write_text(json.dumps(result), encoding="utf-8")


def test_input_audit_rejects_unrelated_protocol_in_binding_or_metrics(tmp_path):
    binding_attempt = _make_attempt(tmp_path / "binding")
    result_path = binding_attempt / "attempt-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["parameter_binding"]["protocol_id"] = "figure8_window60_r013_v1"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(AuditError, match="binding protocol_id"):
        inspect_attempt(binding_attempt)

    metrics_attempt = _make_attempt(tmp_path / "metrics")
    result_path = metrics_attempt / "attempt-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["evidence"]["metrics"]["protocol_id"] = "figure8_window60_r013_v1"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(AuditError, match="metrics protocol_id"):
        inspect_attempt(metrics_attempt)


@pytest.mark.parametrize("metrics_state", ["missing", "null"])
def test_input_audit_requires_metrics_mapping(tmp_path, metrics_state):
    attempt = _make_attempt(tmp_path / metrics_state)
    result_path = attempt / "attempt-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if metrics_state == "missing":
        del result["evidence"]["metrics"]
    else:
        result["evidence"]["metrics"] = None
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(AuditError, match="evidence.metrics mapping is required"):
        inspect_attempt(attempt)


@pytest.mark.parametrize("surface", ["packet", "service", "timeline", "history"])
def test_input_audit_fails_closed_when_per_tick_fields_or_history_appear(tmp_path, surface):
    attempt = _make_attempt(tmp_path / surface)
    if surface == "packet":
        path = attempt / "published_packets.jsonl"
        stamp, packet = json.loads(path.read_text(encoding="utf-8"))
        packet["requested_twist"] = [0.0] * 6
        path.write_text(json.dumps([stamp, packet]) + "\n", encoding="utf-8")
        _rewrite_seal_copy(attempt, "published_packets")
        expected_error = "per-packet replay input fields"
    elif surface == "service":
        path = attempt / "service_observations.jsonl"
        path.write_text(json.dumps({"label": "published_packets", "row": {"actual_dt_s": 0.002}}) + "\n", encoding="utf-8")
        _rewrite_seal_copy(attempt, "service_observations", service=True)
        expected_error = "service observation replay input fields"
    elif surface == "timeline":
        path = attempt / "command_timeline.jsonl"
        path.write_text(json.dumps({"reference_time_s": 0.0}) + "\n", encoding="utf-8")
        _rewrite_seal_copy(attempt, "command_timeline")
        expected_error = "command_timeline contains"
    else:
        result_path = attempt / "attempt-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["state"]["per_tick_results"] = [{"requested_twist": [0.0] * 6}]
        result_path.write_text(json.dumps(result), encoding="utf-8")
        expected_error = "per-tick result history"
    with pytest.raises(AuditError, match=expected_error):
        inspect_attempt(attempt)
