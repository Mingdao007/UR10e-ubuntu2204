"""Focused offline regressions for the r005 P0 repair lane."""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from step5d_autotune_v4_r004.home import evaluate_entry_home
from step5d_autotune_v4_r004.home_profile import load_fixed_home_profile
from step5d_autotune_v4_r004.path_reference import (
    BOUND_FRAME_SEMANTIC_SHA256,
    BOUND_STAGE_ROW_SEMANTIC_SHA256,
    R004_PATH_FRAME_SNAPSHOT,
    R004_PATH_ROW_SNAPSHOT,
    step5_path_reference,
)
from step5d_autotune_v4_r004.evidence import AttemptEvidence
from step5d_autotune_v4_r004.timing import TimingEvidence
from step5d_autotune_v4_r005.contracts import Candidate, load_contract
from step5d_autotune_v4_r005.observations import (
    ObservationError,
    ObservationLedger,
    ObservationRecord,
    VerifiedObservationEvidence,
)
from step5d_autotune_v4_r005.packet_pacing import (
    FreshFrameDeadlineTransport,
    PacketPacingError,
)
from step5d_autotune_v4_r005.optimizer import (
    OptimizerBindingError,
    OptimizerError,
    V4BoAdapter,
)
from step5d_autotune_v4_r005.runtime import Attempt
from step5d_autotune_v4_r005.offline_test_factory import synthetic_force_objective
from step5d_force_objective import ForceObjectiveBuilder, ForcePathSample


ROOT = Path(__file__).resolve().parents[1]


def _as_plain(value):
    if isinstance(value, dict):
        return {key: _as_plain(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {key: _as_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_as_plain(item) for item in value]
    return value


def _raw_fixture(force: float = 5.0):
    builder = ForceObjectiveBuilder()
    samples = []
    for index in range(600):
        sample = ForcePathSample(
            path_time_s=index / 10.0,
            path_phase=25,
            filtered_normal_n=force,
            source_sequences={"packet": index + 1, "rtde": index + 1},
            source_ages_s={"packet": 0.001, "rtde": 0.001},
        )
        builder.add(sample)
        samples.append(sample)
    return builder.finalize(provenance="raw_path_evidence"), tuple(samples)


def _record(contract, objective, sequence: int = 1, *, raw_path_samples=()) -> ObservationRecord:
    return ObservationRecord(
        campaign_fingerprint=contract.campaign_fingerprint,
        epoch=1,
        attempt_sequence=sequence,
        kind="BOOTSTRAP_PD",
        candidate=Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=True,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=False,
        duration_s=60.0,
        force_objective=objective,
        metrics={"execution_id": f"r005-repair-execution-{sequence}"},
        raw_path_samples=tuple(raw_path_samples),
    ).seal()


def _qualification_record(contract, sequence: int) -> ObservationRecord:
    return ObservationRecord(
        campaign_fingerprint=contract.campaign_fingerprint,
        epoch=1,
        attempt_sequence=sequence,
        kind="QUALIFICATION",
        candidate=Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=True,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=True,
        duration_s=0.0,
        metrics={"execution_id": f"r005-repair-qualification-{sequence}"},
    ).seal()


def test_restart_reconciles_persisted_tp_echo_and_cached_frame_does_not_publish() -> None:
    transport = FreshFrameDeadlineTransport(start_monotonic_s=0.0)
    assert transport.reconcile_consumed_echo(204472) == 204473
    published: list[int] = []

    first = transport.cycle(1.0, now_s=0.0, publish=published.append)
    cached = transport.cycle(None, now_s=0.002, publish=published.append)
    equal = transport.cycle(1.0, now_s=0.004, publish=published.append)

    assert first.fresh and first.packet_sequence == 204473
    assert not cached.fresh and cached.packet_sequence == 204473
    assert not equal.fresh and equal.packet_sequence == 204473
    assert published == [204473]
    assert transport.gate is not None
    assert transport.gate.next_packet_sequence == 204474


def test_495_hz_distinct_frames_against_500_hz_host_have_no_backlog_for_60s() -> None:
    transport = FreshFrameDeadlineTransport(start_monotonic_s=0.0)
    transport.reconcile_consumed_echo(-1)
    published: list[int] = []
    published_times: list[float] = []
    for index in range(30_001):
        now_s = index / 500.0
        # A real monotonic 495 Hz frame identity sampled by a 500 Hz host.
        frame_identity = index * 495 // 500
        decision = transport.cycle(
            frame_identity,
            now_s=now_s,
            publish=lambda sequence, now_s=now_s: (
                published.append(sequence),
                published_times.append(now_s),
            ),
        )
        assert decision.stale_age_s < 0.080

    assert len(published) == 29_701
    assert published == list(range(len(published)))
    assert len(set(published)) == 29_701
    observed_hz = (len(published_times) - 1) / (
        published_times[-1] - published_times[0]
    )
    assert observed_hz >= 460.0


def test_genuine_no_fresh_controller_output_fails_closed_at_80ms() -> None:
    transport = FreshFrameDeadlineTransport(start_monotonic_s=0.0)
    transport.cycle(1.0, now_s=0.0, publish=lambda _sequence: None)
    transport.cycle(None, now_s=0.079999, publish=lambda _sequence: None)
    with pytest.raises(PacketPacingError, match="80 ms"):
        transport.cycle(None, now_s=0.080, publish=lambda _sequence: None)


def test_bound_path_snapshot_matches_current_and_canonical_relevant_row_and_ignores_global_mutation(monkeypatch) -> None:
    current = json.loads((ROOT / "config/step5_stage_table.json").read_text())
    frozen = json.loads(
        (
            Path("/home/andy/.codex-worktrees/step5d-r034-live-20260730")
            / "experiments/tase-contact-reproduction/config/step5_stage_table.json"
        ).read_text()
    )
    current_row = next(row for row in current["stages"] if row["id"] == "step5d_strict_rnn_autotune_v1")
    frozen_row = next(row for row in frozen["stages"] if row["id"] == "step5d_strict_rnn_autotune_v1")
    relevant_keys = ("id", "shape", "duration_s", "amplitude_m", "phase_law", "frame")
    assert {key: current_row[key] for key in relevant_keys} == {
        key: frozen_row[key] for key in relevant_keys
    }
    row_sha = hashlib.sha256(
        json.dumps(current_row, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    frame = json.loads((ROOT / current_row["frame"]).read_text())
    frame_sha = hashlib.sha256(
        json.dumps(frame, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert row_sha == BOUND_STAGE_ROW_SEMANTIC_SHA256
    assert frame_sha == BOUND_FRAME_SEMANTIC_SHA256
    assert _as_plain(R004_PATH_ROW_SNAPSHOT)["id"] == current_row["id"]
    assert _as_plain(R004_PATH_FRAME_SNAPSHOT)["basis"] == {
        key: frame["basis"][key]
        for key in ("origin_xy_m", "u_along_xy", "p_lateral_xy")
    }

    import step5_table

    monkeypatch.setattr(step5_table, "step5_path_reference", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("global table was consumed")))
    assert step5_path_reference("step5d_strict_rnn_autotune_v1", (0.0, 0.0), 30.0)["path_time_s"] == 30.0


def test_fixed_home_entry_is_verified_without_a_transfer_route() -> None:
    profile = load_fixed_home_profile()
    decision = evaluate_entry_home(profile, profile.pose, (0.0,) * 6)
    assert decision.passed
    assert decision.branch is not None
    assert decision.verification.pose_verified


def test_path_adapter_keeps_execution_identity() -> None:
    from step5d_autotune_v4_r005.live_adapter import R005LiveWriterAdapter

    timing = TimingEvidence(
        duration_s=60.0,
        successful_writer_publishes=30000,
        distinct_rtde_frames=30000,
        distinct_kunwei_frames=30000,
        distinct_tp_consumed_packet_echoes=30000,
        feedback_age_p99_s=0.001,
        max_fresh_gap_s=0.002,
    )

    class Writer:
        offline_test_mode = True
        _path_sample_sink = None

        def run_60s(self, _attempt):
            return AttemptEvidence(
                complete_bins=550,
                effective_rate_hz=500.0,
                p99_packet_interval_s=0.002,
                max_packet_interval_s=0.002,
                mae_n=1.0,
                objective=1.0,
                safety_gate_passed=True,
                contact_gate_passed=True,
                return_gate_passed=True,
                home_proof={"stationary": True},
                path_samples=550,
                path_bin_ids=tuple(range(550)),
                evidence_sha256="a" * 64,
                path_duration_s=60.0,
                path_phase=6,
                xy_error_p95_m=0.0,
                xy_error_max_m=0.0,
                endpoint_error_max_m=0.0,
                qd_correlation=1.0,
                qd_lag_s=0.001,
                timing_evidence=timing,
            )

    attempt = Attempt(
        epoch=1,
        attempt_sequence=9,
        candidate=Candidate(),
        kind="BO_TRIAL",
        dispatch_sequence=1,
        request_uid="request-path",
        execution_id="r005-path-execution-9",
    )
    result = R005LiveWriterAdapter(Writer(), contract=load_contract()).run_60s(attempt)
    assert result.execution_id == attempt.execution_id


def test_cold_read_rejects_tampered_sealed_objective_even_after_chain_rehash(tmp_path) -> None:
    contract = load_contract()
    path = tmp_path / "observations.jsonl"
    ledger = ObservationLedger(
        path,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    objective, samples = _raw_fixture()
    ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows[1]["force_objective"]["v2_mae_n"] = 0.123456789
    body = {key: value for key, value in rows[1].items() if key != "row_sha256"}
    rows[1]["row_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
    )
    with pytest.raises(ObservationError):
        ledger.fresh_process_verify()


def test_raw_artifact_is_immutable_evidence_and_byte_tamper_fails_cold_read(tmp_path) -> None:
    contract = load_contract()
    objective, samples = _raw_fixture(5.25)
    ledger = ObservationLedger(
        tmp_path / "raw-tamper.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    row = ledger.rows[0]
    binding = row["raw_artifact"]
    artifact = ledger.path.parent / binding["relative_path"]
    original = artifact.read_bytes()
    tampered = original.replace(b'"filtered_normal_n":5.25', b'"filtered_normal_n":5.35', 1)
    assert tampered != original
    artifact.write_bytes(tampered)
    assert "raw_path_samples" not in row["metrics"]
    with pytest.raises(ObservationError):
        ledger.fresh_process_verify()


def test_raw_artifact_swap_between_attempts_fails_identity_or_byte_binding(tmp_path) -> None:
    contract = load_contract()
    objective, samples = _raw_fixture(5.0)
    ledger = ObservationLedger(
        tmp_path / "raw-swap.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    ledger.append(_record(contract, objective, 2, raw_path_samples=samples))
    rows = ledger.rows
    first = ledger.path.parent / rows[0]["raw_artifact"]["relative_path"]
    second = ledger.path.parent / rows[1]["raw_artifact"]["relative_path"]
    first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
    first.write_bytes(second_bytes)
    second.write_bytes(first_bytes)
    with pytest.raises(ObservationError):
        ledger.fresh_process_verify()


def test_raw_artifact_store_rejects_symlink_root(tmp_path) -> None:
    target = tmp_path / "real-artifacts"
    target.mkdir()
    link = tmp_path / "artifact-link"
    link.symlink_to(target, target_is_directory=True)
    contract = load_contract()
    with pytest.raises(ObservationError, match="symlink"):
        ObservationLedger(
            tmp_path / "symlink-root.jsonl",
            campaign_fingerprint=contract.campaign_fingerprint,
            eoat_sha256="a" * 64,
            artifact_root=link,
        )


def test_complete_fabricated_receipt_cannot_cross_ledger_or_optimizer_seam(tmp_path) -> None:
    contract = load_contract()
    forged, _samples = _raw_fixture(5.123456789)
    # All current stats/digests/seal fields are present, but there is no raw
    # sample tuple to persist.  The direct record therefore remains unverified.
    records = tuple(_record(contract, forged, index) for index in range(1, 7))
    assert all(not record.eligible for record in records)
    ledger = ObservationLedger(
        tmp_path / "forge.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    with pytest.raises(ObservationError):
        ledger.append(records[0])
    with pytest.raises(OptimizerError):
        V4BoAdapter(ask_impl=lambda *_: (_ for _ in ()).throw(AssertionError("must reject"))).ask(
            observations=records,
            pending=(),
            incumbent=Candidate(),
        )


def test_structural_binding_and_public_verified_wrapper_cannot_enter_optimizer(tmp_path) -> None:
    contract = load_contract()
    objective, samples = _raw_fixture()
    ledger = ObservationLedger(
        tmp_path / "wrapper.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    verified = ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    assert isinstance(verified.verified_evidence, VerifiedObservationEvidence)
    with pytest.raises(ObservationError, match="only be created by the ledger seam"):
        VerifiedObservationEvidence(verified.force_objective, verified.raw_artifact)

    structural = ObservationRecord(
        campaign_fingerprint=contract.campaign_fingerprint,
        epoch=verified.epoch,
        attempt_sequence=verified.attempt_sequence,
        kind=verified.kind,
        candidate=verified.candidate,
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=True,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=False,
        duration_s=60.0,
        force_objective=verified.force_objective,
        metrics={"execution_id": verified.raw_artifact.execution_id},
        sealed=True,
        raw_artifact=verified.raw_artifact,
    )
    assert structural.verified_evidence is None
    assert structural.eligible is False
    with pytest.raises(OptimizerError):
        V4BoAdapter().tell(structural)
    from step5d_autotune_v4_r005.optimizer_worker import (
        OptimizerWorkerError,
        _observation,
    )

    with pytest.raises(OptimizerWorkerError):
        _observation(structural.payload())


def test_worker_rejects_complete_caller_forged_objective_binding_and_request_mapping(
    tmp_path,
) -> None:
    """The child wire cannot be upgraded by copying every durable JSON field."""

    from step5d_autotune_v4_r005.optimizer_worker import (
        OptimizerWorkerError,
        _observation,
        _request,
    )
    from step5d_optimizer_runtime import ATTESTATION_SCHEMA, REQUEST_SCHEMA

    contract = load_contract()
    objective, samples = _raw_fixture(5.123456789)
    ledger = ObservationLedger(
        tmp_path / "forged-wire.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    verified = ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    forged = json.loads(json.dumps(verified.payload()))
    forged["force_objective"]["v2_mae_n"] = 0.123456789
    forged["force_objective"]["objective"] = 0.123456789
    forged["verified_evidence"]["objective"] = dict(forged["force_objective"])
    forged["raw_artifact"]["objective_sha256"] = "b" * 64
    forged["verified_evidence"]["raw_artifact"] = dict(forged["raw_artifact"])

    # Even a complete, internally duplicated caller mapping is not an
    # observation capability.  The only worker observation path is the ledger.
    with pytest.raises(OptimizerWorkerError, match="caller observation/objective"):
        _observation(forged)

    expected = {
        "schema": ATTESTATION_SCHEMA,
        "bundle_id": "c" * 64,
        "runtime_attestation_sha256": "d" * 64,
        "profile": "optimizer",
        "environment_id": "e" * 64,
        "environment_hash": "f" * 64,
        "required_versions": {"torch": "x", "botorch": "y", "gpytorch": "z"},
        "cuda_available": True,
        "gpu_name": "test-gpu",
        "gpu_uuid": "GPU-test",
    }
    binding = ledger.read_only_identity()
    binding["refs"] = [
        {
            "attempt_sequence": verified.attempt_sequence,
            "observation_uid": verified.observation_uid,
        }
    ]
    choices = [Candidate(force_p_gain=Candidate().force_p_gain).canonical]
    legacy_payload = {
        "ledger_binding": binding,
        "observations": [forged],
        "choices": choices,
        "pending": [],
        "incumbent": Candidate().canonical,
        "seed": 5005,
    }
    with pytest.raises(OptimizerWorkerError, match="request fields differ"):
        _request(
            {
                "schema": REQUEST_SCHEMA,
                "profile": "optimizer",
                "expected_attestation": expected,
                "payload": legacy_payload,
            }
        )


def test_worker_loads_real_temp_ledger_only_from_child_fresh_records(tmp_path) -> None:
    from step5d_autotune_v4_r005.optimizer_worker import _load_fresh_ledger

    contract = load_contract()
    objective, samples = _raw_fixture(5.25)
    ledger = ObservationLedger(
        tmp_path / "child-cold-read.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    record = ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    binding = ledger.read_only_identity()
    binding["refs"] = [
        {
            "attempt_sequence": record.attempt_sequence,
            "observation_uid": record.observation_uid,
        }
    ]

    loaded = _load_fresh_ledger(binding)
    assert len(loaded) == 1
    assert loaded[0].observation_uid == record.observation_uid
    assert loaded[0].eligible
    assert loaded[0].verified_evidence is not record.verified_evidence


def test_parent_cuda_binding_is_exact_fresh_ledger_order_without_evidence_payload(tmp_path) -> None:
    contract = load_contract()
    objective, samples = _raw_fixture(5.25)
    ledger = ObservationLedger(
        tmp_path / "parent-binding.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    ledger.append(_record(contract, objective, 2, raw_path_samples=samples))
    adapter = V4BoAdapter(ledger=ledger)

    binding = adapter._ledger_binding(ledger.records)
    assert set(binding) == {
        "ledger_path",
        "ledger_byte_sha256",
        "campaign_fingerprint",
        "eoat_sha256",
        "refs",
    }
    assert [ref["attempt_sequence"] for ref in binding["refs"]] == [1, 2]
    assert not any(
        key in binding
        for key in ("observations", "objective", "raw_artifact", "verified_evidence")
    )
    with pytest.raises(OptimizerBindingError, match="ledger refs"):
        adapter._ledger_binding(tuple(reversed(ledger.records)))


def test_worker_rejects_artifact_swap_and_ledger_digest_drift(tmp_path) -> None:
    from step5d_autotune_v4_r005.optimizer_worker import OptimizerWorkerError, _load_fresh_ledger

    contract = load_contract()
    objective, samples = _raw_fixture(5.0)
    ledger = ObservationLedger(
        tmp_path / "child-tamper.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    first = ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    second = ledger.append(_record(contract, objective, 2, raw_path_samples=samples))
    binding = ledger.read_only_identity()
    binding["refs"] = [
        {
            "attempt_sequence": record.attempt_sequence,
            "observation_uid": record.observation_uid,
        }
        for record in ledger.records
    ]
    first_path = ledger.path.parent / ledger.rows[0]["raw_artifact"]["relative_path"]
    second_path = ledger.path.parent / ledger.rows[1]["raw_artifact"]["relative_path"]
    first_bytes, second_bytes = first_path.read_bytes(), second_path.read_bytes()
    first_path.write_bytes(second_bytes)
    second_path.write_bytes(first_bytes)
    with pytest.raises(OptimizerWorkerError, match="fresh ledger|artifact|identity"):
        _load_fresh_ledger(binding)

    # Restore the artifacts, then make the parent ledger bytes differ from its
    # explicitly bound digest.  The worker rejects before any candidate import.
    first_path.write_bytes(first_bytes)
    second_path.write_bytes(second_bytes)
    with ledger.path.open("ab") as handle:
        handle.write(b"\n")
        handle.flush()
    with pytest.raises(OptimizerWorkerError, match="digest"):
        _load_fresh_ledger(binding)


def test_production_cuda_path_requires_bound_ledger_but_offline_injected_path_does_not() -> None:
    from step5d_autotune_v4_r005.optimizer import OptimizerBindingError

    with pytest.raises(OptimizerBindingError, match="bound ObservationLedger"):
        V4BoAdapter()._cuda_qlognei((), (), (), Candidate())


def test_bound_ledger_tell_accepts_only_the_fresh_owned_record_reference(tmp_path) -> None:
    contract = load_contract()
    objective, samples = _raw_fixture(5.0)
    ledger = ObservationLedger(
        tmp_path / "tell-reference.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    record = ledger.append(_record(contract, objective, 1, raw_path_samples=samples))
    V4BoAdapter(ledger=ledger).tell(record)

    copied = replace(record)
    with pytest.raises(OptimizerBindingError, match="exact fresh ledger observation reference"):
        V4BoAdapter(ledger=ledger).tell(copied)


def test_r005_runner_passes_the_same_ledger_to_offline_and_live_cuda_adapters() -> None:
    source = (
        ROOT / "tools" / "run_step5d_autotune_v4_r005.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    bound_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "V4BoAdapter":
            continue
        bound_calls.append(
            any(
                keyword.arg == "ledger"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "ledger"
                for keyword in node.keywords
            )
        )
    assert bound_calls == [True, True]


def test_ledger_append_subprocess_verification_is_linear_and_reads_are_cached(
    tmp_path, monkeypatch
) -> None:
    import step5d_autotune_v4_r005.observations as observations_module

    contract = load_contract()
    objective, samples = _raw_fixture()
    calls = []
    real_run_fresh = observations_module._run_fresh

    def counted_run_fresh(mode, path):
        calls.append((mode, Path(path)))
        return real_run_fresh(mode, path)

    monkeypatch.setattr(observations_module, "_run_fresh", counted_run_fresh)
    ledger = ObservationLedger(
        tmp_path / "linear.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    assert [mode for mode, _path in calls] == ["ledger"]

    ledger.append(_qualification_record(contract, 1))
    ledger.append(_qualification_record(contract, 2))
    for sequence in (3, 4, 5):
        ledger.append(_record(contract, objective, sequence, raw_path_samples=samples))
    assert [mode for mode, _path in calls] == [
        "ledger",
        "artifact",
        "artifact",
        "artifact",
    ]

    call_count = len(calls)
    for _ in range(3):
        assert len(ledger.records) == 5
        assert len(ledger.trainable()) == 3
        assert len(ledger.observed_uids()) == 1
    assert len(calls) == call_count

    ledger.fresh_process_verify_details()
    assert len(calls) == call_count + 1
    assert calls[-1][0] == "ledger"


def test_synthetic_objective_is_not_eligible_for_a_record() -> None:
    contract = load_contract()
    record = _record(contract, synthetic_force_objective(0.1))
    assert not record.eligible
