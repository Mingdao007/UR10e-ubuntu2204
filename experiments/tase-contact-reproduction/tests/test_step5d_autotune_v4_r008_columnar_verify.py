"""Columnar cold-verify parity vs stock cold_read_verify (seal Phase 2)."""

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
from step5d_autotune_v4_r008.raw_force_binary import SOURCE_KEYS  # noqa: E402
from step5d_autotune_v4_r008.raw_force_columnar_verify import (  # noqa: E402
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


def _complete_samples(force_n: float, *, sequence_offset: int = 0) -> tuple[ForcePathSample, ...]:
    samples = [_sample(0.05 + i * 0.1, force_n, sequence_offset + i + 1) for i in range(550)]
    samples.extend(
        _sample(55.05 + i * 0.1, force_n, sequence_offset + 550 + i + 1) for i in range(50)
    )
    return tuple(samples)


def test_columnar_matches_cold_read_mae_and_digests() -> None:
    contract = load_contract()
    # builder_sealed then stock cold_read (same as production finalize→verify).
    sealed = build_receipt_from_samples(
        _complete_samples(5.25),
        attempt_sequence=3,
        execution_id="r008-columnar-parity",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    # Re-run stock cold_read on builder_sealed clone via as_dict roundtrip.
    builder_sealed = R006ObjectiveReceipt.from_mapping(
        {**sealed.as_dict(), "verification_state": "builder_sealed"}
    )
    stock = cold_read_verify(
        builder_sealed, expected_campaign_fingerprint=contract.campaign_fingerprint
    )
    columnar = columnar_verify_receipt(
        builder_sealed, expected_campaign_fingerprint=contract.campaign_fingerprint
    )
    assert columnar.objective_mae_n == stock.objective_mae_n
    assert columnar.raw_bundle_digest == stock.raw_bundle_digest
    assert columnar.sufficient_statistics_digest == stock.sufficient_statistics_digest
    assert columnar.source_sequence_time_identity_digest == stock.source_sequence_time_identity_digest
    assert columnar.trainable is True


def test_columnar_mismatch_fails_closed() -> None:
    contract = load_contract()
    sealed = build_receipt_from_samples(
        _complete_samples(5.0, sequence_offset=50),
        attempt_sequence=4,
        execution_id="r008-columnar-mismatch",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    from dataclasses import replace

    from step5d_autotune_v4_r006.contracts import canonical_bytes, sha256_bytes
    from step5d_autotune_v4_r006.objective import R006ObjectiveError

    # Re-seal a receipt that lies about MAE; columnar recompute must reject it.
    lying = replace(sealed, verification_state="builder_sealed", objective_mae_n=1.0)
    lying = replace(lying, builder_seal_sha256=sha256_bytes(canonical_bytes(lying._seal_payload())))
    with pytest.raises(R006ObjectiveError, match="objective_mae_n differs"):
        columnar_verify_receipt(
            lying, expected_campaign_fingerprint=contract.campaign_fingerprint
        )


def test_bounded_append_binary_trainable_via_columnar(tmp_path: Path) -> None:
    contract = load_contract()
    receipt = build_receipt_from_samples(
        _complete_samples(5.1, sequence_offset=90),
        attempt_sequence=1,
        execution_id="r008-columnar-append",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    # Append wants builder_sealed; strip verified state for a realistic sink.
    builder = R006ObjectiveReceipt.from_mapping(
        {**receipt.as_dict(), "verification_state": "builder_sealed"}
    )
    with r008_bounded_sidecar_scope(tail_rows=1):
        sidecar = R006ObjectiveSidecar(
            tmp_path / "obj.jsonl", campaign_fingerprint=contract.campaign_fingerprint
        )
        row = sidecar.append(
            builder,
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="BO_TRIAL",
            point_key=list(ANCHOR_POINT.key),
        )
    assert row["trainable"] is True
    assert Path(row["artifact_path"]).with_suffix(".r008raw").is_file()
    stub = json.loads(Path(row["artifact_path"]).read_text(encoding="utf-8"))
    assert stub["raw_bundle"]["samples"] == []
