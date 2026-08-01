"""Focused proof for the deterministic Lane 3 offline fixture campaign."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil

import numpy as np
import pytest

import ur10e_vic.tacdiffusion.offline_campaign as offline_campaign
from ur10e_vic.tacdiffusion.episode_composition import CausalKunweiAlignmentAdapter
from ur10e_vic.tacdiffusion.episode_recorder import (
    read_episode_artifact,
    validate_sealed_episode_manifest,
)
from ur10e_vic.tacdiffusion.offline_campaign import (
    CAMPAIGN_EPISODE_COUNT,
    CAMPAIGN_FAIL_MODULO,
    CAMPAIGN_RECOVERY_CYCLES,
    OFFLINE_DATASET_SCHEMA,
    SOURCE_IDENTITIES,
    SOURCE_RECEIPT_PATHS,
    DeterministicOfflineEpisodeAssembler,
    DeterministicFakeRTDE,
    FakeController,
    FakeControllerCalibration,
    FakeControllerCommand,
    FixtureEpisodeArtifact,
    FrozenEpisodeSplit,
    OfflineCampaignContract,
    RecoveryReceipt,
    SyntheticKunweiDriver,
    _episode_context,
    _payload_sha256,
    _write_deterministic_npz,
    build_offline_fixture_dataset,
    materialize_offline_campaign_bundle,
    prove_recorder_sidecar_identity,
    validate_offline_campaign_bundle,
    _file_sha256,
    run_persistent_recovery_cycles,
    validate_offline_fixture_dataset,
)
from ur10e_vic.tacdiffusion.trajectory import TRAJECTORY_FAMILIES


@pytest.fixture(scope="module")
def campaign_bundle(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("offline-campaign") / "fixture-bundle"
    return materialize_offline_campaign_bundle(root)


def _resign_dataset_manifest(manifest: dict, dataset_path: Path) -> dict:
    manifest = dict(manifest)
    manifest["dataset_artifact"] = dataset_path.name
    manifest["dataset_sha256"] = _file_sha256(dataset_path)
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = _payload_sha256(unsigned)
    return manifest


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_exact_campaign_counters_families_and_failed_evidence(campaign_bundle) -> None:
    receipt = campaign_bundle.campaign_receipt
    assert receipt["contract"]["attempted_episodes"] == CAMPAIGN_EPISODE_COUNT
    assert receipt["family_order"] == list(TRAJECTORY_FAMILIES)
    assert receipt["fail_modulo"] == CAMPAIGN_FAIL_MODULO
    assert receipt["injected_failure_episode_indices"] == list(range(0, 50, 7))
    assert receipt["counters"] == {
        "attempted": 50,
        "successful": 42,
        "failed": 8,
        "eligible": 42,
        "expert_resets": 50,
        "observation_filter_resets": 50,
        "retract_acks": 50,
        "home_acks": 50,
        "consume_acks": 50,
        "queue_drained": True,
    }
    assert receipt["family_counts"] == {
        "circle": 8,
        "ellipse": 7,
        "figure_eight": 7,
        "lissajous": 7,
        "linear_grid": 7,
        "rounded_arc": 7,
        "seeded_smooth_spline": 7,
    }
    failed = [entry for entry in receipt["episodes"] if entry["status"] == "failed"]
    assert len(failed) == 8
    assert all(entry["failure_reason"] == "injected_fail_modulo_7" for entry in failed)
    assert all(entry["dataset_member"] is False for entry in failed)
    for entry in failed:
        failure_path = campaign_bundle.root / entry["failure"]
        payload = json.loads(failure_path.read_text(encoding="utf-8"))
        assert payload["sealed_evidence_retained"] is True
        assert payload["dataset_membership"] is False


def test_lifecycle_recovery_receipt_is_exactly_100_cycles(tmp_path: Path) -> None:
    receipt = run_persistent_recovery_cycles(
        tmp_path / "persistent-recovery.json", cycles=CAMPAIGN_RECOVERY_CYCLES
    )
    assert receipt.cycles == 100
    assert receipt.interrupted_cycles == 50
    assert receipt.retried_cycles == 50
    assert receipt.reopen_count == 200
    assert receipt.duplicate_enqueue_attempts == 100
    assert receipt.idempotent_consume_attempts == 100
    assert receipt.duplicate_eligibility_promotions == 0
    assert receipt.final_pending_count == 0
    assert receipt.queue_drained is True


def test_disabled_recorder_sidecar_is_algebraic_identity() -> None:
    proof = prove_recorder_sidecar_identity(seed=42)
    assert proof["identical"] is True
    assert proof["disabled_recorder_is_identity"] is True
    assert proof["recorder_enabled_digest"] == proof["recorder_disabled_digest"]
    assert proof["real_recorder_exercised"] is True


def test_recorder_identity_proof_rejects_nonfunctional_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenRecorder:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("recorder intentionally broken")

    monkeypatch.setattr(offline_campaign, "EpisodeRecorder", BrokenRecorder)
    with pytest.raises(RuntimeError, match="recorder intentionally broken"):
        prove_recorder_sidecar_identity(seed=42)


def test_fake_rtde_controller_feedback_is_typed_and_causal() -> None:
    calibration = FakeControllerCalibration(
        jacobian_6x6=tuple(
            tuple(2.0 if row == column else 0.0 for column in range(6))
            for row in range(6)
        )
    )
    controller = FakeController(calibration=calibration)
    rtde = DeterministicFakeRTDE(seed=42, controller=controller)
    rtde.tick(0, 0.002)
    action = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0)
    command = controller.command(
        sequence=0,
        timestamp_s=0.002,
        action_12d=action,
        echoed_action_12d=rtde.echo_action(0, action),
    )
    tick = rtde.tick(1, 0.004)
    assert command.expert_wrench_6d == action[:6]
    assert command.stiffness_6d == action[6:]
    assert command.commanded_no_gravity_torque_6d == (2.0, 4.0, 6.0, 8.0, 10.0, 12.0)
    assert tick.dynamics_sample.previous_commanded_no_gravity_torque_nm == command.commanded_no_gravity_torque_6d
    assert tick.dynamics_sample.previous_commanded_no_gravity_torque_nm != action[:6]
    assert command.command_sha256


def test_assembler_binds_controller_command_to_next_dynamics_tick() -> None:
    composed = DeterministicOfflineEpisodeAssembler(seed=42).compose_episode(
        episode_id="controller-causal",
        family="circle",
        episode_seed=42,
        semantic_context=_episode_context(episode_id="controller-causal", split="train", failed=False),
        reset_state=True,
    )
    first = composed.frames[0]
    second = composed.frames[1]
    first_command = FakeControllerCommand.from_json(
        first.reference_receipt.payload["controller_command_receipt"]
    )
    second_command = FakeControllerCommand.from_json(
        second.reference_receipt.payload["controller_command_receipt"]
    )
    assert second.dynamics_sample.previous_commanded_no_gravity_torque_nm == first_command.commanded_no_gravity_torque_6d
    assert second_command.expert_wrench_6d == tuple(second.expert_action_12d[:6])
    assert second_command.stiffness_6d == tuple(second.expert_action_12d[6:])


def test_recovery_receipt_rejects_rehashed_all_zero_counters() -> None:
    with pytest.raises(ValueError, match="exact executed 100-cycle result"):
        RecoveryReceipt(
            cycles=100,
            interrupted_cycles=0,
            retried_cycles=0,
            reopen_count=0,
            duplicate_enqueue_attempts=0,
            idempotent_consume_attempts=0,
            duplicate_eligibility_promotions=0,
            final_pending_count=0,
            queue_drained=True,
        )


def test_causal_previous_slice_hold_and_fault_latch() -> None:
    context = _episode_context(episode_id="causal", split="train", failed=False)
    composed = DeterministicOfflineEpisodeAssembler(seed=42).compose_episode(
        episode_id="causal",
        family="circle",
        episode_seed=42,
        semantic_context=context,
        reset_state=True,
    )
    first = composed.frames[0]
    second = composed.frames[1]
    first_observation_receipt = first.observation_receipt.as_json()
    second_observation_receipt = second.observation_receipt.as_json()
    first_observation_payload = first_observation_receipt["payload"]
    second_observation_payload = second_observation_receipt["payload"]
    assert first_observation_payload["previous_sequence"] == 0
    assert first_observation_payload["current_sequence"] == 1
    assert second_observation_payload["previous_sequence"] == 1
    assert second_observation_payload["current_sequence"] == 2
    assert list(first.observation_84d[:42]) == first_observation_payload["current_observation_42d"]
    assert list(first.observation_84d[42:]) == first_observation_payload["previous_observation_42d"]

    driver = SyntheticKunweiDriver(seed=42, hold_every=2)
    driver.read(sequence=0, control_timestamp_s=0.002)
    driver.read(sequence=1, control_timestamp_s=0.004)
    held = driver.read(sequence=2, control_timestamp_s=0.006)
    assert held.external_hold is True
    assert held.external_held_ticks == 1
    assert held.source_sample_index == 1

    adapter = CausalKunweiAlignmentAdapter(
        expected_frame_id="tool0_tcp", calibration_sha256="a" * 64
    )
    assert adapter.align(
        control_timestamp_s=0.002,
        device_time_s=0.001,
        host_visible_time_s=0.001,
        batch_id=0,
        sample_index=0,
        wrench_tcp_si=(0.0,) * 6,
    ) is not None
    assert adapter.align(
        control_timestamp_s=0.004,
        device_time_s=0.001,
        host_visible_time_s=0.001,
        batch_id=0,
        sample_index=0,
        wrench_tcp_si=(1.0,) * 6,
    ) is None
    assert adapter.fault is not None


def test_v3_row_tail_content_seals_and_bundle_validation(campaign_bundle, tmp_path: Path) -> None:
    validate_offline_campaign_bundle(campaign_bundle.root)
    eligible = next(item for item in campaign_bundle.episodes if item.status == "completed")
    manifest = validate_sealed_episode_manifest(
        eligible.artifact_path, eligible.manifest_path
    )
    header, rows = read_episode_artifact(eligible.artifact_path)
    assert header["schema"] == "ur10e_tacdiffusion_episode_artifact/v3"
    assert manifest["complete_seal"] is True
    assert all(row["row_sha256"] for row in rows)
    assert all("row_seal_sha256" not in row for row in rows)
    assert all(len(row["observation_84d"]) == 84 for row in rows)
    assert all(len(row["expert_action_12d"]) == 12 for row in rows)
    assert all(row["dynamics_receipt"]["valid"] is True for row in rows)

    tampered = tmp_path / "tampered-bundle"
    shutil.copytree(campaign_bundle.root, tampered)
    artifact = next(tampered.rglob("episode_v3.jsonl"))
    artifact.write_bytes(artifact.read_bytes().replace(b"offline_fake_rtde", b"tampered_fake_rtde", 1))
    with pytest.raises(ValueError, match="missing, extra, or tampered"):
        validate_offline_campaign_bundle(tampered)


def test_negative_promotion_legacy_dimension_and_failure_rejection(campaign_bundle, tmp_path: Path) -> None:
    manifest_path = campaign_bundle.root / "dataset.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["production_promotion_allowed"] = True
    unsigned = dict(payload)
    unsigned.pop("manifest_sha256")
    payload["manifest_sha256"] = _payload_sha256(unsigned)
    promoted_manifest = tmp_path / "promoted.manifest.json"
    promoted_manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be promoted"):
        validate_offline_fixture_dataset(
            campaign_bundle.root / "dataset.npz", promoted_manifest
        )

    failed = next(item for item in campaign_bundle.episodes if item.status == "failed")
    with pytest.raises(ValueError, match="failed episodes"):
        build_offline_fixture_dataset(
            tmp_path / "failed.npz",
            episodes=[failed],
            frozen_split=FrozenEpisodeSplit.freeze([failed.episode_id]),
        )

    bad_dataset = tmp_path / "bad-dimension.npz"
    with np.load(campaign_bundle.root / "dataset.npz", allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    arrays["observations"] = np.zeros((arrays["observations"].shape[0], 36), dtype=np.float32)
    _write_deterministic_npz(bad_dataset, arrays)
    bad_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad_manifest["dataset_artifact"] = bad_dataset.name
    bad_manifest["dataset_sha256"] = hashlib.sha256(bad_dataset.read_bytes()).hexdigest()
    bad_manifest["observation_shape"] = [84, 36]
    unsigned_bad = dict(bad_manifest)
    unsigned_bad.pop("manifest_sha256")
    bad_manifest["manifest_sha256"] = _payload_sha256(unsigned_bad)
    bad_manifest_path = tmp_path / "bad-dimension.manifest.json"
    bad_manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=r"schema/dimension mismatch|not \[N,84\]"):
        validate_offline_fixture_dataset(bad_dataset, bad_manifest_path)

    success = next(item for item in campaign_bundle.episodes if item.status == "completed")
    with pytest.raises(ValueError, match="duplicate episode membership"):
        build_offline_fixture_dataset(
            tmp_path / "duplicate.npz",
            episodes=[success, success],
            frozen_split=FrozenEpisodeSplit.freeze([success.episode_id]),
        )

    bad_split_dataset = tmp_path / "bad-split.npz"
    with np.load(campaign_bundle.root / "dataset.npz", allow_pickle=False) as archive:
        split_arrays = {name: archive[name] for name in archive.files}
    original_split = str(split_arrays["splits"][0])
    split_arrays["splits"] = split_arrays["splits"].copy()
    split_arrays["splits"][0] = "validation" if original_split != "validation" else "train"
    _write_deterministic_npz(bad_split_dataset, split_arrays)
    bad_split_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad_split_manifest["dataset_artifact"] = bad_split_dataset.name
    bad_split_manifest["dataset_sha256"] = hashlib.sha256(bad_split_dataset.read_bytes()).hexdigest()
    unsigned_split = dict(bad_split_manifest)
    unsigned_split.pop("manifest_sha256")
    bad_split_manifest["manifest_sha256"] = _payload_sha256(unsigned_split)
    bad_split_manifest_path = tmp_path / "bad-split.manifest.json"
    bad_split_manifest_path.write_text(json.dumps(bad_split_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="episode row leakage|frozen split mismatch"):
        validate_offline_fixture_dataset(bad_split_dataset, bad_split_manifest_path)

    legacy = tmp_path / "legacy-episode"
    shutil.copytree(success.directory, legacy)
    legacy_artifact = legacy / "episode_v3.jsonl"
    legacy_lines = legacy_artifact.read_text(encoding="utf-8").splitlines()
    legacy_header = json.loads(legacy_lines[0])
    legacy_header["schema"] = "ur10e_tacdiffusion_episode_artifact/v2"
    legacy_lines[0] = json.dumps(legacy_header, sort_keys=True, separators=(",", ":"))
    legacy_artifact.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")
    legacy_manifest_path = legacy / "episode_v3.manifest.json"
    legacy_manifest = json.loads(legacy_manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["artifact_sha256"] = hashlib.sha256(legacy_artifact.read_bytes()).hexdigest()
    legacy_manifest["row_count"] = 3
    legacy_manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    legacy_receipt_path = legacy / "episode.receipt.json"
    legacy_receipt = json.loads(legacy_receipt_path.read_text(encoding="utf-8"))
    legacy_receipt["artifact_sha256"] = legacy_manifest["artifact_sha256"]
    legacy_receipt["manifest_sha256"] = hashlib.sha256(legacy_manifest_path.read_bytes()).hexdigest()
    unsigned_receipt = dict(legacy_receipt)
    unsigned_receipt.pop("receipt_sha256")
    legacy_receipt["receipt_sha256"] = _payload_sha256(unsigned_receipt)
    legacy_receipt_path.write_text(json.dumps(legacy_receipt), encoding="utf-8")
    legacy_episode = FixtureEpisodeArtifact.from_directory(legacy)
    with pytest.raises(ValueError, match="legacy v2"):
        build_offline_fixture_dataset(
            tmp_path / "legacy.npz",
            episodes=[legacy_episode],
            frozen_split=FrozenEpisodeSplit.freeze([legacy_episode.episode_id]),
        )


def test_dataset_validator_rejects_fabricated_rehashed_arrays(campaign_bundle, tmp_path: Path) -> None:
    source_dataset = campaign_bundle.root / "dataset.npz"
    with np.load(source_dataset, allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    arrays["observations"] = np.zeros_like(arrays["observations"])
    arrays["row_sha256"] = np.asarray(["a" * 64] * arrays["observations"].shape[0], dtype=np.str_)
    arrays["episode_artifact_sha256"] = np.asarray(["b" * 64] * arrays["observations"].shape[0], dtype=np.str_)
    fabricated_dataset = tmp_path / "fabricated.npz"
    _write_deterministic_npz(fabricated_dataset, arrays)
    fabricated_manifest = _resign_dataset_manifest(
        json.loads((campaign_bundle.root / "dataset.manifest.json").read_text(encoding="utf-8")),
        fabricated_dataset,
    )
    fabricated_manifest_path = tmp_path / "fabricated.manifest.json"
    _write_json(fabricated_manifest_path, fabricated_manifest)
    with pytest.raises(ValueError, match="cross-bound"):
        validate_offline_fixture_dataset(
            fabricated_dataset,
            fabricated_manifest_path,
            episodes=campaign_bundle.episodes,
        )


def test_dataset_validator_rejects_uppercase_canonical_row_sha(campaign_bundle, tmp_path: Path) -> None:
    with np.load(campaign_bundle.root / "dataset.npz", allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    arrays["row_sha256"] = arrays["row_sha256"].copy()
    arrays["row_sha256"][0] = str(arrays["row_sha256"][0]).upper()
    uppercase_dataset = tmp_path / "uppercase.npz"
    _write_deterministic_npz(uppercase_dataset, arrays)
    uppercase_manifest = _resign_dataset_manifest(
        json.loads((campaign_bundle.root / "dataset.manifest.json").read_text(encoding="utf-8")),
        uppercase_dataset,
    )
    uppercase_manifest_path = tmp_path / "uppercase.manifest.json"
    _write_json(uppercase_manifest_path, uppercase_manifest)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        validate_offline_fixture_dataset(uppercase_dataset, uppercase_manifest_path)


def test_bundle_validator_rejects_rehashed_all_zero_recovery(campaign_bundle, tmp_path: Path) -> None:
    tampered = tmp_path / "zero-recovery-bundle"
    shutil.copytree(campaign_bundle.root, tampered)
    recovery_path = tampered / "recovery.receipt.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    for field in (
        "interrupted_cycles",
        "retried_cycles",
        "reopen_count",
        "duplicate_enqueue_attempts",
        "idempotent_consume_attempts",
    ):
        recovery[field] = 0
    unsigned_recovery = dict(recovery)
    unsigned_recovery.pop("receipt_sha256", None)
    recovery["receipt_sha256"] = _payload_sha256(unsigned_recovery)
    _write_json(recovery_path, recovery)

    campaign_path = tampered / "campaign.receipt.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    campaign["recovery_receipt_sha256"] = _file_sha256(recovery_path)
    unsigned_campaign = dict(campaign)
    unsigned_campaign.pop("receipt_sha256", None)
    campaign["receipt_sha256"] = _payload_sha256(unsigned_campaign)
    _write_json(campaign_path, campaign)

    bundle_path = tampered / "bundle.manifest.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["campaign_receipt_sha256"] = _file_sha256(campaign_path)
    bundle["file_hashes"] = offline_campaign._bundle_files(tampered)
    unsigned_bundle = dict(bundle)
    unsigned_bundle.pop("bundle_digest_sha256", None)
    bundle["bundle_digest_sha256"] = _payload_sha256(unsigned_bundle)
    _write_json(bundle_path, bundle)
    with pytest.raises(ValueError, match="exact executed 100-cycle result"):
        validate_offline_campaign_bundle(tampered)


def test_bundle_validator_rejects_source_drift(campaign_bundle, tmp_path: Path) -> None:
    source_root = tmp_path / "repo-copy"
    repository_root = Path(offline_campaign.__file__).resolve().parents[4]
    for relative in SOURCE_RECEIPT_PATHS:
        source = repository_root / relative
        destination = source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    drifted = source_root / SOURCE_RECEIPT_PATHS[6]
    drifted.write_bytes(drifted.read_bytes() + b"\n# bounded source drift\n")
    with pytest.raises(ValueError, match="source drift"):
        validate_offline_campaign_bundle(campaign_bundle.root, source_root=source_root)


def test_episode_grouped_split_and_content_addressed_rerun(campaign_bundle) -> None:
    manifest = campaign_bundle.dataset_manifest
    assert manifest["schema"] == OFFLINE_DATASET_SCHEMA
    assert manifest["fixture_only"] is True
    assert manifest["production_promotion_allowed"] is False
    assert manifest["observation_shape"] == [84, 84]
    assert manifest["action_shape"] == [84, 12]
    assert manifest["split_episode_counts"] == {"train": 29, "validation": 6, "test": 7}
    assert len(set(manifest["episode_split"].values())) == 3
    for episode_id, split in manifest["episode_split"].items():
        assert split in {"train", "validation", "test"}
        assert episode_id in manifest["source_episode_artifact_hashes"]

    before = (campaign_bundle.root / "bundle.manifest.json").read_bytes()
    rerun = materialize_offline_campaign_bundle(campaign_bundle.root)
    after = (campaign_bundle.root / "bundle.manifest.json").read_bytes()
    assert before == after
    assert rerun.artifact_root_digest == campaign_bundle.artifact_root_digest
    assert validate_offline_campaign_bundle(campaign_bundle.root)["bundle_digest_sha256"] == campaign_bundle.artifact_root_digest
