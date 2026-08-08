from __future__ import annotations

from dataclasses import fields, replace
import math

import pytest

from step5d_force_objective import (
    ForceObjective,
    ForceObjectiveBuilder,
    ForceObjectiveError,
    ForcePathSample,
)
from step5d_autotune_v4_r005.offline_test_factory import synthetic_force_objective


def _sample(index: int, *, force: float = 5.0, path_time_s: float | None = None) -> ForcePathSample:
    time_s = index / 10.0 if path_time_s is None else path_time_s
    return ForcePathSample(
        path_time_s=time_s,
        path_phase=25,
        filtered_normal_n=force,
        source_sequences={"packet": index + 1, "rtde": index + 1},
        source_ages_s={"packet": 0.001, "rtde": 0.001},
    )


def test_formal_v2_window_and_legacy_shadow_use_bin_means() -> None:
    builder = ForceObjectiveBuilder()
    # The same immutable evidence intentionally differs between the windows:
    # [0, 5) is 10 N, while [5, 60) is exactly 5 N.
    for index in range(600):
        builder.add(_sample(index, force=10.0 if index < 50 else 5.0))
    builder.add(_sample(600, force=100.0, path_time_s=60.0))
    objective = builder.finalize()

    assert objective.complete_bins == 550
    assert objective.legacy_complete_bins == 550
    assert objective.formal_bin_ids[0] == 0  # t=5.0 is included
    assert objective.formal_bin_ids[-1] == 549  # [55, 60) is included
    assert objective.v2_mae_n == pytest.approx(0.0)
    assert objective.legacy_mae_n == pytest.approx(50.0 / 110.0)
    assert objective.delta_n == pytest.approx(objective.legacy_mae_n)
    assert objective.sample_count == 601  # t=60.0 is bound but excluded
    assert objective.as_dict()["formal_window_s"] == [5.0, 60.0]
    assert objective.as_dict()["r004_legacy_shadow"] == objective.legacy_mae_n


def test_missing_formal_bin_has_no_trainable_objective() -> None:
    builder = ForceObjectiveBuilder()
    for index in range(550):
        if index != 123:
            builder.add(_sample(50 + index, path_time_s=5.0 + index / 10.0))
    objective = builder.finalize()
    assert objective.complete_bins == 549
    assert objective.v2_mae_n is None
    assert objective.trainable is False
    assert objective.coverage_bitmap[123] == "0"


def test_typed_objective_rejects_fabricated_coverage_and_values() -> None:
    builder = ForceObjectiveBuilder()
    for index in range(600):
        builder.add(_sample(index))
    objective = builder.finalize()

    with pytest.raises(ForceObjectiveError, match="bitmap differs"):
        replace(objective, coverage_bitmap="0" * 550)
    with pytest.raises(ForceObjectiveError, match="count differs"):
        replace(objective, complete_bins=549)
    with pytest.raises(ForceObjectiveError, match="bin identities"):
        replace(objective, formal_bin_ids=objective.formal_bin_ids[:-1])
    with pytest.raises(ForceObjectiveError, match="delta differs"):
        replace(objective, delta_n=(objective.delta_n or 0.0) + 1.0)


def test_identity_replay_deduplicates_exact_payload_and_rejects_conflict() -> None:
    sample = _sample(0, path_time_s=5.0)
    builder = ForceObjectiveBuilder()
    assert builder.add(sample) is True
    assert builder.add(sample) is False
    assert builder.finalize().deduplicated_replays == 1

    conflicting = ForcePathSample(
        path_time_s=5.0,
        path_phase=25,
        filtered_normal_n=5.5,
        source_sequences=sample.source_sequences,
        source_ages_s=sample.source_ages_s,
    )
    with pytest.raises(ForceObjectiveError, match="conflicting payload"):
        builder.add(conflicting)


def test_raw_path_rejects_wrong_stage_nonfinite_time_regression_and_stale_data() -> None:
    with pytest.raises(ForceObjectiveError, match="stage 25"):
        _sample(0).__class__(
            path_time_s=5.0,
            path_phase=24,
            filtered_normal_n=5.0,
            source_sequences={"packet": 1},
            source_ages_s={"packet": 0.001},
        )
    with pytest.raises(ForceObjectiveError, match="finite"):
        _sample(0, force=math.nan)
    with pytest.raises(ForceObjectiveError, match="stale"):
        ForcePathSample(
            path_time_s=5.0,
            path_phase=25,
            filtered_normal_n=5.0,
            source_sequences={"packet": 1},
            source_ages_s={"packet": 0.081},
        )
    builder = ForceObjectiveBuilder()
    builder.add(_sample(1, path_time_s=5.1))
    with pytest.raises(ForceObjectiveError, match="regressed"):
        builder.add(_sample(0, path_time_s=5.0))


def test_from_scratch_scalar_and_fake_identity_forge_is_rejected() -> None:
    forged = {
        "schema": "step5d.force-objective/v2",
        "version": "force_mae_v2",
        "target_force_n": 5.0,
        "formal_window_s": [5.0, 60.0],
        "legacy_window_s": [0.0, 55.0],
        "bin_width_s": 0.1,
        "required_bins": 550,
        "complete_bins": 550,
        "legacy_complete_bins": 550,
        "coverage_bitmap": "1" * 550,
        "formal_bin_ids": list(range(550)),
        "legacy_bin_ids": list(range(550)),
        "sample_identity_ids": ["a" * 64],
        "sample_identity_digest": "b" * 64,
        "sample_count": 1,
        "deduplicated_replays": 0,
        "v2_mae_n": 0.123456789,
        "legacy_mae_n": 0.234,
        "delta_n": 0.110543211,
    }
    with pytest.raises(ForceObjectiveError, match="receipt"):
        # A scalar, bitmap and one fake identity are not a sealed raw receipt.
        ForceObjective.from_mapping(forged)


def test_complete_from_scratch_receipt_without_raw_artifact_is_unverified() -> None:
    # This deliberately has every current sufficient-statistics field,
    # digest, and builder seal.  It is still caller-controlled because no
    # durable raw artifact has crossed the ObservationLedger seam.
    builder = ForceObjectiveBuilder()
    for index in range(600):
        builder.add(_sample(index, force=5.123456789))
    mapping = builder.finalize(provenance="raw_path_evidence").as_dict()
    assert {
        "formal_bin_sum_n",
        "formal_bin_count",
        "legacy_bin_sum_n",
        "legacy_bin_count",
        "raw_evidence_digest",
        "sufficient_statistics_digest",
        "builder_seal_sha256",
    } <= set(mapping)
    restored = ForceObjective.from_mapping(mapping)
    assert restored.trainable is False
    assert restored.objective is None
    mapping["verification_state"] = "verified_raw_artifact"
    with pytest.raises(ForceObjectiveError, match="verified objective state"):
        ForceObjective.from_mapping(mapping)


def test_force_objective_constructor_and_replace_have_no_verified_capability() -> None:
    builder = ForceObjectiveBuilder()
    for index in range(600):
        builder.add(_sample(index, force=5.25))
    objective = builder.finalize()
    constructor_payload = {
        item.name: getattr(objective, item.name)
        for item in fields(ForceObjective)
    }
    constructor_payload["verification_state"] = "verified_raw_artifact"
    with pytest.raises(TypeError):
        ForceObjective(**constructor_payload)
    with pytest.raises(TypeError):
        replace(objective, verification_state="verified_raw_artifact")
    assert objective.trainable is False
    assert objective.objective is None
    assert "verification_state" not in objective.as_dict()


def test_sealed_statistics_round_trip_and_tamper_revalidation() -> None:
    builder = ForceObjectiveBuilder()
    for index in range(600):
        builder.add(_sample(index, force=5.25))
    objective = builder.finalize()
    restored = ForceObjective.from_mapping(objective.as_dict())
    assert restored.trainable is False
    assert restored.v2_mae_n == pytest.approx(0.25)
    assert "sample_identity_ids" not in restored.as_dict()

    with pytest.raises(ForceObjectiveError):
        replace(objective, v2_mae_n=0.123456789).validate_sealed()
    with pytest.raises(ForceObjectiveError, match="sufficient-statistics"):
        replace(objective, sufficient_statistics_digest="c" * 64).validate_sealed()


def test_offline_synthetic_fixture_cannot_become_trainable() -> None:
    objective = synthetic_force_objective(0.25)
    assert objective.provenance == "offline_synthetic_test"
    assert objective.trainable is False
    assert objective.objective is None
