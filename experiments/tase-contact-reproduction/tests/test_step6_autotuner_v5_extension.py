from __future__ import annotations

import json
from pathlib import Path

import pytest

from step6_figure8_autotune_v1.core import CompleteCandidateV1
from step6_figure8_autotune_v1.v5_extension import (
    ACTIVE_SET_MAX,
    EXTENSION_EXACT_TARGET,
    V5ExtensionEpochReceiptV1,
    V5ExtensionError,
    build_active_set,
    derive_extension_fingerprint,
    verify_active_set,
    verify_epoch_receipt,
)
from step6_figure8_autotune_v1.v5_raw_archive import (
    V5RawArchiveError,
    compress_r013life,
    verify_raw_archive_receipt,
)
from step6_figure8_autotune_v1.v5_extension_runner import (
    V5ExtensionEpochInputsV1,
    V5ExtensionEpochV1,
)
from step6_figure8_autotune_v1.v5_campaign import V5CampaignV2


def _candidate(index: int) -> CompleteCandidateV1:
    return CompleteCandidateV1(
        controller_path={
            "force_p_gain": 0.01 + index * 0.0001,
            "force_damping": 100.0 + index,
            "force_i_gain": 0.0001 + index * 0.000001,
            "i_off": False,
            "normal_filter_tau_s": 0.02 + index * 0.0001,
            "orientation_ko": 0.05 + index * 0.0001,
            "motion_kp": 2.0 + index * 0.001,
        },
        correction_weights=(0.0,) * 6,
    )


def _rows(count: int = 640) -> list[dict[str, object]]:
    return [
        {
            "candidate": _candidate(index).as_dict(),
            "candidate_key": _candidate(index).candidate_key,
            "observation_id": f"obs-{index:04d}",
            "ordinal": index + 1,
            "mean_n": 0.1 + (index % 29) * 0.001 + index * 1e-7,
        }
        for index in range(count)
    ]


def test_active_set_is_deterministic_bounded_and_cold_verifiable() -> None:
    rows = _rows()
    first = build_active_set(rows, parent_fingerprint_sha256="a" * 64, parent_ledger_head_sha256="b" * 64)
    second = build_active_set(tuple(reversed(rows)), parent_fingerprint_sha256="a" * 64, parent_ledger_head_sha256="b" * 64)
    assert first == second
    assert first["selected_observation_count"] == ACTIVE_SET_MAX
    assert len(first["selected_observation_ids"]) == ACTIVE_SET_MAX
    assert verify_active_set(first)["active_set_sha256"] == first["active_set_sha256"]
    assert {source for row in first["observations"] for source in row["selection_sources"]} >= {
        "historical_champion", "lowest_mae", "recent", "maximin_coverage"
    }


def test_active_set_rejects_tampering_and_empty_source() -> None:
    with pytest.raises(V5ExtensionError):
        build_active_set([], parent_fingerprint_sha256="a" * 64, parent_ledger_head_sha256="b" * 64)
    receipt = build_active_set(_rows(8), parent_fingerprint_sha256="a" * 64, parent_ledger_head_sha256="b" * 64)
    tampered = dict(receipt)
    tampered["selected_observation_count"] = 7
    with pytest.raises(V5ExtensionError):
        verify_active_set(tampered)


def test_extension_fingerprint_and_epoch_receipt_bind_parent() -> None:
    fingerprint = derive_extension_fingerprint(
        release_identity_sha256="c" * 64,
        parent_campaign_fingerprint_sha256="a" * 64,
        parent_ledger_head_sha256="b" * 64,
        epoch=1,
    )
    active = build_active_set(_rows(12), parent_fingerprint_sha256="a" * 64, parent_ledger_head_sha256="b" * 64)
    receipt = V5ExtensionEpochReceiptV1(
        epoch=1,
        parent_epoch=0,
        parent_campaign_fingerprint_sha256="a" * 64,
        parent_ledger_head_sha256="b" * 64,
        campaign_fingerprint_sha256=fingerprint,
        physical_ledger_head_sha256="d" * 64,
        active_set_sha256=active["active_set_sha256"],
        exact_novel_count=EXTENSION_EXACT_TARGET,
        top3_total_n=5,
    ).as_dict()
    assert verify_epoch_receipt(receipt).epoch == 1
    bad = dict(receipt)
    bad["parent_epoch"] = 1
    with pytest.raises(V5ExtensionError):
        verify_epoch_receipt(bad)


def test_raw_archive_round_trip_cold_verifies_without_deleting_source(tmp_path: Path) -> None:
    raw = tmp_path / "trial.r013life"
    raw.write_bytes(b"R013LIFE\x01\x00" + bytes(range(256)) * 100)
    receipt = compress_r013life(raw)
    assert receipt["decompression_cold_verified"] is True
    assert receipt["retain_uncompressed"] is True
    assert raw.is_file()
    assert Path(receipt["compressed_path"]).is_file()
    assert verify_raw_archive_receipt(receipt)["raw_sha256"] == receipt["raw_sha256"]
    with pytest.raises(V5RawArchiveError):
        compress_r013life(raw)


def test_raw_archive_rejects_changed_source(tmp_path: Path) -> None:
    raw = tmp_path / "trial.r013life"
    raw.write_bytes(b"raw")
    receipt = compress_r013life(raw)
    raw.write_bytes(b"changed")
    with pytest.raises(V5RawArchiveError):
        verify_raw_archive_receipt(receipt)


def test_extension_runner_materializes_fresh_home_only_seed_and_closeout(tmp_path: Path) -> None:
    runner = V5ExtensionEpochV1(
        V5ExtensionEpochInputsV1(
            root=tmp_path,
            state_root=tmp_path / "epoch-001",
            release_identity_sha256="c" * 64,
            parent_campaign_fingerprint_sha256="a" * 64,
            parent_ledger_head_sha256="b" * 64,
            parent_epoch=0,
            observations=_rows(12),
            epoch=1,
        )
    )
    identity = runner.campaign_identity()
    assert identity.entry_mode == "HOME_ONLY_V1"
    assert identity.campaign_fingerprint == runner.fingerprint
    closeout = runner.closeout(
        physical_ledger_head_sha256="d" * 64,
        exact_novel_count=200,
        top3_total_n=5,
    )
    assert runner.verify()["closeout"]["receipt_sha256"] == closeout["receipt_sha256"]
    campaign = V5CampaignV2.from_config(
        Path("config/step6/autotuner_v5_home_only_primary_v1.json"),
        runner.campaign_identity(),
    )
    assert len(campaign._seed_observations) == 12
    assert campaign.exact_novel_count == 0
