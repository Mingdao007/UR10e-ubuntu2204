from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from step5d_autotune_v4_r005.alignment import (
    AlignmentError,
    AlignedJointVelocity,
    JointVelocityPacket,
    align_qdot_actual_qd,
)
from step5d_autotune_v4_r005.contracts import (
    Candidate,
    P_ANCHOR,
    PROGRAM,
    R005ContractError,
    bootstrap_pd_candidates,
    full_domain,
    load_contract,
    rebuild_contract,
    validate_transition,
)
from step5d_autotune_v4_r005.fake_rtde import FakeAttemptRuntime, FakeRTDEError, TPResidentLoop
from step5d_autotune_v4_r005.observations import ObservationLedger, ObservationRecord
from step5d_autotune_v4_r005.offline_test_factory import synthetic_force_objective
from step5d_autotune_v4_r005.optimizer import OptimizerError, V4BoAdapter
from step5d_autotune_v4_r005.queue import (
    QueueCapacityError,
    QueueError,
    OfflineDurableQueue,
    V3DurableQueueAdapter,
)
from step5d_autotune_v4_r005.runtime import Attempt, AttemptOutcome, AttemptResult, CampaignPhase, HostLoop
from step5d_autotune_v4_r005.timing import AbsoluteDeadlinePacer, RateGates
from step5d_autotune_v4_r005.tp import ResidentTPModel, render_script
from step5d_autotune_v4_r004.evidence import AttemptEvidence, PathSample, QualificationEvidence
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from step5d_autotune_v4_r004.timing import TimingEvidence
from step5d_force_objective import ForceObjectiveBuilder, ForcePathSample


ROOT = Path(__file__).resolve().parents[1]


def _contract():
    return load_contract()


def _sealed_raw_fixture(mae: float):
    """Build a complete raw-evidence receipt for host-loop tests.

    This is intentionally separate from ``synthetic_force_objective``: tests
    that exercise optimizer eligibility must still cross the same builder,
    per-bin-statistics, and receipt path as production evidence.
    """

    value = float(mae)
    builder = ForceObjectiveBuilder()
    samples = []
    for index in range(600):
        sample = ForcePathSample(
            path_time_s=index / 10.0,
            path_phase=25,
            filtered_normal_n=5.0 + value,
            source_sequences={"packet": index + 1, "rtde": index + 1},
            source_ages_s={"packet": 0.001, "rtde": 0.001},
        )
        builder.add(sample)
        samples.append(sample)
    return builder.finalize(provenance="raw_path_evidence"), tuple(samples)


def test_live_wire_epoch_is_int32_while_host_identity_remains_nanosecond():
    from prepare_step5d_autotune_v4_r005_live import _wire_epoch

    nonce_ns = 1_785_615_153_980_565_968
    assert _wire_epoch(nonce_ns) == 1_785_615_153
    assert _wire_epoch(nonce_ns) <= 2**31 - 1
    with pytest.raises(RuntimeError, match="INT32 epoch"):
        _wire_epoch((2**31) * 1_000_000_000)


def test_prepare_creates_empty_r005_host_state_after_resident_readiness(tmp_path):
    prepare_source = (
        ROOT / "tools" / "prepare_step5d_autotune_v4_r005_live.py"
    ).read_text(encoding="utf-8")
    ledger_path = tmp_path / "r005-observations.jsonl"
    ObservationLedger(
        ledger_path,
        campaign_fingerprint="a" * 64,
        eoat_sha256="b" * 64,
    )
    queue_root = tmp_path / "r005-queue"
    queue_root.mkdir()
    header = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["schema"] == "step5d.autotune-v4/r005-sealed-observations-v1"
    assert header["record_type"] == "header"
    assert ObservationLedger(
        ledger_path,
        campaign_fingerprint="a" * 64,
        eoat_sha256="b" * 64,
    ).rows == ()
    assert queue_root.is_dir()
    assert tuple(queue_root.iterdir()) == ()
    assert 'R005_LEDGER_FILENAME = "r005-observations.jsonl"' in prepare_source
    assert 'R005_QUEUE_DIRNAME = "r005-queue"' in prepare_source
    assert "ObservationLedger(" in prepare_source
    assert "queue_root.mkdir(exist_ok=True)" in prepare_source
    assert "ledger.fresh_process_verify()" in prepare_source
    ready_write = prepare_source.index(
        '        _write_json(run_dir / "resident_ready_evidence.json", ready_evidence)'
    )
    state_init = prepare_source.index(
        "        ledger_path, queue_root = _initialize_r005_run_state("
    )
    assert ready_write < state_init


def _record(contract, sequence: int, *, kind: str = "BOOTSTRAP_PD", candidate: Candidate | None = None, eligible: bool = True, motion_gate: bool | None = None, qualification_passed: bool = False, mae: float = 1.0) -> ObservationRecord:
    objective, samples = _sealed_raw_fixture(mae)
    return ObservationRecord(
        campaign_fingerprint=contract.campaign_fingerprint,
        epoch=1,
        attempt_sequence=sequence,
        kind=kind,
        candidate=candidate or Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=eligible,
        return_gate=True,
        motion_gate=eligible if motion_gate is None else motion_gate,
        timing_gate=eligible,
        identity_gate=True,
        qualification_passed=qualification_passed,
        duration_s=60.0,
        force_objective=objective if eligible else None,
        metrics={"offline": True, "execution_id": f"r005-test-execution-{sequence}"},
        raw_path_samples=samples,
    ).seal()


def test_r005_contract_named_domain_and_transition_bounds():
    contract = _contract()
    assert contract.raw["invariants"]["target_force_n"] == 5.0
    assert contract.raw["invariants"]["anchor_damping"] == 28.0
    assert len(bootstrap_pd_candidates()) == 10
    domain = full_domain()
    assert len(domain) == 22500
    assert {candidate.i_off for candidate in domain} == {True, False}


def test_r005_campaign_fingerprint_binds_derived_r004_parent_digest(tmp_path, monkeypatch):
    import step5d_autotune_v4_r005.contracts as r005_contracts

    base = json.loads(json.dumps(_contract().raw))
    parent_relative = next(iter(base["derived_from_r004"]))
    original_digest = base["derived_from_r004"][parent_relative]
    changed_digest = "0" * 64 if original_digest != "0" * 64 else "1" * 64
    changed = json.loads(json.dumps(base))
    changed["derived_from_r004"][parent_relative] = changed_digest
    base_path = tmp_path / "base-contract.json"
    changed_path = tmp_path / "changed-contract.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    parent_source = (r005_contracts.ROOT / parent_relative).resolve()
    real_sha256_file = r005_contracts.sha256_file

    def load_with_parent_digest(path, document):
        def sha256_file(candidate):
            if Path(candidate).resolve() == parent_source:
                return document["derived_from_r004"][parent_relative]
            return real_sha256_file(candidate)

        with monkeypatch.context() as patch:
            patch.setattr(r005_contracts, "sha256_file", sha256_file)
            return r005_contracts.load_contract(path)

    first = load_with_parent_digest(base_path, base)
    second = load_with_parent_digest(changed_path, changed)
    assert first.campaign_fingerprint == r005_contracts._campaign_fingerprint(first.raw)
    assert second.campaign_fingerprint == r005_contracts._campaign_fingerprint(second.raw)
    assert first.campaign_fingerprint != second.campaign_fingerprint


def test_explicit_rebuild_refreshes_parent_digests_and_converges(tmp_path, monkeypatch):
    import step5d_autotune_v4_r005.contracts as r005_contracts

    source_root = r005_contracts.ROOT
    raw = json.loads(json.dumps(_contract().raw))
    runtime_manifest = json.loads(
        (source_root / raw["runtime_manifest"]["path"]).read_text(encoding="utf-8")
    )
    relative_sources = tuple(raw["derived_from_r004"]) + (
        raw["invariants"]["eoat_profile"]["path"],
        raw["runtime_manifest"]["path"],
        runtime_manifest["launcher_path"],
        *runtime_manifest["source_closure"],
    )
    for relative in dict.fromkeys(relative_sources):
        source = source_root / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    contract_path = tmp_path / "config/step5d/autotune_v4_r005.json"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(r005_contracts, "ROOT", tmp_path)

    first = rebuild_contract(contract_path)
    first_bytes = contract_path.read_bytes()
    second = rebuild_contract(contract_path)
    assert contract_path.read_bytes() == first_bytes
    assert second.sha256 == first.sha256
    assert second.campaign_fingerprint == first.campaign_fingerprint

    drifted_parent = tmp_path / "tools/step5d_autotune_v4_r004/evidence.py"
    drifted_parent.write_bytes(drifted_parent.read_bytes() + b"\n# deterministic drift\n")
    with pytest.raises(R005ContractError, match="parent source drift"):
        load_contract(contract_path)
    repaired = rebuild_contract(contract_path)
    assert repaired.sha256 != first.sha256
    assert repaired.campaign_fingerprint != first.campaign_fingerprint
    assert load_contract(contract_path).sha256 == repaired.sha256


def test_r005_rebuild_identity_bindings_are_converged() -> None:
    contract = _contract()
    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM, contract.sha256, contract.campaign_fingerprint
    )
    expected = {
        "contract_sha256": contract.sha256,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "runtime_identity": {"hi": runtime_hi, "lo": runtime_lo},
    }
    live_writer = json.loads(
        (ROOT / "config/step5d/autotune_v4_live_writer_r005.json").read_text()
    )
    baseline = json.loads(
        (ROOT / "config/step5d/autotune_v4_r005_baseline_ledger_genesis.json").read_text()
    )
    closure = json.loads(
        (ROOT / "config/step5d/autotune_v4_r005_offline_closure.json").read_text()
    )
    numeric = json.loads(
        (
            ROOT
            / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r005.numeric-sanity.json"
        ).read_text()
    )
    manifest = json.loads(
        (
            ROOT
            / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r005.deploy-manifest.json"
        ).read_text()
    )
    assert live_writer["release_binding"] == expected
    assert baseline["release_contract_sha256"] == contract.sha256
    assert baseline["campaign_fingerprint"] == contract.campaign_fingerprint
    assert baseline["runtime_identity"] == expected["runtime_identity"]
    assert closure["release_contract"]["sha256"] == contract.sha256
    assert closure["release_contract"]["campaign_fingerprint"] == contract.campaign_fingerprint
    assert numeric["contract_sha256"] == contract.sha256
    assert numeric["campaign_fingerprint"] == contract.campaign_fingerprint
    assert manifest["release_binding"] == expected
    base = Candidate()
    p_plus = Candidate(force_p_gain=P_ANCHOR * 2.0**0.25)
    assert validate_transition(base, p_plus) == ("force_p_gain",)
    with pytest.raises(Exception, match="more than one"):
        validate_transition(
            base,
            Candidate(
                force_p_gain=P_ANCHOR * 2.0**0.25,
                force_damping=28.0 * 2.0**0.25,
            ),
        )
    with pytest.raises(Exception, match="0.25 octave"):
        validate_transition(base, Candidate(force_p_gain=P_ANCHOR * 2.0**0.5))
    i_on_first = Candidate(force_i_gain=0.00001 * 0.5)
    assert validate_transition(base, i_on_first) == ("force_i_gain",)


def test_sealed_observations_chain_cold_read_and_r004_audit_boundary(tmp_path):
    contract = _contract()
    path = tmp_path / "r005-observations.jsonl"
    ledger = ObservationLedger(
        path,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    ledger.append(_record(contract, 1, eligible=False))
    ledger.append(_record(contract, 2, eligible=True))
    assert len(ledger.records) == 2
    assert len(ledger.trainable()) == 1
    assert ledger.fresh_process_verify()
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["old_r004_ledger_read"] is False
    resumed = ObservationLedger(
        path,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    assert len(resumed.records) == 2
    assert resumed.records[0].sealed is True
    assert resumed.records[0].eligible is False


def test_motion_gate_is_diagnostic_for_optimizer_and_stable_across_cold_read(tmp_path):
    contract = _contract()
    objective, samples = _sealed_raw_fixture(0.1)
    result = AttemptResult(
        epoch=1,
        attempt_sequence=1,
        kind="BOOTSTRAP_PD",
        candidate=Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=False,
        timing_gate=True,
        identity_gate=True,
        duration_s=60.0,
        force_objective=objective,
        execution_id="r005-motion-diagnostic-1",
        joint_evidence=AlignedJointVelocity(
            packet_sequence=1,
            rtde_sequence=1,
            timestamp_s=0.0,
            qdot=(0.1,) * 6,
            actual_qd=(0.1,) * 6,
        ),
        raw_path_samples=samples,
    )
    # AttemptResult objectives are deliberately advisory until the ledger
    # rebuilds them from the immutable raw artifact.  Motion quality must not
    # distinguish the two pre-ledger states or bypass that trust boundary.
    assert not result.eligible
    assert result.outcome is AttemptOutcome.SAFE_NONTRAINABLE
    motion_passing_result = dataclasses.replace(result, motion_gate=True)
    assert not motion_passing_result.eligible
    assert motion_passing_result.outcome is AttemptOutcome.SAFE_NONTRAINABLE

    ledger = ObservationLedger(
        tmp_path / "motion-diagnostic.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    record = ledger.append(result.to_record(contract.campaign_fingerprint))
    for sequence in range(2, 7):
        ledger.append(_record(contract, sequence, motion_gate=False, mae=0.1))

    assert record.sealed
    assert record.motion_gate is False
    assert record.eligible
    assert record.verified_evidence is not None
    assert record.verified_evidence.trainable
    assert ledger.trainable()
    assert ledger.bootstrap_objectives() == pytest.approx((0.1,) * 6)
    v3_record = V4BoAdapter._to_v3_observation(record)
    assert v3_record.evaluation.eligible
    assert v3_record.evaluation.structural_failures == ()

    seen: dict[str, object] = {}

    def ask_impl(v3_observations, choices, _pending, _q, _seed):
        seen["count"] = len(v3_observations)
        seen["all_eligible"] = all(item.evaluation.eligible for item in v3_observations)
        return choices[0], {"motion_diagnostic": True}

    ask = V4BoAdapter(seed=5005, ask_impl=ask_impl).ask(
        observations=ledger.records,
        pending=(),
        incumbent=Candidate(),
    )
    assert ask.candidate != Candidate()
    assert seen == {"count": 6, "all_eligible": True}

    assert ledger.fresh_process_verify()
    resumed = ObservationLedger(
        ledger.path,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="a" * 64,
    )
    assert all(row.motion_gate is False and row.eligible for row in resumed.records)


def test_motion_gate_remains_required_for_final_three_retest_completion(tmp_path):
    loop, ledger, _runtime = _build_loop(tmp_path, motion_gate=False)
    for _ in range(30):
        loop.run_one()
        if "RETEST_FAILED:RETURN_TO_BO" in loop.events:
            break

    retests = tuple(row for row in ledger.records if row.kind == "RETEST")
    assert len(retests) == 3
    assert all(row.eligible and not row.motion_gate for row in retests)
    assert loop.status == "bo"
    assert "RETEST_FAILED:RETURN_TO_BO" in loop.events
    assert "COMPLETE:2_OF_3_AND_MEDIAN_5_PERCENT" not in loop.events


def test_offline_queue_enforces_two_pending_one_inflight_and_durable_cancel(tmp_path):
    queue = OfflineDurableQueue(tmp_path / "queue")
    queue.bind_home(campaign_epoch=1)
    first = queue.enqueue(Candidate(), kind="BO_TRIAL", epoch=1)
    second = queue.enqueue(
        Candidate(force_p_gain=P_ANCHOR * 2.0**0.25), kind="BO_TRIAL", epoch=1
    )
    with pytest.raises(QueueCapacityError):
        queue.enqueue(
            Candidate(force_damping=28.0 * 2.0**0.25), kind="BO_TRIAL", epoch=1
        )
    ticket = queue.prepare_next()
    assert ticket is not None and ticket.request_uid == first.request_uid
    assert queue.prepare_next() == ticket
    cancelled = queue.cancel_pending(reason="threshold_retests")
    assert cancelled == (second,)
    queue.complete(ticket, status="SUCCEEDED")
    assert queue.inflight is None
    assert not queue.pending()
    replay = OfflineDurableQueue(tmp_path / "queue")
    assert replay.inflight is None
    assert not replay.pending()
    assert len(replay.cancelled) == 1


def test_v3_queue_adapter_keeps_cancellation_out_of_next_dispatch(tmp_path):
    queue = V3DurableQueueAdapter(
        tmp_path / "v3-queue",
        campaign_id="r005-v3-adapter-test",
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v4_r005_launch_profile.json",
    )
    queue.bind_home(campaign_epoch=1)
    queue.enqueue(Candidate(), kind="BO_TRIAL", epoch=1)
    queue.enqueue(
        Candidate(force_p_gain=P_ANCHOR * 2.0**0.25), kind="BO_TRIAL", epoch=1
    )
    ticket = queue.prepare_next()
    assert ticket is not None
    dispatch_count_before_cancel = len(list((tmp_path / "v3-queue" / "dispatches").glob("*.json")))
    assert len(queue.cancel_pending(reason="completion_threshold")) == 1
    queue.complete(ticket, status="SUCCEEDED")
    assert queue.prepare_next() is None
    assert not queue.pending()
    assert len(list((tmp_path / "v3-queue" / "dispatches").glob("*.json"))) == dispatch_count_before_cancel


def test_v3_queue_adapter_repeats_qualification_and_retest_identities(tmp_path):
    queue = V3DurableQueueAdapter(
        tmp_path / "v3-repeat-queue",
        campaign_id="r005-v3-repeat-test",
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v4_r005_launch_profile.json",
    )
    queue.bind_home(campaign_epoch=1)
    tickets = []
    for kind in ("QUALIFICATION", "QUALIFICATION", "RETEST", "RETEST"):
        queue.enqueue(Candidate(), kind=kind, epoch=1)
        ticket = queue.prepare_next()
        assert ticket is not None
        tickets.append(ticket)
        queue.complete(ticket, status="SUCCEEDED")
    assert [ticket.dispatch_sequence for ticket in tickets] == [1, 2, 3, 4]
    assert len({ticket.request_uid for ticket in tickets}) == 4
    assert len({ticket.entry.logical_uid for ticket in tickets}) == 4
    assert len(list((tmp_path / "v3-repeat-queue" / "dispatches").glob("*.json"))) == 4


def test_v3_queue_adapter_recovers_after_receipt_before_crash_resume_submit(
    tmp_path, monkeypatch
):
    import step5d_parameter_queue as v3_queue

    queue_root = tmp_path / "v3-crash-before-submit"
    profile = ROOT / "config/step5/step5d_autotune_v4_r005_launch_profile.json"
    queue = V3DurableQueueAdapter(
        queue_root,
        campaign_id="r005-v3-crash-before-submit",
        launch_profile_path=profile,
    )
    queue.bind_home(campaign_epoch=1)
    old = queue.enqueue(Candidate(), kind="QUALIFICATION", epoch=1)
    ticket = queue.prepare_next()
    assert ticket is not None
    queue.record_execution(ticket, attempt_sequence=41, execution_id="sealed-old")

    def fail_submit(*_args, **_kwargs):
        raise RuntimeError("injected submit failure")

    with monkeypatch.context() as patch:
        patch.setattr(v3_queue, "submit_manifest", fail_submit)
        with pytest.raises(QueueError, match=r"phase=requeue_submit.*injected submit failure"):
            queue.reconcile_inflight_after_home()

    resumed = V3DurableQueueAdapter(
        queue_root,
        campaign_id="r005-v3-crash-before-submit",
        launch_profile_path=profile,
    )
    pending = resumed.pending()
    assert len(pending) == 1
    assert pending[0].request_uid != old.request_uid
    assert pending[0].logical_uid == old.logical_uid
    assert pending[0].candidate == old.candidate
    assert resumed.last_attempt_sequence == 41
    requests = v3_queue.list_requests(queue_root)
    assert len(requests) == 2
    assert len(list((queue_root / "receipts").glob("*.json"))) == 1
    assert json.loads(
        next((queue_root / "receipts").glob("*.json")).read_text(encoding="utf-8")
    )["status"] == "FAILED"

    replay = V3DurableQueueAdapter(
        queue_root,
        campaign_id="r005-v3-crash-before-submit",
        launch_profile_path=profile,
    )
    assert [entry.request_uid for entry in replay.pending()] == [pending[0].request_uid]
    assert len(v3_queue.list_requests(queue_root)) == 2
    assert len((queue_root / "r005-metadata.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_v3_queue_adapter_repairs_metadata_after_crash_resume_submit(
    tmp_path, monkeypatch
):
    import step5d_parameter_queue as v3_queue

    queue_root = tmp_path / "v3-crash-after-submit"
    profile = ROOT / "config/step5/step5d_autotune_v4_r005_launch_profile.json"
    queue = V3DurableQueueAdapter(
        queue_root,
        campaign_id="r005-v3-crash-after-submit",
        launch_profile_path=profile,
    )
    queue.bind_home(campaign_epoch=1)
    old = queue.enqueue(Candidate(), kind="QUALIFICATION", epoch=1)
    ticket = queue.prepare_next()
    assert ticket is not None

    original_append = queue._append

    def fail_metadata_append(path, payload):
        if path == queue._metadata_path:
            raise OSError("injected metadata append failure")
        return original_append(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(queue, "_append", fail_metadata_append)
        with pytest.raises(QueueError, match=r"phase=metadata_append.*injected metadata append failure"):
            queue.reconcile_inflight_after_home()

    def forbidden_submit(*_args, **_kwargs):
        raise AssertionError("metadata-only repair must not resubmit transport")

    with monkeypatch.context() as patch:
        patch.setattr(v3_queue, "submit_manifest", forbidden_submit)
        repaired = V3DurableQueueAdapter(
            queue_root,
            campaign_id="r005-v3-crash-after-submit",
            launch_profile_path=profile,
        )
    pending = repaired.pending()
    assert len(pending) == 1
    assert pending[0].request_uid != old.request_uid
    assert pending[0].logical_uid == old.logical_uid
    assert pending[0].candidate == old.candidate
    assert len(v3_queue.list_requests(queue_root)) == 2

    replay = V3DurableQueueAdapter(
        queue_root,
        campaign_id="r005-v3-crash-after-submit",
        launch_profile_path=profile,
    )
    assert [entry.request_uid for entry in replay.pending()] == [pending[0].request_uid]
    assert len(v3_queue.list_requests(queue_root)) == 2
    assert len((queue_root / "r005-metadata.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_v4_adapter_passes_x_pending_and_has_no_fallback(tmp_path):
    contract = _contract()
    ledger = ObservationLedger(
        tmp_path / "x-pending-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="e" * 64,
    )
    for index in range(1, 7):
        ledger.append(_record(contract, index))
    observations = ledger.records
    pending = (Candidate(force_p_gain=P_ANCHOR * 2.0**0.25),)
    seen: dict[str, int] = {}

    def ask_impl(v3_observations, choices, v3_pending, q, seed):
        seen.update(
            observations=len(v3_observations), pending=len(v3_pending), q=q, seed=seed
        )
        assert choices
        return choices[0], {"injected": True}

    adapter = V4BoAdapter(seed=5050, ask_impl=ask_impl)
    result = adapter.ask(observations=observations, pending=pending, incumbent=Candidate())
    assert result.candidate.candidate_uid not in {row.candidate_uid for row in observations}
    assert result.candidate.candidate_uid != pending[0].candidate_uid
    assert seen == {"observations": 6, "pending": 1, "q": 1, "seed": 5050}
    assert result.metadata["pending_count"] == 1

    def broken(*_):
        raise RuntimeError("backend unavailable")

    with pytest.raises(OptimizerError, match="no fallback"):
        V4BoAdapter(ask_impl=broken).ask(
            observations=observations,
            pending=(),
            incumbent=Candidate(),
        )


def test_host_refill_transitions_from_pending_tail_not_physical_cursor(tmp_path):
    contract = _contract()
    observations = ObservationLedger(
        tmp_path / "pending-tail-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="f" * 64,
    )
    for sequence in range(1, 7):
        observations.append(_record(contract, sequence, candidate=Candidate(), mae=1.0))
    queue = OfflineDurableQueue(tmp_path / "pending-tail-queue")
    queue.bind_home(campaign_epoch=1)
    seen_pending: list[int] = []
    candidate2 = Candidate(force_p_gain=P_ANCHOR * 2.0**0.25)
    candidate3 = Candidate(force_p_gain=P_ANCHOR * 2.0**0.5)

    def ask_impl(_observations, choices, pending, _q, _seed):
        seen_pending.append(len(pending))
        desired = candidate2 if not pending else candidate3
        desired_v3 = V4BoAdapter._to_v3(desired)
        assert desired_v3 in choices
        return desired_v3, {"pending": len(pending)}

    loop = HostLoop(
        contract=contract,
        queue=queue,
        ledger=observations,
        optimizer=V4BoAdapter(ask_impl=ask_impl),
        runtime=FakeAttemptRuntime(
            result_factory=lambda attempt, timing: _integration_result(attempt, timing)
        ),
    )
    loop.start_epoch()
    loop.phase = CampaignPhase.BO
    loop._refill()
    pending = queue.pending()
    assert [entry.candidate for entry in pending] == [candidate2, candidate3]
    assert seen_pending == [0, 1]
    with pytest.raises(Exception):
        validate_transition(Candidate(), candidate3)
    assert validate_transition(candidate2, candidate3) == ("force_p_gain",)


def test_production_shaped_injected_writer_runs_two_consecutive_arms_after_four(tmp_path):
    from step5d_autotune_v4_r005.live_adapter import (
        R005LiveAdapter,
        R005LiveWriterAdapter,
        R005MatureWriter,
        R005_LIVE_ACK,
    )

    contract = _contract()
    ledger = ObservationLedger(
        tmp_path / "injected-live-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="1" * 64,
    )
    for sequence in range(10, 16):
        ledger.append(_record(contract, sequence, kind="BO_TRIAL", mae=1.0))
    queue = OfflineDurableQueue(tmp_path / "injected-live-queue")
    queue.bind_home(campaign_epoch=1)

    candidate1 = Candidate(force_p_gain=P_ANCHOR * 2.0**0.25)
    candidate2 = Candidate(force_p_gain=P_ANCHOR * 2.0**0.5)
    candidate3 = Candidate(force_p_gain=P_ANCHOR * 2.0**0.75)
    candidate4 = Candidate(
        force_p_gain=P_ANCHOR * 2.0**0.75,
        force_damping=28.0 * 2.0**-0.25,
    )

    class InjectedWriter:
        offline_test_mode = True

        def __init__(self):
            self.open_count = 0
            self.events: list[str] = []
            self.arm_sequences: list[int] = []
            self._ask_index = 0
            self._path_sample_sink = None

        def open(self, *, live_ack):
            assert live_ack == R005_LIVE_ACK
            self.open_count += 1
            self.events.append("OPEN")

        def close(self):
            self.events.append("CLOSE")

        def home(self):
            self.events.append("HOME")

        def sync_qualification_passes(self, count):
            self.events.append(f"QUALIFICATION_SYNC:{count}")

        def dispatch(self, attempt, ticket):
            del ticket
            self.events.append(f"DISPATCH:{attempt.attempt_sequence}")

        def arm(self, attempt):
            self.arm_sequences.append(attempt.attempt_sequence)
            self.events.append(f"ARM:{attempt.attempt_sequence}")

        def run_60s(self, attempt):
            self.events.append(f"RUN:{attempt.attempt_sequence}")
            assert self._path_sample_sink is not None
            # Bind the optimizer value from raw PATH packets.  The first five
            # seconds differ only to make the audit-only legacy shadow
            # observably different from formal [5,60) v2.
            for index in range(600):
                path_time_s = 0.05 + index * 0.1
                sequence = index + 1
                self._path_sample_sink(
                    PathSample(
                        observed_at_s=100.0 + path_time_s,
                        filtered_normal_n=6.0 if path_time_s < 5.0 else 5.0,
                        force_norm_n=6.0,
                        torque_norm_nm=0.01,
                        sensor_fresh=True,
                        state=25,
                        safety_normal=True,
                        path_time_s=path_time_s,
                        path_phase=6,
                        desired_xy_m=(0.0, 0.0),
                        actual_xy_m=(0.0, 0.0),
                        desired_velocity_m_s=(0.0, 0.0),
                        actual_velocity_m_s=(0.0, 0.0),
                        qdot=(0.1,) * 6,
                        actual_qd=(0.1,) * 6,
                        source_ages_s={
                            "writer": 0.001,
                            "rtde": 0.001,
                            "kunwei": 0.001,
                            "tp": 0.001,
                        },
                        source_sequences={
                            "writer": sequence,
                            "rtde": sequence,
                            "kunwei": sequence,
                            "tp": sequence,
                        },
                        qd_lag_s=0.001,
                    )
                )
            return AttemptEvidence(
                complete_bins=550,
                effective_rate_hz=500.0,
                p99_packet_interval_s=0.002,
                max_packet_interval_s=0.002,
                mae_n=99.0,
                objective=99.0,
                safety_gate_passed=True,
                contact_gate_passed=True,
                return_gate_passed=True,
                home_proof={"injected": True},
                path_samples=600,
                path_bin_ids=tuple(range(550)),
                evidence_sha256="b" * 64,
                path_duration_s=60.0,
                path_phase=6,
                xy_error_p95_m=0.0,
                xy_error_max_m=0.0,
                endpoint_error_max_m=0.0,
                qd_correlation=1.0,
                qd_lag_s=0.001,
                timing_evidence=TimingEvidence(
                    duration_s=60.0,
                    successful_writer_publishes=30000,
                    distinct_rtde_frames=30000,
                    distinct_kunwei_frames=30000,
                    distinct_tp_consumed_packet_echoes=30000,
                    feedback_age_p99_s=0.001,
                    max_fresh_gap_s=0.002,
                ),
                metrics={"injected_transport": True},
            )

        def safe_return(self, attempt, result):
            self.events.append(f"RETURN:{attempt.attempt_sequence}")
            return result

        def revoke_authority(self, reason):
            self.events.append(f"REVOKE:{reason}")

    writer = InjectedWriter()
    asks = [candidate1, candidate2, candidate3, candidate4]

    def ask_impl(_observations, choices, _pending, _q, _seed):
        desired = asks.pop(0)
        desired_v3 = V4BoAdapter._to_v3(desired)
        assert desired_v3 in choices
        return desired_v3, {"injected_transport": True}

    adapter = R005LiveAdapter()
    loop = adapter.build_host_loop(
        queue=queue,
        ledger=ledger,
        optimizer=V4BoAdapter(ask_impl=ask_impl),
        writer=writer,
    )
    loop.phase = CampaignPhase.BO
    loop.epoch = 1
    loop.next_attempt_sequence = 4
    assert adapter._writer is not None
    adapter._writer.open(live_ack=R005_LIVE_ACK)
    loop.run_one()
    loop.run_one()

    assert writer.open_count == 1
    assert writer.arm_sequences == [4, 5], (loop.events, loop.stop_reason, queue.pending())
    assert [row.attempt_sequence for row in ledger.records[-2:]] == [4, 5]
    assert "RUN:4" in writer.events and "RETURN:4" in writer.events
    assert "RUN:5" in writer.events and "RETURN:5" in writer.events
    assert all(row.force_objective is not None for row in ledger.records[-2:])
    assert all(row.force_objective.v2_mae_n == 0.0 for row in ledger.records[-2:])
    assert all(row.force_objective.legacy_mae_n == pytest.approx(50.0 / 550.0) for row in ledger.records[-2:])
    assert all(row.force_objective.sample_count == 600 for row in ledger.records[-2:])
    assert R005LiveWriterAdapter(writer, contract=contract).open_count == 0
    assert R005MatureWriter._kind("BOOTSTRAP_PD").name == "BATCH_A"
    assert R005MatureWriter._kind("BO_TRIAL").name == "BATCH_B"


def test_qualification_adapter_preserves_execution_identity():
    from step5d_autotune_v4_r005.live_adapter import R005LiveWriterAdapter

    timing = TimingEvidence(
        duration_s=1.0,
        successful_writer_publishes=500,
        distinct_rtde_frames=500,
        distinct_kunwei_frames=500,
        distinct_tp_consumed_packet_echoes=500,
        feedback_age_p99_s=0.001,
        max_fresh_gap_s=0.002,
    )

    class QualificationWriter:
        def run_60s(self, _attempt):
            return QualificationEvidence(
                qualification_passed=True,
                effective_rate_hz=500.0,
                p99_packet_interval_s=0.002,
                max_packet_interval_s=0.002,
                safety_gate_passed=True,
                contact_gate_passed=True,
                return_gate_passed=True,
                fresh_sensor_gate_passed=True,
                sample_count=500,
                observed_states=(20, 21, 78),
                home_proof={"stationary": True},
                evidence_sha256="a" * 64,
                timing_evidence=timing,
            )

    attempt = Attempt(
        epoch=1,
        attempt_sequence=1,
        candidate=Candidate(),
        kind="QUALIFICATION",
        dispatch_sequence=1,
        request_uid="request-1",
        execution_id="r005-execution-1",
    )
    result = R005LiveWriterAdapter(
        QualificationWriter(), contract=_contract()
    ).run_60s(attempt)
    assert result.execution_id == attempt.execution_id


def test_tp_accepts_unbounded_positive_monotonic_sequences_and_stays_resident():
    model = ResidentTPModel()
    for sequence in range(1, 17):
        model.arm(sequence)
        model.safe_return()
    model.arm(17)
    model.safe_return()
    assert model.last_attempt_sequence == 17
    assert model.state == "WAITING_ARM"
    with pytest.raises(Exception, match="increase"):
        model.arm(17)
    fake = TPResidentLoop()
    with pytest.raises(FakeRTDEError, match="positive"):
        fake.accept_arm(epoch=1, attempt_sequence=0)


def test_generated_urscript_epoch_and_complete_identity_are_simulated():
    script = render_script()
    arm_guard = next(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("if input_epoch <= last_failed_epoch")
    )
    expression = arm_guard[3:-1]  # remove leading ``if `` and trailing ``:``
    first_arm_rejected = eval(
        expression,
        {},
        {
            "input_epoch": 7,
            "last_failed_epoch": 0,
            "active_epoch": 0,
            "input_ordinal": 4,
            "current_ordinal": 0,
            "input_kind": 2,
            "input_token": 99,
        },
    )
    later_epoch_rejected = eval(
        expression,
        {},
        {
            "input_epoch": 8,
            "last_failed_epoch": 0,
            "active_epoch": 7,
            "input_ordinal": 5,
            "current_ordinal": 4,
            "input_kind": 2,
            "input_token": 100,
        },
    )
    assert first_arm_rejected is False
    assert later_epoch_rejected is True
    assert "input_epoch == active_epoch and input_ordinal == current_ordinal" in script
    assert "input_token == current_token and input_kind == current_kind" in script

    model = ResidentTPModel()
    model.arm(4, epoch=7, candidate_token=99, attempt_kind=2, session_command_sequence=10)
    model.safe_return()
    with pytest.raises(Exception, match="epoch"):
        model.arm(5, epoch=8, candidate_token=100, attempt_kind=2, session_command_sequence=11)
    model.complete(
        session_command_sequence=11,
        epoch=7,
        attempt_sequence=4,
        candidate_token=99,
        attempt_kind=2,
    )
    assert model.state == "COMPLETE"


def test_qdot_actual_qd_common_packet_rtde_and_time_binding():
    qdot = JointVelocityPacket(10, 20, 1.0, (1.0, 2.0))
    actual = JointVelocityPacket(10, 20, 1.0005, (0.9, 1.8))
    aligned = __import__(
        "step5d_autotune_v4_r005.alignment", fromlist=["align_qdot_actual_qd"]
    ).align_qdot_actual_qd(qdot, actual)
    assert aligned.packet_sequence == 10
    with pytest.raises(AlignmentError, match="packet sequence"):
        align_qdot_actual_qd(qdot, JointVelocityPacket(11, 20, 1.0, (1.0, 2.0)))
    with pytest.raises(AlignmentError, match="time binding"):
        align_qdot_actual_qd(qdot, JointVelocityPacket(10, 20, 1.01, (1.0, 2.0)))


def test_absolute_deadline_and_four_distinct_rate_gates():
    pacer = AbsoluteDeadlinePacer(start_monotonic=0.0)
    assert pacer.tick(now=0.0) == 0.0
    assert pacer.tick(now=0.002) == 0.002
    assert pacer.missed_deadlines == 0
    gates = RateGates()
    for name in ("writer", "rtde", "kunwei", "tp"):
        gates.record(name, 0.0)
        gates.record(name, 0.002)
    evidence = gates.evidence()
    assert set(evidence.rates) == {"writer", "rtde", "kunwei", "tp"}
    assert all(rate == pytest.approx(500.0) for rate in evidence.rates.values())
    assert evidence.passes


def _integration_result(attempt, timing, *, failed_retest: bool = False, motion_gate: bool = True):
    good = attempt.kind in {"BO_TRIAL", "RETEST"}
    mae = 0.3 if failed_retest and attempt.kind == "RETEST" else (0.1 if good else 1.0)
    objective, samples = _sealed_raw_fixture(mae)
    return AttemptResult(
        epoch=attempt.epoch,
        attempt_sequence=attempt.attempt_sequence,
        kind=attempt.kind,
        candidate=attempt.candidate,
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=motion_gate,
        timing_gate=timing.passes,
        identity_gate=True,
        qualification_passed=attempt.kind == "QUALIFICATION",
        duration_s=60.0,
        force_objective=None if attempt.kind == "QUALIFICATION" else objective,
        metrics={"fake_rtde": True},
        raw_path_samples=() if attempt.kind == "QUALIFICATION" else samples,
    )


def _build_loop(tmp_path, *, failed_retest: bool = False, motion_gate: bool = True):
    contract = _contract()

    def ask_impl(_observations, choices, pending, _q, _seed):
        return choices[0], {"injected": True, "pending_seen": len(pending)}

    runtime = FakeAttemptRuntime(
        result_factory=lambda attempt, timing: _integration_result(
            attempt, timing, failed_retest=failed_retest, motion_gate=motion_gate
        )
    )
    ledger = ObservationLedger(
        tmp_path / "observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="b" * 64,
    )
    loop = HostLoop(
        contract=contract,
        queue=OfflineDurableQueue(tmp_path / "queue"),
        ledger=ledger,
        optimizer=V4BoAdapter(ask_impl=ask_impl),
        runtime=runtime,
    )
    return loop, ledger, runtime


def test_host_loop_completion_and_fake_rtde_no_live_side_effects(tmp_path):
    loop, ledger, runtime = _build_loop(tmp_path)
    for _ in range(30):
        if loop.terminal:
            break
        loop.run_one()
    assert loop.status == "complete"
    assert [row.kind for row in ledger.records].count("QUALIFICATION") == 3
    assert [row.kind for row in ledger.records].count("BOOTSTRAP_PD") == 10
    assert [row.kind for row in ledger.records].count("BO_TRIAL") == 1
    assert [row.kind for row in ledger.records].count("RETEST") == 3
    assert "RETEST_START:THREE_INCUMBENT_RETESTS" in loop.events
    assert "COMPLETE:2_OF_3_AND_MEDIAN_5_PERCENT" in loop.events
    assert runtime.rtde.hardware_calls == 0
    assert runtime.rtde.tp.state == "WAITING_ARM"
    assert runtime.rtde.tp.last_attempt_sequence == 17
    binding = ledger.records[0].metrics["joint_velocity_binding"]
    assert set(binding) == {
        "packet_sequence",
        "rtde_sequence",
        "timestamp_s",
        "commanded_qdot",
        "actual_qd",
    }
    assert binding["packet_sequence"] == 1
    assert binding["rtde_sequence"] == 1


def test_qualification_wire_count_syncs_only_from_sealed_host_state(tmp_path):
    loop, _ledger, runtime = _build_loop(tmp_path)
    synced: list[int] = []
    runtime.sync_qualification_passes = synced.append
    loop._sync_qualification_state()
    loop.run_one()
    loop.run_one()
    loop.run_one()
    assert synced == [0, 1, 2, 3]
    assert loop.phase is CampaignPhase.BOOTSTRAP


def test_resume_uses_fresh_process_cold_read_and_keeps_r005_epoch(tmp_path):
    loop, ledger, _ = _build_loop(tmp_path)
    loop.run_one()
    assert loop.epoch == 1
    assert ledger.fresh_process_verify()
    resumed_runtime = FakeAttemptRuntime(
        result_factory=lambda attempt, timing: _integration_result(attempt, timing)
    )
    resumed = HostLoop(
        contract=_contract(),
        queue=OfflineDurableQueue(tmp_path / "queue"),
        ledger=ObservationLedger(
            tmp_path / "observations.jsonl",
            campaign_fingerprint=_contract().campaign_fingerprint,
            eoat_sha256="b" * 64,
        ),
        optimizer=V4BoAdapter(
            ask_impl=lambda _observations, choices, _pending, _q, _seed: (
                choices[0],
                {"resumed": True},
            )
        ),
        runtime=resumed_runtime,
    )
    assert "RESUME_COLD_READ_VERIFIED" in resumed.events
    assert resumed.epoch == 1
    assert resumed.next_attempt_sequence == 2
    resumed.run_one()
    assert resumed.status == "qualification"


def test_crash_resume_reuses_logical_identity_with_new_execution_id(tmp_path):
    queue_root = tmp_path / "crash-queue"
    queue = OfflineDurableQueue(queue_root)
    queue.bind_home(campaign_epoch=1)
    original = queue.enqueue(Candidate(), kind="QUALIFICATION", epoch=1)
    old_ticket = queue.prepare_next()
    assert old_ticket is not None
    queue.record_execution(
        old_ticket,
        attempt_sequence=41,
        execution_id="r005-crashed-execution",
    )
    contract = _contract()
    ledger = ObservationLedger(
        tmp_path / "crash-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="e" * 64,
    )
    runtime = FakeAttemptRuntime(
        result_factory=lambda attempt, timing: _integration_result(attempt, timing)
    )
    resumed = HostLoop(
        contract=contract,
        queue=OfflineDurableQueue(queue_root),
        ledger=ledger,
        optimizer=V4BoAdapter(ask_impl=lambda *_: (_ for _ in ()).throw(RuntimeError("unused"))),
        runtime=runtime,
    )
    assert resumed.next_attempt_sequence == 42
    result = resumed.run_one()
    assert result is not None
    assert resumed.status == "qualification"
    assert "RESUME_HOME_RECONCILED:" + original.logical_uid in resumed.events
    assert resumed.queue.completed[-1].entry.logical_uid == original.logical_uid
    assert resumed.queue.completed[-1].dispatch_sequence == 2
    assert resumed.queue.last_attempt_sequence == 42
    assert result.execution_id != "r005-crashed-execution"
    assert result.execution_id
    assert runtime.attempts[-1].logical_identity == original.logical_uid


def test_cold_resume_revalidates_a_real_raw_artifact_before_optimizer_state(tmp_path):
    loop, ledger, _ = _build_loop(tmp_path)
    # Three qualification rows are intentionally artifact-free; the fourth
    # row is the first real PATH observation and must survive a fresh ledger
    # reconstruction before the resumed host can proceed.
    for _ in range(4):
        loop.run_one()
    rows = ledger.records
    assert rows[3].raw_artifact is not None
    assert rows[3].eligible
    contract = _contract()
    resumed = HostLoop(
        contract=contract,
        queue=OfflineDurableQueue(tmp_path / "queue"),
        ledger=ObservationLedger(
            tmp_path / "observations.jsonl",
            campaign_fingerprint=contract.campaign_fingerprint,
            eoat_sha256="b" * 64,
        ),
        optimizer=V4BoAdapter(ask_impl=lambda *_: (_ for _ in ()).throw(RuntimeError("unused"))),
        runtime=FakeAttemptRuntime(
            result_factory=lambda attempt, timing: _integration_result(attempt, timing)
        ),
    )
    assert "RESUME_COLD_READ_VERIFIED" in resumed.events
    assert resumed.next_attempt_sequence == 5
    assert resumed.ledger.records[3].objective == pytest.approx(1.0)


def test_attempt_outcome_enum_and_safety_failure_revoke_before_safe_return(tmp_path):
    assert tuple(AttemptOutcome.__members__) == (
        "OBJECTIVE",
        "SAFE_NONTRAINABLE",
        "CODE_OR_EVIDENCE_BUG",
        "SAFETY_OR_RETURN_FAILURE",
    )

    def safety_failure(attempt, timing):
        return dataclasses.replace(
            _integration_result(attempt, timing),
            safety_gate=False,
        )

    loop, ledger, runtime = _build_loop(tmp_path)
    runtime.result_factory = safety_failure
    result = loop.run_one()
    assert result is not None
    assert result.outcome is AttemptOutcome.SAFETY_OR_RETURN_FAILURE
    assert loop.status == "incomplete_stopped"
    assert runtime.authority_revoked
    assert runtime.rtde.safe_return_calls == 0
    assert runtime.rtde.tp.state == "REVOKED"
    assert not ledger.records


def test_failed_retests_return_to_bo_without_trial_limit(tmp_path):
    loop, ledger, _ = _build_loop(tmp_path, failed_retest=True)
    for _ in range(30):
        if loop.phase.value == "bo" and len(ledger.records) >= 17:
            break
        loop.run_one()
    assert loop.status == "bo"
    assert "RETEST_FAILED:RETURN_TO_BO" in loop.events
    assert loop.stop_reason is None


def test_safe_nontrainable_continues_but_binding_fault_revokes(tmp_path):
    contract = _contract()
    calls = {"count": 0}

    def nontrainable(attempt, timing):
        calls["count"] += 1
        return AttemptResult(
            epoch=attempt.epoch,
            attempt_sequence=attempt.attempt_sequence,
            kind=attempt.kind,
            candidate=attempt.candidate,
            safe_return=True,
            binding_ok=True,
            safety_gate=True,
            contact_gate=False,
            return_gate=True,
            motion_gate=False,
            timing_gate=timing.passes,
            identity_gate=True,
            duration_s=60.0,
        )

    ledger = ObservationLedger(
        tmp_path / "safe.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="c" * 64,
    )
    runtime = FakeAttemptRuntime(result_factory=nontrainable)
    loop = HostLoop(
        contract=contract,
        queue=OfflineDurableQueue(tmp_path / "safe-queue"),
        ledger=ledger,
        optimizer=V4BoAdapter(ask_impl=lambda *_: (_ for _ in ()).throw(RuntimeError("unused"))),
        runtime=runtime,
    )
    loop.run_one()
    loop.run_one()
    assert len(ledger.records) == 2
    assert not ledger.records[0].eligible
    assert loop.stop_reason is None
    assert not runtime.authority_revoked
    assert runtime.rtde.safe_return_calls == 2

    def mismatched(attempt, timing):
        del timing
        return AttemptResult(
            epoch=attempt.epoch,
            attempt_sequence=attempt.attempt_sequence,
            kind=attempt.kind,
            candidate=Candidate(force_p_gain=P_ANCHOR * 2.0**0.25),
            safe_return=True,
            binding_ok=True,
            safety_gate=True,
            contact_gate=True,
            return_gate=True,
            motion_gate=True,
            timing_gate=True,
            identity_gate=True,
            duration_s=60.0,
            force_objective=synthetic_force_objective(1.0),
        )

    runtime2 = FakeAttemptRuntime(result_factory=mismatched)
    ledger2 = ObservationLedger(
        tmp_path / "binding.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="d" * 64,
    )
    loop2 = HostLoop(
        contract=contract,
        queue=OfflineDurableQueue(tmp_path / "binding-queue"),
        ledger=ledger2,
        optimizer=V4BoAdapter(ask_impl=lambda *_: (_ for _ in ()).throw(RuntimeError("unused"))),
        runtime=runtime2,
    )
    loop2.run_one()
    assert loop2.status == "incomplete_stopped"
    assert runtime2.authority_revoked
    assert "identity" in (loop2.stop_reason or "") or "fault" in (loop2.stop_reason or "")


def test_r005_tp_package_and_cli_boundaries():
    script = render_script()
    contract = _contract()
    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM, contract.sha256, contract.campaign_fingerprint
    )
    assert "# TP_PROGRAM_ID: step5d_strict_rnn_autotune_v4_r005" in script
    assert f"local runtime_hi = {runtime_hi}" in script
    assert f"local runtime_lo = {runtime_lo}" in script
    assert "ordinal > 16" not in script
    assert "input_ordinal <= current_ordinal" in script
    assert "R005_RATE_CONTRACT: 500 Hz" in script
    from build_step5d_autotune_v4_r005 import numeric_sanity, validate_urscript_block_balance

    validate_urscript_block_balance(script)
    assert numeric_sanity(script)["passed"] is True
    delivery_manifest = json.loads(
        (
            ROOT
            / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r005.deploy-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert set(delivery_manifest) == {
        "schema_version",
        "basename",
        "controller_directory",
        "release_binding",
        "artifacts",
    }
    from run_step5d_autotune_v4_r005 import build_parser

    parser = build_parser()
    args = parser.parse_args(["offline", "--root", "/tmp/r005-test", "--stop-after-ordinal", "1"])
    assert args.stop_after_ordinal == 1
    with pytest.raises(SystemExit):
        parser.parse_args(["live", "--stop-after-ordinal", "1"])


def test_offline_main_ordinal4_seals_raw_path_evidence(tmp_path, capsys):
    """Exercise the public CLI seam, not only a directly constructed HostLoop."""

    from run_step5d_autotune_v4_r005 import main

    root = tmp_path / "ordinal4"
    result = main(
        [
            "offline",
            "--root",
            str(root),
            "--stop-after-ordinal",
            "4",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload == {
        "attempts": [1, 2, 3, 4],
        "hardware_calls": 0,
        "live_evidence": False,
        "records": 4,
        "status": "incomplete_stopped",
        "stop_reason": "offline_diagnostic_limit",
    }

    contract = _contract()
    ledger = ObservationLedger(
        root / "r005-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="0" * 64,
    )
    row = ledger.records[-1]
    assert row.kind == "BOOTSTRAP_PD"
    assert row.eligible
    assert row.objective == pytest.approx(0.25)
    assert row.raw_artifact is not None
    assert row.raw_artifact.sample_count == 600
    assert "raw_path_samples" not in row.metrics
    assert "caller force objective is not raw-artifact bound" not in (ledger.path.read_text())
    artifact_path = root / row.raw_artifact.relative_path
    assert artifact_path.is_file()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert len(artifact["samples"]) == 600
    assert artifact["samples"][50]["path_time_s"] == pytest.approx(5.05)
    assert artifact["samples"][-1]["path_time_s"] == pytest.approx(59.95)


def test_offline_main_fake_rtde_reaches_complete_without_hardware(tmp_path, capsys):
    from run_step5d_autotune_v4_r005 import main

    root = tmp_path / "complete"
    result = main(["offline", "--root", str(root)])
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "complete"
    assert payload["attempts"] == list(range(1, 19))
    assert payload["records"] == 18
    assert payload["hardware_calls"] == 0
    assert payload["live_evidence"] is False
    assert payload["stop_reason"] is None

    contract = _contract()
    ledger = ObservationLedger(
        root / "r005-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="0" * 64,
    )
    safe_rows = [
        row
        for row in ledger.records
        if row.kind == "BO_TRIAL" and not row.eligible
    ]
    assert len(safe_rows) == 1
    assert safe_rows[0].raw_artifact is not None
    assert safe_rows[0].contact_gate is False
    assert len([row for row in ledger.records if row.eligible]) == 14
    assert [row.kind for row in ledger.records[-3:]] == ["RETEST"] * 3


def test_r005_live_adapter_composes_verified_r004_stack_without_opening_it():
    from step5d_autotune_v4_r005.live_adapter import R005LiveAdapter

    adapter = R005LiveAdapter()
    descriptor = adapter.descriptor()
    assert descriptor["adapter"] == "r005_host_loop_over_verified_r004_live_stack"
    assert descriptor["production_live_stop_after_ordinal"] is False
    rtde, kunwei = adapter.build_transport_pair(
        controller_host="controller.invalid",
        kunwei_host="sensor.invalid",
        kunwei_port=9000,
    )
    assert rtde.client is None
    assert kunwei._delegate is None
    mature_source = (
        ROOT / "tools/step5d_autotune_v4_r005/live_adapter.py"
    ).read_text(encoding="utf-8")
    assert "allow_safe_nontrainable=True" in mature_source


def test_live_adapter_normalizes_rtde_frame_and_binds_consumed_packet_echo():
    from step5d_autotune_v4_r005.live_adapter import R005LiveAdapter

    class Writer:
        _path_sample_sink = None

    writer = Writer()
    adapted = R005LiveAdapter().adapt_verified_writer(writer)
    adapted.observe_r004_path_sample(
        PathSample(
            observed_at_s=100.0,
            filtered_normal_n=5.0,
            force_norm_n=5.0,
            torque_norm_nm=0.01,
            sensor_fresh=True,
            state=25,
            safety_normal=True,
            path_time_s=5.05,
            path_phase=6,
            desired_xy_m=(0.0, 0.0),
            actual_xy_m=(0.0, 0.0),
            desired_velocity_m_s=(0.0, 0.0),
            actual_velocity_m_s=(0.0, 0.0),
            qdot=(0.1,) * 6,
            actual_qd=(0.09,) * 6,
            source_ages_s={"writer": 0.0, "rtde": 0.0, "kunwei": 0.0, "tp": 0.001},
            source_sequences={"writer": 12, "rtde": 43790.123, "kunwei": 20, "tp": 11},
            qd_lag_s=0.001,
        )
    )
    assert adapted._path_samples[0].source_sequences["rtde"] == 1
    assert adapted._last_joint_evidence is not None
    assert adapted._last_joint_evidence.packet_sequence == 11
    assert adapted._last_joint_evidence.rtde_sequence == 1


def test_live_adapter_reports_canonical_source_age_diagnostic_before_objective_sample():
    from step5d_autotune_v4_r005.live_adapter import (
        MAX_SOURCE_AGE_S,
        R005LiveAdapter,
        R005LiveAdapterError,
    )

    class Writer:
        _path_sample_sink = None

    adapted = R005LiveAdapter().adapt_verified_writer(Writer())
    with pytest.raises(R005LiveAdapterError) as caught:
        adapted.observe_r004_path_sample(
            PathSample(
                observed_at_s=100.0,
                filtered_normal_n=5.0,
                force_norm_n=5.0,
                torque_norm_nm=0.01,
                sensor_fresh=True,
                state=25,
                safety_normal=True,
                path_time_s=5.05,
                path_phase=6,
                desired_xy_m=(0.0, 0.0),
                actual_xy_m=(0.0, 0.0),
                desired_velocity_m_s=(0.0, 0.0),
                actual_velocity_m_s=(0.0, 0.0),
                qdot=(0.1,) * 6,
                actual_qd=(0.09,) * 6,
                source_ages_s={
                    "writer": 0.001,
                    "rtde": 0.002,
                    "kunwei": 0.003,
                    "tp": 0.081,
                },
                source_sequences={
                    "writer": 12,
                    "rtde": 43790.123,
                    "kunwei": 20,
                    "tp": 11,
                },
                qd_lag_s=0.001,
            )
        )
    assert MAX_SOURCE_AGE_S == 0.080
    expected = {
        "adapter_last_rtde_identity": 43790.123,
        "adapter_normalized_rtde_sequence": 1,
        "limit": 0.080,
        "observed_at_s": 100.0,
        "offending_numeric_age": 0.081,
        "offending_source": "tp",
        "path_time_s": 5.05,
        "raw_rtde_identity": 43790.123,
        "source_ages_s": {
            "kunwei": 0.003,
            "rtde": 0.002,
            "tp": 0.081,
            "writer": 0.001,
        },
        "source_sequences": {
            "kunwei": 20,
            "rtde": 43790.123,
            "tp": 11,
            "writer": 12,
        },
    }
    expected_json = json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert str(caught.value) == "r005 raw PATH source age invariant violation: " + expected_json
    assert adapted._path_samples == []
