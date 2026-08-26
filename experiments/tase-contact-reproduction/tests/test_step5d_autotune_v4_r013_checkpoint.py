from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r013.checkpoint import (  # noqa: E402
    R013CheckpointError,
    R013CheckpointReceiptV1,
    build_checkpoint_receipt,
)
from step5d_autotune_v4_r013.checkpoint_manifest import (  # noqa: E402
    R013CheckpointManifestError,
    R013CheckpointManifestV1,
    materialize_checkpoint_manifest,
)
from step5d_autotune_v4_r013.domain import candidate_to_log_features  # noqa: E402
from step5d_autotune_v4_r013.floor_coordinator import (  # noqa: E402
    CORE_PROPOSAL_CONTRACT,
    FloorCandidateProposal,
    FloorDiscoveryCoordinator,
    FloorDiscoveryPolicyV1,
    BlockProposalContract,
)


def _coordinator() -> FloorDiscoveryCoordinator:
    def core_provider(pool: Sequence[dict[str, Any]]) -> FloorCandidateProposal:
        candidate = pool[0]
        return FloorCandidateProposal(
            candidate=candidate,
            block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
            contract=CORE_PROPOSAL_CONTRACT,
            acquisition_value=0.1,
            posterior_beating_probability=0.0,
            fit_receipt={"backend": "checkpoint-test", "model_dimensions": 4},
        )

    def correction_provider(pool: Sequence[tuple[float, ...]]) -> FloorCandidateProposal:
        return FloorCandidateProposal(
            candidate={},
            block_input=tuple(pool[0]),
            contract=BlockProposalContract("correction", 6),
            acquisition_value=0.1,
            posterior_beating_probability=0.0,
            fit_receipt={"backend": "checkpoint-test", "model_dimensions": 6},
        )

    return FloorDiscoveryCoordinator(
        FloorDiscoveryPolicyV1(),
        fingerprint="checkpoint-fingerprint",
        core_proposal_provider=core_provider,
        correction_proposal_provider=correction_provider,
    )


def _advance(coordinator: FloorDiscoveryCoordinator, *, target: int | None = None) -> None:
    while not coordinator.complete and (target is None or coordinator.novel_count < target):
        request = coordinator.next_request()
        coordinator.record_result(
            admitted=True,
            complete=True,
            sealed=True,
            sealed_mae_n=0.2 if request.novel_index == 4 else 0.8,
            posterior_beating_probability=0.3 if request.novel_index == 4 else 0.0,
            guardrails_passed=True,
            receipt_id=f"receipt-{request.request_id}",
        )


def test_numeric_checkpoint_requires_exact_observed_count_and_round_trips() -> None:
    coordinator = _coordinator()
    with pytest.raises(R013CheckpointError, match="exact observed count"):
        build_checkpoint_receipt(coordinator, 100)

    _advance(coordinator, target=100)
    receipt = build_checkpoint_receipt(coordinator, 100)
    assert receipt.ready is True
    assert receipt.checkpoint_only is True
    assert receipt.empirical_floor_claimed is False
    assert receipt.observed_novel_count == 100
    assert receipt.single_trial_min_n == 0.2
    assert R013CheckpointReceiptV1.from_mapping(receipt.as_dict()) == receipt


def test_final_checkpoint_requires_complete_top3_n5() -> None:
    coordinator = _coordinator()
    _advance(coordinator)
    receipt = build_checkpoint_receipt(coordinator, "final")
    assert receipt.ready is True
    assert receipt.complete is True
    assert receipt.empirical_floor_claimed is True
    assert len(receipt.final_top3) == 3
    assert all(group.n >= 5 for group in receipt.final_top3)
    assert R013CheckpointReceiptV1.from_mapping(receipt.as_dict()) == receipt


def test_checkpoint_rejects_fingerprint_and_malformed_novel_row() -> None:
    coordinator = _coordinator()
    _advance(coordinator, target=100)
    with pytest.raises(R013CheckpointError, match="fingerprint mismatch"):
        build_checkpoint_receipt(coordinator, 100, fingerprint="wrong")

    coordinator.novel_rows.append({"canonical_token": "unsealed"})
    with pytest.raises(R013CheckpointError, match="incomplete or unsealed"):
        build_checkpoint_receipt(coordinator, 100)


def test_checkpoint_receipt_is_cold_readable_from_event_log() -> None:
    coordinator = _coordinator()
    _advance(coordinator, target=100)
    replay = FloorDiscoveryCoordinator.from_records(
        FloorDiscoveryPolicyV1(),
        coordinator.event_log,
        fingerprint="checkpoint-fingerprint",
        core_proposal_provider=_coordinator().core_proposal_provider,
        correction_proposal_provider=_coordinator().correction_proposal_provider,
    )
    assert build_checkpoint_receipt(replay, 100) == build_checkpoint_receipt(coordinator, 100)


def test_checkpoint_manifest_materializes_only_observed_boundaries() -> None:
    coordinator = _coordinator()
    partial = materialize_checkpoint_manifest(coordinator)
    assert partial.receipts == ()
    _advance(coordinator, target=100)
    at_100 = materialize_checkpoint_manifest(coordinator)
    assert [receipt.checkpoint for receipt in at_100.receipts] == [100]
    assert R013CheckpointManifestV1.from_mapping(at_100.as_dict()) == at_100

    _advance(coordinator)
    complete = materialize_checkpoint_manifest(coordinator)
    assert [receipt.checkpoint for receipt in complete.receipts] == [100, 160, 200, "final"]
    assert complete.receipts[-1].empirical_floor_claimed is True


def test_checkpoint_manifest_rejects_malformed_event_log() -> None:
    coordinator = _coordinator()
    coordinator._records.append({"event": "result", "request_id": "missing"})
    with pytest.raises(R013CheckpointManifestError, match="lacks its request"):
        materialize_checkpoint_manifest(coordinator)


def test_checkpoint_manifest_rejects_future_receipt_round_trip() -> None:
    coordinator = _coordinator()
    _advance(coordinator, target=100)
    receipt = build_checkpoint_receipt(coordinator, 100)
    with pytest.raises(R013CheckpointManifestError, match="future receipt"):
        R013CheckpointManifestV1(
            fingerprint="checkpoint-fingerprint",
            current_novel_count=0,
            current_state="running",
            current_complete=False,
            receipts=(receipt,),
            event_count=1,
        )


def test_checkpoint_receipt_cold_read_rejects_count_and_final_n5_drift() -> None:
    coordinator = _coordinator()
    _advance(coordinator, target=100)
    numeric = build_checkpoint_receipt(coordinator, 100).as_dict()
    with pytest.raises(R013CheckpointError, match="observed count differs"):
        R013CheckpointReceiptV1.from_mapping({**numeric, "observed_novel_count": 99})

    _advance(coordinator)
    final = build_checkpoint_receipt(coordinator, "final").as_dict()
    malformed_groups = [dict(group) for group in final["final_top3"]]
    malformed_groups[0]["n"] = 1
    with pytest.raises(R013CheckpointError, match="n>=5"):
        R013CheckpointReceiptV1.from_mapping({**final, "final_top3": malformed_groups})

    with pytest.raises(R013CheckpointError, match="not final/complete"):
        R013CheckpointReceiptV1.from_mapping({**final, "complete": False})
