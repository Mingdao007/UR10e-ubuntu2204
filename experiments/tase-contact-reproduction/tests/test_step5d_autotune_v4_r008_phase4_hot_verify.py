"""Seal Phase 4: hot-append in-memory columnar verify + encode dedupe."""

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
    build_receipt_from_samples,
    cold_read_verify,
)
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.bounded_sidecar_verify import (  # noqa: E402
    r008_bounded_sidecar_scope,
)
from step5d_autotune_v4_r008.raw_force_binary import (  # noqa: E402
    SOURCE_KEYS,
    load_receipt_mapping,
    try_encode_samples,
)
from step5d_autotune_v4_r008.raw_force_columnar_verify import (  # noqa: E402
    columnar_verify_hot_append,
    columnar_verify_receipt,
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


def _complete_samples(force_n: float = 5.25) -> tuple[ForcePathSample, ...]:
    samples = [_sample(0.05 + i * 0.1, force_n, i + 1) for i in range(550)]
    samples.extend(_sample(55.05 + i * 0.1, force_n, 551 + i) for i in range(50))
    return tuple(samples)


def test_try_encode_samples_single_pass() -> None:
    samples = [s.as_dict() for s in _complete_samples(5.0)[:3]]
    payload = try_encode_samples(samples)
    assert payload is not None and len(payload) > 0
    assert try_encode_samples([]) is None
    assert try_encode_samples([{"path_time_s": 0.0}]) is None


def test_hot_append_matches_cold_read_mae(tmp_path: Path) -> None:
    contract = load_contract()
    samples = _complete_samples(5.15)
    receipt = build_receipt_from_samples(
        samples,
        attempt_sequence=1,
        execution_id="phase4-hot",
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
            kind="STAIRCASE",
            point_key=list(ANCHOR_POINT.key),
        )
    artifact = Path(row["artifact_path"])
    assert artifact.with_suffix(".r008raw").is_file()
    stub = json.loads(artifact.read_text(encoding="utf-8"))
    assert stub["raw_bundle"]["samples"] == []
    assert row["trainable"] is True
    assert row["objective_mae_n"] == receipt.objective_mae_n

    cold = cold_read_verify(
        load_receipt_mapping(artifact),
        expected_campaign_fingerprint=contract.campaign_fingerprint,
    )
    assert cold.objective_mae_n == row["objective_mae_n"]
    assert cold.raw_bundle_digest == receipt.raw_bundle_digest


def test_columnar_hot_append_binds_bytes(tmp_path: Path) -> None:
    contract = load_contract()
    receipt = build_receipt_from_samples(
        _complete_samples(5.0),
        attempt_sequence=2,
        execution_id="phase4-bind",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    receipt_dict = receipt.as_dict()
    samples = receipt_dict["raw_bundle"]["samples"]
    raw_payload = try_encode_samples(samples)
    assert raw_payload is not None
    slim = dict(receipt_dict)
    slim["raw_bundle"] = dict(receipt_dict["raw_bundle"])
    slim["raw_bundle"]["samples"] = []
    from step5d_autotune_v4_r006.contracts import canonical_bytes

    encoded = canonical_bytes(slim) + b"\n"
    path = tmp_path / "x.json"
    path.write_bytes(encoded)
    path.with_suffix(".r008raw").write_bytes(raw_payload)

    verified = columnar_verify_hot_append(
        receipt,
        expected_campaign_fingerprint=contract.campaign_fingerprint,
        artifact_path=path,
        encoded_stub=encoded,
        raw_payload=raw_payload,
    )
    assert verified.verification_state == "verified_raw_artifact"
    assert verified.objective_mae_n == receipt.objective_mae_n

    with pytest.raises(Exception):
        columnar_verify_hot_append(
            receipt,
            expected_campaign_fingerprint=contract.campaign_fingerprint,
            artifact_path=path,
            encoded_stub=encoded + b"x",
            raw_payload=raw_payload,
        )

    # Cold columnar on hydrated mapping still matches.
    hydrated = load_receipt_mapping(path)
    cold_col = columnar_verify_receipt(
        hydrated, expected_campaign_fingerprint=contract.campaign_fingerprint
    )
    assert cold_col.objective_mae_n == verified.objective_mae_n
