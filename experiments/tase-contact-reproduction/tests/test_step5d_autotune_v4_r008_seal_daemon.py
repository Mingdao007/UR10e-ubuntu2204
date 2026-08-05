"""Offline tests for the long-lived r008 seal daemon (sole writer)."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r005.observations import ObservationLedger  # noqa: E402
from step5d_autotune_v4_r005.contracts import Candidate  # noqa: E402
from step5d_autotune_v4_r005.runtime import AttemptResult  # noqa: E402
from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006.live_adapter import R006ObservationLedger  # noqa: E402
from step5d_autotune_v4_r006.objective import build_receipt_from_samples  # noqa: E402
from step5d_autotune_v4_r006.sidecar import GENESIS_SHA256, R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.async_seal import AsyncSealPipeline  # noqa: E402
from step5d_autotune_v4_r008.seal_client import SealDaemonClient  # noqa: E402
from step5d_force_objective import ForcePathSample  # noqa: E402


EOAT = "a" * 64


def _sample(path_time_s: float, force_n: float, sequence: int) -> ForcePathSample:
    return ForcePathSample(
        path_time_s=path_time_s,
        path_phase=25,
        filtered_normal_n=force_n,
        source_sequences={"controller": sequence, "rtde": sequence},
        source_ages_s={"controller": 0.001, "rtde": 0.001},
        timestamp_s=1000.0 + path_time_s,
    )


def _complete_samples(force_n: float, *, sequence_offset: int = 0) -> tuple[ForcePathSample, ...]:
    samples = [_sample(0.05 + i * 0.1, force_n, sequence_offset + i + 1) for i in range(550)]
    samples.extend(
        _sample(55.05 + i * 0.1, force_n, sequence_offset + 550 + i + 1) for i in range(50)
    )
    return tuple(samples)


def _r006_candidate():
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    return R006Candidate.from_point(ANCHOR_POINT)


def _attempt(contract, sequence: int, *, force_n: float = 5.25) -> AttemptResult:
    samples = _complete_samples(force_n, sequence_offset=sequence * 10_000)
    # Build a trainable-looking force objective via r006 receipt path for samples.
    _ = build_receipt_from_samples(
        samples,
        attempt_sequence=sequence,
        execution_id=f"r008-daemon-{sequence}",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    cand = _r006_candidate()
    return AttemptResult(
        epoch=1,
        attempt_sequence=sequence,
        kind="SPACEFILL",
        candidate=cand,
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=True,
        timing_gate=True,
        identity_gate=True,
        duration_s=60.0,
        execution_id=f"r008-daemon-{sequence}",
        raw_path_samples=samples,
        metrics={"execution_id": f"r008-daemon-{sequence}"},
    )


def test_async_seal_daemon_mode_skips_search_critical_gate() -> None:
    pipeline = AsyncSealPipeline(
        seal_fn=lambda r: r,
        advance_fn=lambda r: None,
        join_executing_on_search_critical=False,
        search_critical_gates_seals=False,
    )
    pipeline.begin_search_critical()  # must be a no-op
    assert pipeline._search_critical is False  # noqa: SLF001
    pipeline.close_and_join(timeout_s=2.0)


def test_seal_daemon_two_seals_hash_chain(tmp_path: Path) -> None:
    contract = load_contract()
    ledger_path = tmp_path / "observations.jsonl"
    ledger = ObservationLedger(
        ledger_path,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=EOAT,
    )
    r006 = R006ObservationLedger(ledger, campaign_fingerprint=contract.campaign_fingerprint)
    client = SealDaemonClient(
        ledger_path=ledger_path,
        r006_sidecar_path=r006.raw_sidecar_path,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=EOAT,
        cwd=ROOT,
        timeout_s=300.0,
    )
    try:
        p1 = client.seal_result(_attempt(contract, 1, force_n=5.1))
        p2 = client.seal_result(_attempt(contract, 2, force_n=5.2))
        assert int(p1["sealed_seq"]) == 1
        assert int(p2["sealed_seq"]) == 2
        cold = R006ObjectiveSidecar(
            r006.raw_sidecar_path, campaign_fingerprint=contract.campaign_fingerprint
        )
        rows = cold.fresh_process_verify()
        assert len(rows) == 2
        assert rows[0]["previous_sha256"] == GENESIS_SHA256
        assert rows[1]["previous_sha256"] == rows[0]["row_sha256"]
    finally:
        client.close()
