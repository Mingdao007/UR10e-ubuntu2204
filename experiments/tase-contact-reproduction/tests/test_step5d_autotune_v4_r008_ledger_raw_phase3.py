"""Seal Phase 3: ledger R008RAW1 + ForceObjective fast verify + lazy digests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r005.observations import ObservationRecord  # noqa: E402
from step5d_autotune_v4_r006.live_adapter import R006Candidate  # noqa: E402
from step5d_autotune_v4_r008.bounded_resume_ledger import (  # noqa: E402
    R008BoundedResumeObservationLedger,
)
from step5d_autotune_v4_r008.force_objective_columnar import (  # noqa: E402
    build_force_objective_from_sample_maps,
)
from step5d_autotune_v4_r008.raw_force_binary import SOURCE_KEYS  # noqa: E402
from step5d_force_objective import (  # noqa: E402
    ForceObjectiveBuilder,
    ForcePathSample,
)
from step5d_autotune_v4_r006.objective import R006ObjectiveBuilder  # noqa: E402


CAMPAIGN = "a" * 64
EOAT = "b" * 64


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


def _append_formal(
    ledger: R008BoundedResumeObservationLedger,
    sequence: int,
    samples: tuple[ForcePathSample, ...],
) -> ObservationRecord:
    record = ObservationRecord(
        campaign_fingerprint=CAMPAIGN,
        epoch=1,
        attempt_sequence=sequence,
        kind="STAIRCASE",
        candidate=R006Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=False,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=False,
        duration_s=60.0,
        raw_path_samples=samples,
        metrics={"execution_id": f"exec-{sequence}", "outcome": "SAFE_TRAINABLE"},
    )
    return ledger.append(record)


def test_ledger_write_slim_json_plus_r008raw(tmp_path: Path) -> None:
    ledger = R008BoundedResumeObservationLedger(
        tmp_path / "obs.jsonl",
        campaign_fingerprint=CAMPAIGN,
        eoat_sha256=EOAT,
    )
    samples = _complete_samples(5.1)
    sealed = _append_formal(ledger, 1, samples)
    assert sealed.raw_artifact is not None
    artifact = ledger._artifact_path(sealed.raw_artifact.relative_path)
    stub = json.loads(artifact.read_text(encoding="utf-8"))
    assert stub["samples"] == []
    raw = artifact.with_suffix(".r008raw")
    assert raw.is_file() and raw.stat().st_size > 0
    full_json_size = len(
        json.dumps({**stub, "samples": [s.as_dict() for s in samples]}, separators=(",", ":")).encode()
    )
    assert artifact.stat().st_size + raw.stat().st_size < full_json_size
    assert sealed.force_objective is not None
    assert sealed.force_objective.sample_count == len(samples)


def test_ledger_append_accepts_advisory_stock_fo_with_phase5_raw2(
    tmp_path: Path,
) -> None:
    """Regression: live host advisory FO must not block RAW2-bound ledger seal."""

    from step5d_autotune_v4_r008.raw_force_binary_v2 import seal_sha256_of

    samples = _complete_samples(5.25)
    stock = ForceObjectiveBuilder()
    stock.extend(samples)
    advisory = stock.finalize()

    ledger = R008BoundedResumeObservationLedger(
        tmp_path / "obs.jsonl",
        campaign_fingerprint=CAMPAIGN,
        eoat_sha256=EOAT,
    )
    record = ObservationRecord(
        campaign_fingerprint=CAMPAIGN,
        epoch=1,
        attempt_sequence=1,
        kind="ANCHOR",
        candidate=R006Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=False,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=False,
        duration_s=60.0,
        force_objective=advisory,
        raw_path_samples=samples,
        metrics={"execution_id": "exec-advisory-fo", "outcome": "SAFE_TRAINABLE"},
    )
    sealed = ledger.append(record)
    assert sealed.force_objective is not None
    assert sealed.force_objective.v2_mae_n == advisory.v2_mae_n
    assert sealed.force_objective.formal_bin_count == advisory.formal_bin_count
    artifact = ledger._artifact_path(sealed.raw_artifact.relative_path)
    assert sealed.force_objective.raw_evidence_digest == seal_sha256_of(
        artifact.with_suffix(".r008raw").read_bytes()
    )
    assert sealed.force_objective.raw_evidence_digest != advisory.raw_evidence_digest
    assert "caller force objective is not raw-artifact bound" not in ledger.path.read_text()


def test_ledger_fresh_matches_stock_full_json(tmp_path: Path) -> None:
    samples = _complete_samples(5.25)
    stock_builder = ForceObjectiveBuilder()
    stock_builder.extend(samples)
    stock = stock_builder.finalize()

    ledger = R008BoundedResumeObservationLedger(
        tmp_path / "obs.jsonl",
        campaign_fingerprint=CAMPAIGN,
        eoat_sha256=EOAT,
    )
    sealed = _append_formal(ledger, 1, samples)
    assert sealed.force_objective is not None
    # Phase 5: FO MAE/bins match stock; raw_evidence_digest binds R008RAW2 seal SHA.
    assert sealed.force_objective.v2_mae_n == stock.v2_mae_n
    assert sealed.force_objective.formal_bin_sum_n == stock.formal_bin_sum_n
    assert sealed.force_objective.formal_bin_count == stock.formal_bin_count
    assert sealed.force_objective.sample_count == stock.sample_count
    from step5d_autotune_v4_r008.raw_force_binary_v2 import seal_sha256_of

    artifact = ledger._artifact_path(sealed.raw_artifact.relative_path)
    assert sealed.force_objective.raw_evidence_digest == seal_sha256_of(
        artifact.with_suffix(".r008raw").read_bytes()
    )
    assert sealed.force_objective.raw_evidence_digest != stock.raw_evidence_digest

    # Cold reopen: bounded resume re-verifies the tail via hydrate path.
    reopened = R008BoundedResumeObservationLedger(
        tmp_path / "obs.jsonl",
        campaign_fingerprint=CAMPAIGN,
        eoat_sha256=EOAT,
        tail_rows=1,
    )
    assert reopened.records[-1].force_objective is not None
    assert (
        reopened.records[-1].force_objective.as_dict()
        == sealed.force_objective.as_dict()
    )


def test_legacy_full_json_still_verifies(tmp_path: Path) -> None:
    """kunwei-only fixtures are not R008RAW1-compatible → full JSON path."""

    dt = 0.1
    samples = []
    t = 5.0
    for i in range(550):
        samples.append(
            ForcePathSample(
                path_time_s=t,
                filtered_normal_n=5.0,
                source_sequences={"kunwei": i + 1},
                source_ages_s={"kunwei": 0.001},
                stage=25,
            )
        )
        t += dt
    ledger = R008BoundedResumeObservationLedger(
        tmp_path / "obs.jsonl",
        campaign_fingerprint=CAMPAIGN,
        eoat_sha256=EOAT,
    )
    sealed = _append_formal(ledger, 1, tuple(samples))
    artifact = ledger._artifact_path(sealed.raw_artifact.relative_path)
    stub = json.loads(artifact.read_text(encoding="utf-8"))
    assert len(stub["samples"]) == 550
    assert not artifact.with_suffix(".r008raw").exists()
    assert sealed.force_objective is not None
    assert sealed.force_objective.sample_count == 550


def test_columnar_force_objective_matches_stock() -> None:
    samples = _complete_samples(5.0)
    stock = ForceObjectiveBuilder()
    stock.extend(samples)
    assert (
        build_force_objective_from_sample_maps([s.as_dict() for s in samples]).as_dict()
        == stock.finalize().as_dict()
    )


def test_lazy_add_digests_replay_and_finalize_parity() -> None:
    base = list(_complete_samples(5.1)[:10])
    # Replay must not regress PATH time: duplicate the last sample.
    replay = base[-1]
    conflict = ForcePathSample(
        path_time_s=replay.path_time_s,
        path_phase=25,
        filtered_normal_n=9.9,
        source_sequences=dict(replay.source_sequences),
        source_ages_s=dict(replay.source_ages_s),
        commanded_qdot=replay.commanded_qdot,
        actual_qd=replay.actual_qd,
        timestamp_s=replay.timestamp_s,
    )

    stock = ForceObjectiveBuilder()
    lazy = ForceObjectiveBuilder()
    for sample in base:
        assert stock.add(sample) is True
        assert lazy.add(sample) is True
    assert stock.add(replay) is False
    assert lazy.add(replay) is False
    assert stock._replays == lazy._replays == 1
    with pytest.raises(Exception):
        lazy.add(conflict)
    assert stock.finalize().as_dict() == lazy.finalize().as_dict()

    # R006: collision fixture preserves replay count; finalize digests match via seal.
    r006_a = R006ObjectiveBuilder(
        attempt_sequence=1,
        execution_id="lazy-a",
        campaign_fingerprint=CAMPAIGN,
    )
    r006_b = R006ObjectiveBuilder(
        attempt_sequence=1,
        execution_id="lazy-a",
        campaign_fingerprint=CAMPAIGN,
    )
    for sample in base:
        r006_a.add(sample)
        r006_b.add(sample)
    assert r006_a.add(replay) is False
    assert r006_b.add(replay) is False
    ra = r006_a.finalize()
    rb = r006_b.finalize()
    assert ra.deduplicated_replays == rb.deduplicated_replays == 1
    assert ra.builder_seal_sha256 == rb.builder_seal_sha256
    assert ra.raw_bundle_digest == rb.raw_bundle_digest
    # Trailing validate_seal skipped on finalize; from_mapping still validates.
    ra.validate_seal()
