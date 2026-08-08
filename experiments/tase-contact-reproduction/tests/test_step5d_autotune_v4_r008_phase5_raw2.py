"""Seal Phase 5: R008RAW2 codec + binary-native finalize/append."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006 import objective as r006_objective  # noqa: E402
from step5d_autotune_v4_r006.objective import (  # noqa: E402
    R006ObjectiveBuilder,
    build_receipt_from_samples,
)
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.binary_seal import (  # noqa: E402
    R008_OBJECTIVE_RECEIPT_VERSION,
    peek_pending_raw2,
    r008_binary_seal_scope,
)
from step5d_autotune_v4_r008.bounded_sidecar_verify import (  # noqa: E402
    r008_bounded_sidecar_scope,
)
from step5d_autotune_v4_r008.raw_force_binary import (  # noqa: E402
    SOURCE_KEYS,
    encode_samples as encode_samples_v1,
)
from step5d_autotune_v4_r008.raw_force_binary_v2 import (  # noqa: E402
    MAGIC_V1,
    MAGIC_V2,
    decode_samples_v2,
    detect_and_decode,
    detect_magic,
    encode_samples_v2,
    seal_sha256_of,
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


def test_r008raw2_encode_decode_round_trip() -> None:
    samples = [s.as_dict() for s in _complete_samples(5.1)[:3]]
    samples[1]["timestamp_s"] = None
    full, seal_region, digest = encode_samples_v2(samples)
    assert full.startswith(MAGIC_V2)
    assert full.startswith(seal_region)
    assert seal_sha256_of(full) == digest
    restored = decode_samples_v2(full, include_audit=True)
    assert restored == samples
    seal_only = decode_samples_v2(full, include_audit=False)
    assert seal_only[0]["path_time_s"] == samples[0]["path_time_s"]
    assert seal_only[0]["filtered_normal_n"] == samples[0]["filtered_normal_n"]
    assert seal_only[0]["source_sequences"] == samples[0]["source_sequences"]


def test_seal_digest_ignores_audit_ages() -> None:
    samples_a = [s.as_dict() for s in _complete_samples(5.25)[:5]]
    samples_b = copy.deepcopy(samples_a)
    for row in samples_b:
        for key in SOURCE_KEYS:
            row["source_ages_s"][key] = 0.055
        row["commanded_qdot"] = [x + 0.1 for x in row["commanded_qdot"]]
        row["actual_qd"] = [0.1] * 6

    full_a, seal_a, dig_a = encode_samples_v2(samples_a)
    full_b, seal_b, dig_b = encode_samples_v2(samples_b)
    assert dig_a == dig_b
    assert seal_a == seal_b
    assert seal_sha256_of(full_a) == seal_sha256_of(full_b) == dig_a

    mutated = bytearray(full_a)
    # Flip a byte in the audit suffix only.
    mutated[-1] ^= 0xFF
    assert seal_sha256_of(bytes(mutated)) == dig_a
    assert bytes(mutated) != full_a


def test_detect_and_decode_dual_read_raw1_and_raw2() -> None:
    samples = [s.as_dict() for s in _complete_samples(5.0)[:4]]
    raw1 = encode_samples_v1(samples)
    raw2, _, _ = encode_samples_v2(samples)
    assert detect_magic(raw1) == MAGIC_V1
    assert detect_magic(raw2) == MAGIC_V2
    decoded1 = detect_and_decode(raw1)
    decoded2 = detect_and_decode(raw2)
    assert decoded1 == samples
    assert decoded2 == samples


def test_binary_finalize_under_scope_matches_stock_mae() -> None:
    contract = load_contract()
    samples = _complete_samples(5.25, sequence_offset=10)
    stock = build_receipt_from_samples(
        samples,
        attempt_sequence=7,
        execution_id="r008-phase5-stock-mae",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    assert stock.version != R008_OBJECTIVE_RECEIPT_VERSION
    assert len(stock.raw_bundle["samples"]) == len(samples)

    with r008_binary_seal_scope():
        builder = R006ObjectiveBuilder(
            attempt_sequence=8,
            execution_id="r008-phase5-binary-mae",
            campaign_fingerprint=contract.campaign_fingerprint,
            candidate_uid=ANCHOR_POINT.uid,
        )
        for sample in samples:
            builder.add(sample)
        sealed = builder.finalize()
        assert sealed.version == R008_OBJECTIVE_RECEIPT_VERSION
        assert sealed.raw_bundle["samples"] == []
        pending = peek_pending_raw2(sealed.attempt_sequence, sealed.execution_id)
        assert pending is not None and pending.startswith(MAGIC_V2)
        assert sealed.raw_bundle_digest == seal_sha256_of(pending)
        assert sealed.raw_bundle.get("seal_block_sha256") == sealed.raw_bundle_digest

        # Use module attribute so r008_binary_seal_scope's patch is visible.
        verified = r006_objective.cold_read_verify(
            sealed, expected_campaign_fingerprint=contract.campaign_fingerprint
        )
        assert verified.trainable is True
        assert verified.objective_mae_n == stock.objective_mae_n

        # Same samples via build_receipt_from_samples under scope.
        built = build_receipt_from_samples(
            samples,
            attempt_sequence=9,
            execution_id="r008-phase5-built-mae",
            campaign_fingerprint=contract.campaign_fingerprint,
            candidate_uid=ANCHOR_POINT.uid,
        )
        assert built.version == R008_OBJECTIVE_RECEIPT_VERSION
        assert built.raw_bundle["samples"] == []
        pending_built = peek_pending_raw2(built.attempt_sequence, built.execution_id)
        assert pending_built is not None
        assert built.raw_bundle_digest == seal_sha256_of(pending_built)
        assert built.objective_mae_n == stock.objective_mae_n


def test_bounded_append_writes_r008raw2_and_verifies(tmp_path: Path) -> None:
    contract = load_contract()
    samples = _complete_samples(5.1, sequence_offset=100)
    with r008_bounded_sidecar_scope(tail_rows=1):
        receipt = build_receipt_from_samples(
            samples,
            attempt_sequence=1,
            execution_id="r008-phase5-append",
            campaign_fingerprint=contract.campaign_fingerprint,
            candidate_uid=ANCHOR_POINT.uid,
        )
        assert receipt.version == R008_OBJECTIVE_RECEIPT_VERSION
        # Live sink strips to builder_sealed before append.
        from dataclasses import replace

        builder_sealed = replace(receipt, verification_state="builder_sealed")
        sidecar = R006ObjectiveSidecar(
            tmp_path / "r006-objectives.jsonl",
            campaign_fingerprint=contract.campaign_fingerprint,
        )
        row = sidecar.append(
            builder_sealed,
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="SPACEFILL",
            point_key=list(ANCHOR_POINT.key),
        )
    artifact = Path(row["artifact_path"])
    stub = json.loads(artifact.read_text(encoding="utf-8"))
    raw = artifact.with_suffix(".r008raw")
    assert stub["version"] == R008_OBJECTIVE_RECEIPT_VERSION
    assert stub["raw_bundle"]["samples"] == []
    assert raw.is_file()
    raw_bytes = raw.read_bytes()
    assert raw_bytes.startswith(MAGIC_V2)
    assert stub["raw_bundle_digest"] == seal_sha256_of(raw_bytes)
    assert row["trainable"] is True
    assert row["objective_mae_n"] == receipt.objective_mae_n
