"""Deterministic CPU proof for the offline model qualification lane."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from ur10e_vic.tacdiffusion import offline_model_qualification as qualification
from ur10e_vic.tacdiffusion.mainline_model import ConditionalActionModel, MainlineModelConfig


CAMPAIGN_ROOT = Path(__file__).parents[1] / "evidence" / "tacdiffusion_offline_fixture_campaign_v1"
REPO_ROOT = qualification.REPO_ROOT


def test_contract_is_full_84d_12d_50_step_and_single_worker() -> None:
    contract = qualification.QualificationContract()
    assert contract.as_dict()["observation_dimension"] == 84
    assert contract.as_dict()["action_dimension"] == 12
    assert contract.diffusion_steps == 50
    assert contract.hidden_dimension == 512
    assert contract.single_worker is True
    assert contract.blas_openmp_threads == 1
    assert contract.max_vram_fraction < 0.85


def test_frozen_fixture_bindings_and_episode_grouped_split_are_current() -> None:
    fixture = qualification.validate_fixture_campaign(CAMPAIGN_ROOT, repo_root=REPO_ROOT)
    assert fixture.observations.shape == (84, 84)
    assert fixture.actions.shape == (84, 12)
    assert len(set(fixture.episode_ids)) == 42
    assert fixture.bindings.bundle_digest_sha256 == qualification.EXPECTED_BUNDLE_DIGEST
    assert fixture.bindings.split_row_counts == {"train": 58, "validation": 12, "test": 14}
    assert all(
        len({split for episode, split in zip(fixture.episode_ids, fixture.splits) if episode == selected}) == 1
        for selected in set(fixture.episode_ids)
    )


def test_fixture_dataset_and_source_receipts_reject_tamper(tmp_path) -> None:
    dataset_copy = tmp_path / "dataset-tampered"
    shutil.copytree(CAMPAIGN_ROOT, dataset_copy)
    dataset_path = dataset_copy / "dataset.npz"
    dataset_path.write_bytes(dataset_path.read_bytes() + b"tamper")
    with pytest.raises(qualification.QualificationContractError, match="closure|tampered"):
        qualification.validate_fixture_campaign(dataset_copy, repo_root=REPO_ROOT)

    source_copy = tmp_path / "source-tampered"
    shutil.copytree(CAMPAIGN_ROOT, source_copy)
    source_receipt_path = source_copy / "source_identities.json"
    source_receipt = json.loads(source_receipt_path.read_text(encoding="utf-8"))
    source_receipt["files"][0]["sha256"] = "0" * 64
    source_receipt_path.write_text(json.dumps(source_receipt, sort_keys=True), encoding="utf-8")
    with pytest.raises(qualification.QualificationContractError, match="source"):
        qualification.validate_fixture_campaign(source_copy, repo_root=REPO_ROOT)


def test_gate_deferred_path_never_queries_cuda_or_creates_checkpoint(tmp_path, monkeypatch) -> None:
    def cuda_must_not_be_called():
        raise AssertionError("gate-closed path queried CUDA")

    monkeypatch.setattr(qualification, "_cuda_device_identity", cuda_must_not_be_called)
    report = qualification.run_cuda_qualification(
        CAMPAIGN_ROOT,
        output_root=tmp_path,
        repo_root=REPO_ROOT,
        gate_open=False,
    )
    assert report["cuda_execution_status"] == qualification.CUDA_GATE_DEFERRED
    assert report["qualification_executed"] is False
    assert report["checkpoint_artifacts"] == []
    assert report["no_fake_checkpoint"] is True
    assert not list(tmp_path.glob("*.pt"))


def test_reverse_sampler_trace_is_exactly_50_distinct_steps() -> None:
    assert qualification.REVERSE_STEP_TRACE == tuple(range(49, -1, -1))
    assert len(set(qualification.REVERSE_STEP_TRACE)) == 50
    with pytest.raises(qualification.QualificationContractError, match="exactly 50"):
        qualification.run_paced_diagnostics(
            lambda _observation, _seed: np.zeros(12, dtype=np.float32),
            np.zeros((1, 84), dtype=np.float32),
            checkpoint_sha256="a" * 64,
            dataset_sha256="b" * 64,
            source_receipt_sha256="c" * 64,
            code_source_hashes_binding={},
            sampler_trace=tuple(range(48, -1, -1)),
            warmup_samples=0,
            steady_samples=1,
        )


class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += max(0.0, duration)


class _SlowPredictor:
    def __init__(self, clock: _VirtualClock, latency_s: float) -> None:
        self.clock = clock
        self.latency_s = latency_s

    def predict(self, _observation, _seed):
        self.clock.now += self.latency_s
        return np.zeros(12, dtype=np.float32)


def _diagnostic(clock: _VirtualClock) -> dict[str, object]:
    return qualification.run_paced_diagnostics(
        _SlowPredictor(clock, 0.02),
        np.zeros((2, 84), dtype=np.float32),
        checkpoint_sha256="a" * 64,
        dataset_sha256="b" * 64,
        source_receipt_sha256="c" * 64,
        code_source_hashes_binding={"qualification.py": "d" * 64},
        warmup_samples=1,
        steady_samples=4,
        clock=clock.clock,
        sleeper=clock.sleep,
    )


def test_diagnostics_record_warmup_deadline_stale_transition_and_governed_stop() -> None:
    artifact = _diagnostic(_VirtualClock())
    assert artifact["selection_eligible"] is False
    assert artifact["selected_rate_hz"] is None
    assert artifact["formal_rate_selection"] is False
    for result in artifact["rate_results"]:
        assert result["warmup_samples"]
        assert result["stale_action_transition"] is True
        assert result["governed_stop"] is True
        assert result["governed_stop_reason"] == "bounded_stale_or_deadline_policy"
        assert result["deadline_misses"] >= 2


def test_structural_diagnostic_validator_accepts_current_rows() -> None:
    artifact = _diagnostic(_VirtualClock())
    assert qualification.validate_diagnostic_artifact(
        artifact,
        expected_checkpoint_sha256="a" * 64,
        expected_dataset_sha256="b" * 64,
        expected_source_receipt_sha256="c" * 64,
        expected_code_source_hashes={"qualification.py": "d" * 64},
    ) == artifact


def test_diagnostic_rehash_cannot_promote_short_rate(tmp_path) -> None:
    artifact = _diagnostic(_VirtualClock())
    tampered = dict(artifact)
    tampered["selected_rate_hz"] = 100
    unsigned = dict(tampered)
    unsigned.pop("artifact_sha256")
    tampered["artifact_sha256"] = qualification.canonical_sha256(unsigned)
    with pytest.raises(qualification.QualificationContractError, match="cannot be promoted"):
        qualification.validate_diagnostic_artifact(
            tampered,
            expected_checkpoint_sha256="a" * 64,
            expected_dataset_sha256="b" * 64,
            expected_source_receipt_sha256="c" * 64,
            expected_code_source_hashes={"qualification.py": "d" * 64},
        )


def test_k800_candidate_is_content_addressed_single_variable_delta(tmp_path) -> None:
    before = qualification.sha256_file(REPO_ROOT / qualification.K600_SOURCE_RELATIVE_PATH)
    manifest = qualification.materialize_k800_candidate(tmp_path, repo_root=REPO_ROOT)
    after = qualification.sha256_file(REPO_ROOT / qualification.K600_SOURCE_RELATIVE_PATH)
    assert before == qualification.EXPECTED_K600_SOURCE_SHA256 == after
    assert manifest["candidate_stiffness_6d"] == [800.0, 800.0, 800.0, 30.0, 30.0, 30.0]
    assert manifest["semantic_delta"] == ["translational_stiffness"]
    assert manifest["current"] is False
    assert manifest["active"] is False
    assert manifest["active_allowed"] is False
    assert manifest["deployment_allowed"] is False
    assert manifest["live_qualified"] is False
    assert qualification.validate_k800_candidate(tmp_path / "candidate.manifest.json", repo_root=REPO_ROOT)["candidate_sha256"] == manifest["candidate_sha256"]


def test_k800_candidate_rejects_activation_and_source_drift(tmp_path) -> None:
    qualification.materialize_k800_candidate(tmp_path, repo_root=REPO_ROOT)
    manifest_path = tmp_path / "candidate.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["active"] = True
    unsigned = dict(manifest)
    unsigned.pop("artifact_sha256")
    manifest["artifact_sha256"] = qualification.canonical_sha256(unsigned)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(qualification.QualificationContractError, match="activation boundary"):
        qualification.validate_k800_candidate(manifest_path, repo_root=REPO_ROOT)

    qualification.materialize_k800_candidate(tmp_path, repo_root=REPO_ROOT)
    temporary_repo = tmp_path / "repo-copy"
    source_copy = temporary_repo / qualification.K600_SOURCE_RELATIVE_PATH
    source_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / qualification.K600_SOURCE_RELATIVE_PATH, source_copy)
    source_copy.write_bytes(source_copy.read_bytes() + b"\n")
    with pytest.raises(qualification.QualificationContractError, match="immutable K=600"):
        qualification.validate_k800_candidate(manifest_path, repo_root=temporary_repo)


def test_wrong_dimensions_and_legacy_model_config_fail_closed() -> None:
    with pytest.raises(ValueError, match="84D"):
        MainlineModelConfig(observation_dimension=36)
    with pytest.raises(qualification.QualificationContractError, match="84"):
        qualification.run_paced_diagnostics(
            lambda _observation, _seed: np.zeros(12, dtype=np.float32),
            np.zeros((1, 36), dtype=np.float32),
            checkpoint_sha256="a" * 64,
            dataset_sha256="b" * 64,
            source_receipt_sha256="c" * 64,
            code_source_hashes_binding={},
            warmup_samples=0,
            steady_samples=1,
        )


def test_invalid_uppercase_hash_is_rejected() -> None:
    with pytest.raises(qualification.QualificationContractError, match="lowercase"):
        qualification.canonical_sha256({"digest": "A" * 64}) if qualification._require_sha256("A" * 64, "digest") else None


def test_missing_checkpoint_receipt_fails_closed_without_cuda(tmp_path) -> None:
    with pytest.raises(qualification.QualificationContractError, match="missing"):
        qualification.validate_checkpoint_binding(
            tmp_path / "missing.pt",
            {},
            require_cuda=False,
        )


def test_tampered_checkpoint_binding_fails_closed_without_cuda(tmp_path) -> None:
    checkpoint = tmp_path / "tampered.pt"
    if qualification.torch is None:
        checkpoint.write_bytes(b"tampered checkpoint")
        expected_message = "PyTorch"
    else:
        qualification.torch.save({"checkpoint_binding": {"fixture_only": True}}, checkpoint)
        expected_message = "binding"
    with pytest.raises(qualification.QualificationContractError, match=expected_message):
        qualification.validate_checkpoint_binding(
            checkpoint,
            {"fixture_only": True, "device": {"device_index": 0}},
            require_cuda=False,
        )


def test_deferred_rejects_resigned_nested_promotion_and_tampered_contract(tmp_path) -> None:
    qualification_root = tmp_path / "qualification"
    shutil.copytree(
        Path(__file__).parents[1] / "evidence" / "tacdiffusion_offline_model_qualification_v1",
        qualification_root,
    )
    report_path = qualification_root / qualification.DEFERRED_ARTIFACT_NAME
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["diagnostics"]["selection_eligible"] = True
    report["diagnostics"]["selected_rate_hz"] = 100
    report["diagnostics"]["formal_rate_selection"] = True
    report["artifact_sha256"] = qualification.canonical_sha256({key: value for key, value in report.items() if key != "artifact_sha256"})
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(qualification.QualificationContractError, match="promoted|altered|contract"):
        qualification.validate_gate_deferred_evidence(report_path, bundle_root=CAMPAIGN_ROOT, repo_root=REPO_ROOT)

    shutil.copytree(
        Path(__file__).parents[1] / "evidence" / "tacdiffusion_offline_model_qualification_v1",
        qualification_root,
        dirs_exist_ok=True,
    )
    contract_path = qualification_root / qualification.CONTRACT_ARTIFACT_NAME
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["contract"]["model_update_rate_hz"] = 50
    contract["contract_sha256"] = qualification.canonical_sha256(contract["contract"])
    contract["artifact_sha256"] = qualification.canonical_sha256({key: value for key, value in contract.items() if key != "artifact_sha256"})
    contract_path.write_text(json.dumps(contract, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(qualification.QualificationContractError, match="frozen contract|contract"):
        qualification.validate_gate_deferred_evidence(report_path, bundle_root=CAMPAIGN_ROOT, repo_root=REPO_ROOT)


def test_diagnostic_rejects_erased_rows_even_after_resigning() -> None:
    artifact = _diagnostic(_VirtualClock())
    tampered = json.loads(json.dumps(artifact))
    del tampered["rate_results"][0]["warmup_samples"]
    tampered["artifact_sha256"] = qualification.canonical_sha256({key: value for key, value in tampered.items() if key != "artifact_sha256"})
    with pytest.raises(qualification.QualificationContractError, match="fields|warmup"):
        qualification.validate_diagnostic_artifact(
            tampered,
            expected_checkpoint_sha256="a" * 64,
            expected_dataset_sha256="b" * 64,
            expected_source_receipt_sha256="c" * 64,
            expected_code_source_hashes={"qualification.py": "d" * 64},
        )


def test_k800_rejects_resigned_empty_unchanged_identities_and_bound_sanity(tmp_path) -> None:
    qualification.materialize_k800_candidate(tmp_path, repo_root=REPO_ROOT)
    manifest_path = tmp_path / "candidate.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unchanged_identities"] = []
    unsigned = {key: value for key, value in manifest.items() if key != "artifact_sha256"}
    candidate_address = {key: value for key, value in unsigned.items() if key != "candidate_sha256"}
    manifest["candidate_sha256"] = qualification.canonical_sha256(candidate_address)
    unsigned["candidate_sha256"] = manifest["candidate_sha256"]
    manifest["artifact_sha256"] = qualification.canonical_sha256(unsigned)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    sanity_path = tmp_path / "numeric_sanity.json"
    sanity = json.loads(sanity_path.read_text(encoding="utf-8"))
    sanity["candidate_sha256"] = manifest["candidate_sha256"]
    sanity["artifact_sha256"] = qualification.canonical_sha256({key: value for key, value in sanity.items() if key != "artifact_sha256"})
    sanity_path.write_text(json.dumps(sanity, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(qualification.QualificationContractError, match="canonical semantic|unchanged"):
        qualification.validate_k800_candidate(manifest_path, repo_root=REPO_ROOT)


def test_receipt_path_portability_is_explicit_and_negative() -> None:
    portable = qualification._portableize_receipt(
        {
            "train_result": {"checkpoint_path": "/home/andy/worktree/fixture_checkpoint.initial.pt"},
            "resume_result": {"source_checkpoint_path": "/tmp/fixture_checkpoint.initial.pt"},
            "evaluation": {"checkpoint_path": "evidence/run/fixture_checkpoint.resumed.pt"},
        }
    )
    assert portable == {
        "train_result": {"checkpoint_path": "fixture_checkpoint.initial.pt"},
        "resume_result": {"source_checkpoint_path": "fixture_checkpoint.initial.pt"},
        "evaluation": {"checkpoint_path": "fixture_checkpoint.resumed.pt"},
    }
    with pytest.raises(qualification.QualificationContractError, match="non-portable"):
        qualification._assert_portable_payload({"path": "/home/andy/worktree/fixture_checkpoint.initial.pt"})


def _make_synthetic_cuda_evidence(root: Path) -> None:
    if qualification.torch is None:
        pytest.skip("PyTorch is required for CPU checkpoint metadata fixtures")
    fixture = qualification.validate_fixture_campaign(CAMPAIGN_ROOT, repo_root=REPO_ROOT)
    contract = qualification.QualificationContract()
    contract_payload = qualification.build_contract_payload(fixture, repo_root=REPO_ROOT)
    device = {
        "backend": "cuda",
        "device_index": 0,
        "device_name": "synthetic-cuda-metadata",
        "compute_capability": [8, 0],
        "total_memory_bytes": 1_000_000,
        "torch_version": "synthetic",
        "platform": "synthetic-cpu-test",
    }
    execution = {
        "environment_threads": {
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        },
        "torch_num_threads": 1,
        "torch_num_interop_threads": 1,
        "deterministic_algorithms_requested": True,
        "deterministic_algorithms_enabled": True,
        "deterministic_warn_only": False,
        "device": device,
        "device_string": "cuda:0",
        "unavoidable_cuda_nondeterminism": [],
    }
    binding = qualification._build_checkpoint_binding(fixture, contract_payload, device)
    model = ConditionalActionModel(contract.model_config())
    train_indices = [index for index, split in enumerate(fixture.splits) if split == "train"]
    validation_indices = [index for index, split in enumerate(fixture.splits) if split == "validation"]
    test_indices = [index for index, split in enumerate(fixture.splits) if split == "test"]

    def write_checkpoint(path: Path, losses: list[float], epoch_completed: int) -> None:
        qualification.torch.save(
            {
                "schema": "ur10e_tacdiffusion_checkpoint/v3",
                "config": contract.model_config().__dict__,
                "model_state_dict": model.state_dict(),
                "normalization": {
                    "observation_mean": qualification.torch.zeros(84),
                    "observation_std": qualification.torch.ones(84),
                    "action_mean": qualification.torch.zeros(12),
                    "action_std": qualification.torch.ones(12),
                },
                "train_indices": train_indices,
                "validation_indices": validation_indices,
                "test_indices": test_indices,
                "losses": losses,
                "validation_loss": 0.25,
                "checkpoint_binding": binding,
                "optimizer_state_dict": {},
                "generator_state": qualification.torch.Generator().get_state(),
                "epoch_completed": epoch_completed,
            },
            path,
        )

    root.mkdir(parents=True, exist_ok=True)
    initial_path = root / qualification.INITIAL_CHECKPOINT_NAME
    resumed_path = root / qualification.RESUMED_CHECKPOINT_NAME
    write_checkpoint(initial_path, [1.0], 1)
    write_checkpoint(resumed_path, [1.0, 0.5], 2)
    initial_result = {
        "checkpoint_path": str(initial_path),
        "training_loss": [1.0],
        "validation_loss": 0.25,
        "train_count": 58,
        "validation_count": 12,
        "test_count": 14,
        "model_schema": "ur10e_tacdiffusion_checkpoint/v3",
        "runtime": "cuda:0",
        "diffusion_steps": 50,
    }
    resumed_result = {
        "checkpoint_path": str(resumed_path),
        "training_loss": [0.5],
        "validation_loss": 0.2,
        "train_count": 58,
        "validation_count": 12,
        "test_count": 14,
        "model_schema": "ur10e_tacdiffusion_checkpoint/v3",
        "runtime": "cuda:0",
        "diffusion_steps": 50,
        "resume": {
            "resumed": True,
            "source_checkpoint_path": str(initial_path),
            "optimizer_state_restored": True,
            "generator_state_restored": True,
            "epoch_completed": 2,
        },
    }
    memory_initial = {
        "stage": "initial_train",
        "peak_allocated_bytes": 100_000,
        "peak_reserved_bytes": 200_000,
        "device_total_memory_bytes": 1_000_000,
        "peak_fraction": 0.2,
        "limit_fraction_strictly_below": contract.max_vram_fraction,
        "below_85_percent": True,
    }
    memory_resumed = dict(memory_initial, stage="resume_train")
    initial_receipt = qualification._checkpoint_receipt(
        initial_path,
        stage="initial",
        fixture=fixture,
        contract_payload=contract_payload,
        execution=execution,
        extra={"train_result": initial_result, "memory": memory_initial},
    )
    resumed_receipt = qualification._checkpoint_receipt(
        resumed_path,
        stage="resumed",
        fixture=fixture,
        contract_payload=contract_payload,
        execution=execution,
        extra={"resume_result": resumed_result, "memory": memory_resumed},
    )
    evaluation = qualification._artifact_with_hash(
        {
            "schema": "ur10e_tacdiffusion_evaluation/v2",
            "checkpoint_path": qualification.RESUMED_CHECKPOINT_NAME,
            "split": "test",
            "sample_count": 14,
            "diffusion_steps": 50,
            "distinct_reverse_steps": 50,
            "reverse_step_trace": list(qualification.REVERSE_STEP_TRACE),
            "normalized_action_mse": 0.1,
            "device": "cuda:0",
            "require_cuda": True,
            "checkpoint_binding": binding,
            "fixture_only": True,
            "formal_checkpoint": False,
            "active_allowed": False,
            "active": False,
            "reproduction_status": "not_claimed",
            "model_rate_selected_hz": None,
            "memory": dict(memory_initial, stage="evaluate_and_sampler"),
            "checkpoint_sha256": resumed_receipt["checkpoint_sha256"],
            "dataset_sha256": fixture.bindings.dataset_sha256,
            "source_receipt_sha256": fixture.bindings.source_receipt_sha256,
            "code_source_hashes": dict(contract_payload["code_source_hashes"]),
        }
    )
    runtime_clock = _VirtualClock()
    runtime_diagnostics = qualification.run_paced_diagnostics(
        _SlowPredictor(runtime_clock, 0.001),
        fixture.observations,
        checkpoint_sha256=resumed_receipt["checkpoint_sha256"],
        dataset_sha256=fixture.bindings.dataset_sha256,
        source_receipt_sha256=fixture.bindings.source_receipt_sha256,
        code_source_hashes_binding=contract_payload["code_source_hashes"],
        warmup_samples=1,
        steady_samples=4,
        clock=runtime_clock.clock,
        sleeper=runtime_clock.sleep,
    )
    probe = qualification.build_deterministic_lifecycle_policy_probe(
        fixture.observations,
        checkpoint_sha256=resumed_receipt["checkpoint_sha256"],
        dataset_sha256=fixture.bindings.dataset_sha256,
        source_receipt_sha256=fixture.bindings.source_receipt_sha256,
        code_source_hashes_binding=contract_payload["code_source_hashes"],
    )
    diagnostics = dict(runtime_diagnostics, lifecycle_policy_probe=probe)
    diagnostics = qualification._artifact_with_hash(diagnostics)
    report = qualification._artifact_with_hash(
        {
            "schema": qualification.QUALIFICATION_SCHEMA,
            "status": "cuda_qualified_fixture_only",
            "cuda_execution_status": qualification.CUDA_GATE_OPEN,
            "fixture_only": True,
            "production_promotion_allowed": False,
            "formal_checkpoint": False,
            "active_allowed": False,
            "active": False,
            "reproduction_status": "not_claimed",
            "model_rate_selected_hz": None,
            "selection_eligible": False,
            "selected_rate_hz": None,
            "formal_rate_selection": False,
            "qualification_executed": True,
            "no_cpu_fallback": True,
            "execution": execution,
            "fixture_binding": fixture.bindings.as_dict(),
            "code_source_hashes": dict(contract_payload["code_source_hashes"]),
            "contract_artifact": qualification.CONTRACT_ARTIFACT_NAME,
            "contract_artifact_sha256": qualification._serialized_json_sha256(contract_payload),
            "checkpoint_artifacts": [initial_receipt, resumed_receipt],
            "evaluation_artifacts": [evaluation],
            "diagnostic_artifacts": [diagnostics],
            "sampler_smoke": {
                "executed": True,
                "diffusion_steps": 50,
                "distinct_reverse_steps": 50,
                "reverse_step_trace": list(qualification.REVERSE_STEP_TRACE),
            },
        }
    )
    qualification._write_json_atomic(root / qualification.CONTRACT_ARTIFACT_NAME, contract_payload)
    qualification._write_json_atomic(root / qualification.EVALUATION_ARTIFACT_NAME, evaluation)
    qualification._write_json_atomic(root / qualification.DIAGNOSTICS_ARTIFACT_NAME, diagnostics)
    qualification._write_json_atomic(root / qualification.CUDA_ARTIFACT_NAME, report)


def test_cuda_validator_is_cpu_metadata_only_and_rejects_tampered_bindings(tmp_path, monkeypatch) -> None:
    root = tmp_path / "valid-cuda"
    _make_synthetic_cuda_evidence(root)
    if qualification.torch is not None:
        monkeypatch.setattr(qualification.torch.cuda, "is_available", lambda: pytest.fail("CUDA must not be queried"))
    report_path = root / qualification.CUDA_ARTIFACT_NAME
    assert qualification.validate_cuda_qualification_evidence(report_path, bundle_root=CAMPAIGN_ROOT, repo_root=REPO_ROOT)["status"] == "cuda_qualified_fixture_only"

    for name, mutate, pattern in (
        (
            "checkpoint",
            lambda evidence_root: (evidence_root / qualification.RESUMED_CHECKPOINT_NAME).write_bytes((evidence_root / qualification.RESUMED_CHECKPOINT_NAME).read_bytes() + b"tamper"),
            "checkpoint|bytes|receipt",
        ),
        (
            "evaluation",
            lambda evidence_root: _tamper_json_artifact(evidence_root / qualification.EVALUATION_ARTIFACT_NAME, "checkpoint_sha256", "0" * 64),
            "evaluation|binding",
        ),
        (
            "diagnostic",
            lambda evidence_root: _tamper_json_artifact(evidence_root / qualification.DIAGNOSTICS_ARTIFACT_NAME, "dataset_sha256", "0" * 64),
            "diagnostic|binding",
        ),
    ):
        tampered = tmp_path / f"tampered-{name}"
        shutil.copytree(root, tampered)
        mutate(tampered)
        with pytest.raises(qualification.QualificationContractError, match=pattern):
            qualification.validate_cuda_qualification_evidence(tampered / qualification.CUDA_ARTIFACT_NAME, bundle_root=CAMPAIGN_ROOT, repo_root=REPO_ROOT)


def _tamper_json_artifact(path: Path, field: str, value: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    payload["artifact_sha256"] = qualification.canonical_sha256({key: item for key, item in payload.items() if key != "artifact_sha256"})
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_validation_rejects_torn_mixed_outputs_and_routes_cuda_state(tmp_path, monkeypatch) -> None:
    source_root = Path(__file__).parents[1] / "evidence" / "tacdiffusion_offline_model_qualification_v1"
    torn = tmp_path / "torn"
    shutil.copytree(source_root, torn)
    (torn / "evaluation.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(qualification.QualificationContractError, match="partial|mixed|unexpected"):
        qualification.validate_gate_deferred_evidence(torn / qualification.DEFERRED_ARTIFACT_NAME, bundle_root=CAMPAIGN_ROOT, repo_root=REPO_ROOT)

    import importlib.util

    tool_path = Path(__file__).parents[1] / "tools" / "materialize_tacdiffusion_offline_model_qualification.py"
    spec = importlib.util.spec_from_file_location("materialize_offline_qualification_test", tool_path)
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    qualification_root = tmp_path / "route"
    candidate_root = tmp_path / "candidate"
    qualification_root.mkdir()
    candidate_root.mkdir()
    (qualification_root / qualification.CUDA_ARTIFACT_NAME).write_text("{}\n", encoding="utf-8")
    (candidate_root / "candidate.manifest.json").write_text("{}\n", encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(tool.qualification, "validate_k800_candidate", lambda *args, **kwargs: {"candidate_sha256": "a" * 64})
    monkeypatch.setattr(tool.qualification, "validate_cuda_qualification_evidence", lambda *args, **kwargs: seen.append("cuda") or {"cuda_execution_status": qualification.CUDA_GATE_OPEN, "artifact_sha256": "b" * 64})
    monkeypatch.setattr(tool.qualification, "validate_gate_deferred_evidence", lambda *args, **kwargs: seen.append("deferred") or {"cuda_execution_status": qualification.CUDA_GATE_DEFERRED, "artifact_sha256": "c" * 64})
    result = tool._validate_existing(qualification_root, candidate_root, CAMPAIGN_ROOT, REPO_ROOT)
    assert seen == ["cuda"]
    assert result["cuda_execution_status"] == qualification.CUDA_GATE_OPEN
