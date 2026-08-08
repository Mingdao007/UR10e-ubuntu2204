"""R008RAW1 binary raw-force sidecar: round-trip + cold-read MAE parity."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006.objective import (  # noqa: E402
    R006ObjectiveReceipt,
    build_receipt_from_samples,
    cold_read_verify,
)
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.bounded_sidecar_verify import (  # noqa: E402
    r008_bounded_sidecar_scope,
)
from step5d_autotune_v4_r008.raw_force_binary import (  # noqa: E402
    SOURCE_KEYS,
    decode_samples,
    encode_samples,
    load_receipt_mapping,
    prepare_slim_receipt,
    sha256_bytes,
    write_raw_sidecar_bytes,
)
from step5d_force_objective import ForcePathSample  # noqa: E402


def _sample(path_time_s: float, force_n: float, sequence: int) -> ForcePathSample:
    return ForcePathSample(
        path_time_s=path_time_s,
        path_phase=25,
        filtered_normal_n=force_n,
        source_sequences={key: sequence for key in SOURCE_KEYS},
        source_ages_s={key: 0.001 for key in SOURCE_KEYS},
        commanded_qdot=(0.01, -0.02, 0.03, -0.04, 0.05, -0.06),
        actual_qd=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        timestamp_s=1000.0 + path_time_s,
    )


def _complete_samples(force_n: float, *, sequence_offset: int = 0) -> tuple[ForcePathSample, ...]:
    samples = [_sample(0.05 + i * 0.1, force_n, sequence_offset + i + 1) for i in range(550)]
    samples.extend(
        _sample(55.05 + i * 0.1, force_n, sequence_offset + 550 + i + 1) for i in range(50)
    )
    return tuple(samples)


def test_encode_decode_round_trip_preserves_fields() -> None:
    samples = [s.as_dict() for s in _complete_samples(5.1)[:3]]
    samples[1]["timestamp_s"] = None
    payload = encode_samples(samples)
    restored = decode_samples(payload)
    assert restored == samples
    assert sha256_bytes(payload) == sha256_bytes(encode_samples(restored))
    assert len(payload) < 2_000


def test_binary_much_smaller_than_json_fixture(tmp_path: Path) -> None:
    samples = [s.as_dict() for s in _complete_samples(5.25)]
    json_bytes = json.dumps({"samples": samples}, separators=(",", ":")).encode()
    raw = encode_samples(samples)
    # Columnar float64 still beats verbose JSON keys; live ~28k samples win more.
    assert len(raw) < len(json_bytes)
    assert len(raw) < 130_000
    path = tmp_path / "x.r008raw"
    write_raw_sidecar_bytes(path, raw)
    assert path.read_bytes() == raw


def test_cold_read_mae_equal_json_vs_binary(tmp_path: Path) -> None:
    contract = load_contract()
    receipt = build_receipt_from_samples(
        _complete_samples(5.25),
        attempt_sequence=7,
        execution_id="r008-raw-binary-mae",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    full = receipt.as_dict()
    json_path = tmp_path / "full.json"
    json_path.write_text(json.dumps(full, sort_keys=True) + "\n", encoding="utf-8")

    slim = dict(full)
    slim["raw_bundle"] = dict(full["raw_bundle"])
    slim["metadata"] = dict(full["metadata"])
    binary_path = tmp_path / "slim.json"
    raw_payload = prepare_slim_receipt(slim, artifact_json_path=binary_path)
    assert raw_payload is not None
    write_raw_sidecar_bytes(binary_path.with_suffix(".r008raw"), raw_payload)
    binary_path.write_text(json.dumps(slim, sort_keys=True) + "\n", encoding="utf-8")
    assert binary_path.stat().st_size < json_path.stat().st_size // 10
    assert slim["raw_bundle"]["samples"] == []
    assert binary_path.with_suffix(".r008raw").is_file()

    verified_json = cold_read_verify(
        R006ObjectiveReceipt.from_mapping(json.loads(json_path.read_text())),
        expected_campaign_fingerprint=contract.campaign_fingerprint,
    )
    verified_bin = cold_read_verify(
        R006ObjectiveReceipt.from_mapping(load_receipt_mapping(binary_path)),
        expected_campaign_fingerprint=contract.campaign_fingerprint,
    )
    assert verified_bin.objective_mae_n == verified_json.objective_mae_n
    assert verified_bin.raw_bundle_digest == verified_json.raw_bundle_digest
    assert verified_bin.trainable is True


def test_bounded_append_writes_r008raw_and_verifies(tmp_path: Path) -> None:
    contract = load_contract()
    receipt = build_receipt_from_samples(
        _complete_samples(5.1, sequence_offset=100),
        attempt_sequence=1,
        execution_id="r008-raw-binary-append",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    with r008_bounded_sidecar_scope(tail_rows=1):
        sidecar = R006ObjectiveSidecar(
            tmp_path / "r006-objectives.jsonl",
            campaign_fingerprint=contract.campaign_fingerprint,
        )
        row = sidecar.append(
            receipt,
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="SPACEFILL",
            point_key=list(ANCHOR_POINT.key),
        )
    artifact = Path(row["artifact_path"])
    assert artifact.is_file()
    raw = artifact.with_suffix(".r008raw")
    assert raw.is_file()
    stub = json.loads(artifact.read_text(encoding="utf-8"))
    assert stub["raw_bundle"]["samples"] == []
    assert raw.is_file() and raw.stat().st_size > 0
    # Combined on-disk size must beat a full JSON receipt (samples embedded).
    full_json_size = len(json.dumps(receipt.as_dict(), separators=(",", ":")).encode())
    assert artifact.stat().st_size + raw.stat().st_size < full_json_size
    assert row["trainable"] is True
    assert row["objective_mae_n"] == receipt.objective_mae_n


def test_legacy_full_json_still_verifies(tmp_path: Path) -> None:
    """Back-compat: existing ~18MB artifacts without .r008raw still cold-verify."""

    contract = load_contract()
    receipt = build_receipt_from_samples(
        _complete_samples(5.0, sequence_offset=200),
        attempt_sequence=2,
        execution_id="r008-raw-binary-legacy",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    # Write full JSON via stock sidecar (outside r008 scope).
    sidecar = R006ObjectiveSidecar(
        tmp_path / "legacy.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
    )
    row = sidecar.append(
        receipt,
        epoch=1,
        candidate_uid=ANCHOR_POINT.uid,
        kind="SPACEFILL",
        point_key=list(ANCHOR_POINT.key),
    )
    artifact = Path(row["artifact_path"])
    assert not artifact.with_suffix(".r008raw").exists()
    stub = json.loads(artifact.read_text(encoding="utf-8"))
    assert len(stub["raw_bundle"]["samples"]) == receipt.sample_count
    # r008 fresh path must still accept legacy JSON.
    from step5d_autotune_v4_r008.bounded_sidecar_verify import _r008_fresh_verify_artifact

    verified = _r008_fresh_verify_artifact(artifact, contract.campaign_fingerprint)
    assert verified.trainable is True
    assert verified.objective_mae_n == receipt.objective_mae_n
