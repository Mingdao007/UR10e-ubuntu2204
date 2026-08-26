from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r013.lifecycle_trace import (  # noqa: E402
    LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
    LifecycleTrace,
)
from step6_figure8_autotune_v1.v5_campaign import (  # noqa: E402
    CampaignIdentityV2,
    CampaignRoleV2,
    ENTRY_MODE_HOME_ONLY_V1,
    V5_WIRE_EPOCH,
    V5CampaignV2,
)
from step6_figure8_autotune_v1.v5_campaign_runner import (  # noqa: E402
    V5AmbiguousPhysicalDispatch,
    V5CampaignRunnerError,
    V5DurableChainResultV1,
    V5ExecutionJournalV1,
    V5PhysicalCampaignRunnerV1,
    V5RecoverableFailureClass,
    V5RecoverableFailureReceiptV1,
    V5RecoverableOwnerFailure,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    BoundaryMode,
    LedgerRole,
    V5PhysicalAdmissionLedgerV2,
    canonical_sha256,
)
from step6_figure8_autotune_v1.v5_live_owner import (  # noqa: E402
    V5LifecycleTraceAdapter,
    V5LiveContextV1,
    V5SingleWriterOwnerV1,
)
from step6_figure8_autotune_v1.v5_optimizer_journal import (  # noqa: E402
    V5OptimizerTellJournalV1,
)
from test_step6_autotuner_v5_campaign import (  # noqa: E402
    FP,
    RELEASE,
    _install_fast_mature_cursor,
)
from test_step6_autotuner_v5_live_owner import (  # noqa: E402
    _Backend,
    _Transport,
    _Writer,
)


class _NeverOwner:
    def __init__(self) -> None:
        self.calls = 0

    def execute_chain(self, _request):
        self.calls += 1
        raise AssertionError("resume must not dispatch physical motion")


def _identity(tmp_path: Path) -> CampaignIdentityV2:
    return CampaignIdentityV2(
        CampaignRoleV2.PRIMARY,
        FP,
        RELEASE,
        tmp_path / "campaign",
        f"PRIMARY:{FP}",
    )


def _components(tmp_path: Path, campaign: V5CampaignV2):
    physical = V5PhysicalAdmissionLedgerV2(
        tmp_path / "physical.jsonl",
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    optimizer = V5OptimizerTellJournalV1(
        tmp_path / "optimizer.jsonl",
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    execution = V5ExecutionJournalV1(
        tmp_path / "execution.jsonl",
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    return physical, optimizer, execution


def _rewrite_activation_with_valid_outer_hashes(
    durable_path: Path,
    mutate,
) -> None:
    payload = json.loads(durable_path.read_text(encoding="utf-8"))
    journal_path = Path(payload["activation_receipt"]["journal_path"])
    journal_rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    mutate(journal_rows)
    previous = "0" * 64
    for row in journal_rows[1:]:
        row.pop("row_sha256", None)
        row["details_sha256"] = canonical_sha256(row["details"])
        row["previous_sha256"] = previous
        row["row_sha256"] = canonical_sha256(row)
        previous = row["row_sha256"]
    journal_bytes = (
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in journal_rows
        )
    ).encode("utf-8")
    journal_path.write_bytes(journal_bytes)
    payload["activation_receipt"]["journal_sha256"] = hashlib.sha256(
        journal_bytes
    ).hexdigest()
    payload["activation_receipt"]["journal_head_sha256"] = previous
    publications = payload["activation_receipt"]["publication_receipts"]
    for publication in publications:
        generation = publication["generation"]
        durable_rows = [
            row
            for row in journal_rows[1:]
            if row.get("generation") == generation
            and row.get("state") in {"COMMIT_ACK", "ACTIVATED"}
        ]
        publication["durable_row_sha256s"] = [
            row["row_sha256"] for row in durable_rows
        ]
        publication["durable_journal_head_sha256"] = durable_rows[-1][
            "row_sha256"
        ]
    payload["activation_receipt"]["publication_receipts_sha256"] = (
        canonical_sha256(publications)
    )
    unsigned = {
        key: value for key, value in payload.items() if key != "result_sha256"
    }
    payload["result_sha256"] = canonical_sha256(unsigned)
    durable_path.write_bytes(
        (
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    )


def test_owner_sealed_result_crash_reconciles_without_second_motion_or_tell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fast_mature_cursor(monkeypatch)
    identity = _identity(tmp_path)
    campaign = V5CampaignV2.create(
        identity,
        wire_session_epoch=V5_WIRE_EPOCH,
    )
    transport = _Transport()
    writer = _Writer(transport)
    trace = V5LifecycleTraceAdapter(
        LifecycleTrace(
            tmp_path / "life",
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
    )
    context = V5LiveContextV1(
        writer=writer,
        transport=transport,  # type: ignore[arg-type]
        trace=trace,
        backend_factory=lambda _spec: _Backend(),
        anchor_pose=(0.0,) * 6,
        close_callback=lambda: None,
    )
    physical, optimizer, execution = _components(tmp_path, campaign)
    runner = V5PhysicalCampaignRunnerV1(
        campaign=campaign,
        owner=V5SingleWriterOwnerV1(context),
        physical_ledger=physical,
        optimizer_journal=optimizer,
        execution_journal=execution,
        max_chain_attempts=2,
    )

    def crash_before_execution_append(_plans, _result):
        raise RuntimeError("simulated crash before execution result append")

    monkeypatch.setattr(execution, "append_result", crash_before_execution_append)
    with pytest.raises(RuntimeError, match="before execution result append"):
        runner.run_next_chain()

    assert writer.session.finished is True
    assert len(execution.ambiguous_chain_ids) == 1
    durable_path = execution.result_path(execution.ambiguous_chain_ids[0])
    assert durable_path.is_file()
    assert execution.results == ()
    assert physical.records == ()
    assert optimizer.tell_count == 0

    durable_original = durable_path.read_bytes()
    durable_payload = json.loads(durable_original)
    activation_path = Path(
        durable_payload["activation_receipt"]["journal_path"]
    )
    activation_original = activation_path.read_bytes()

    def prepared_moved_to_tail(rows: list[dict]) -> None:
        prepared = next(row for row in rows if row.get("state") == "PREPARED")
        intent = next(row for row in rows if row.get("state") == "COMMIT_INTENT")
        prepared["details"]["sample_index"] = (
            int(intent["details"]["sample_index"]) - 1
        )

    _rewrite_activation_with_valid_outer_hashes(
        durable_path,
        prepared_moved_to_tail,
    )
    with pytest.raises(
        V5CampaignRunnerError,
        match="PREPARED/COMMIT intent",
    ):
        execution.recover_sealed_results()
    assert physical.records == ()
    assert optimizer.tell_count == 0

    activation_path.write_bytes(activation_original)
    durable_path.write_bytes(durable_original)

    def forged_commit_identity_and_seed(rows: list[dict]) -> None:
        intent = next(row for row in rows if row.get("state") == "COMMIT_INTENT")
        intent["details"]["wire_input"]["38"] += 1
        intent["details"]["commit_seed_qdot"][0] = 0.01

    _rewrite_activation_with_valid_outer_hashes(
        durable_path,
        forged_commit_identity_and_seed,
    )
    with pytest.raises(
        V5CampaignRunnerError,
        match="PREPARED/COMMIT intent",
    ):
        execution.recover_sealed_results()
    assert physical.records == ()
    assert optimizer.tell_count == 0

    activation_path.write_bytes(activation_original)
    durable_path.write_bytes(durable_original)

    early_publication = json.loads(durable_original)
    publication = early_publication["activation_receipt"][
        "publication_receipts"
    ][0]
    publication["durability_settled_monotonic_s"] = publication[
        "first_successor_publish_monotonic_s"
    ]
    early_publication["activation_receipt"][
        "publication_receipts_sha256"
    ] = canonical_sha256(
        early_publication["activation_receipt"]["publication_receipts"]
    )
    early_publication["result_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in early_publication.items()
            if key != "result_sha256"
        }
    )
    durable_path.write_bytes(
        (
            json.dumps(
                early_publication,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    with pytest.raises(
        V5CampaignRunnerError,
        match="activation receipt is not cold-valid",
    ):
        execution.recover_sealed_results()
    assert physical.records == ()
    assert optimizer.tell_count == 0

    durable_path.write_bytes(durable_original)

    assert execution.recover_sealed_results() == 1
    assert execution.recover_sealed_results() == 0
    assert execution.ambiguous_chain_ids == ()
    assert len(execution.results) == 1

    sealed_result = execution.results[0]
    tampered = V5DurableChainResultV1(
        sealed_result.chain_id,
        sealed_result.plans,
        (
            sealed_result.records[0],
            replace(
                sealed_result.records[1],
                release_identity_sha256="e" * 64,
            ),
        ),
        sealed_result.lifecycle_receipt,
        sealed_result.event_bundle_sha256,
        sealed_result.home_evidence_sha256,
        sealed_result.activation_receipt,
    )
    atomic_runner = V5PhysicalCampaignRunnerV1(
        campaign=campaign,
        owner=_NeverOwner(),
        physical_ledger=physical,
        optimizer_journal=optimizer,
        execution_journal=execution,
    )
    with pytest.raises(V5CampaignRunnerError, match="identity/artifact"):
        atomic_runner._account_chain(tampered)
    assert physical.records == ()
    assert optimizer.tell_count == 0
    assert campaign.outcomes == ()

    resumed_campaign = V5CampaignV2.resume(
        identity,
        wire_session_epoch=V5_WIRE_EPOCH,
    )
    resumed_physical, resumed_optimizer, resumed_execution = _components(
        tmp_path, resumed_campaign
    )
    never = _NeverOwner()
    resumed = V5PhysicalCampaignRunnerV1(
        campaign=resumed_campaign,
        owner=never,
        physical_ledger=resumed_physical,
        optimizer_journal=resumed_optimizer,
        execution_journal=resumed_execution,
    )
    assert resumed.reconcile_durable_results() == 2
    assert resumed.reconcile_durable_results() == 0
    assert never.calls == 0
    assert resumed_campaign.exact_novel_count == 2
    assert len(resumed_physical.records) == 2
    assert resumed_optimizer.tell_count == 2
    assert [record.boundary.mode for record in resumed_physical.records] == [
        BoundaryMode.CONTACT_ROLLOVER,
        BoundaryMode.HOME,
    ]


def test_dispatch_without_sealed_result_is_ambiguous_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path))
    plan = campaign.plan_next()
    assert plan is not None
    physical, optimizer, execution = _components(tmp_path, campaign)
    execution.append_dispatch("ambiguous-chain", (plan,))
    never = _NeverOwner()
    runner = V5PhysicalCampaignRunnerV1(
        campaign=campaign,
        owner=never,
        physical_ledger=physical,
        optimizer_journal=optimizer,
        execution_journal=execution,
    )
    with pytest.raises(V5AmbiguousPhysicalDispatch, match="ambiguous-chain"):
        runner.run_next_chain()
    assert never.calls == 0
    assert campaign.exact_novel_count == 0
    assert optimizer.tell_count == 0


def test_home_only_connection_reset_closes_dispatch_and_resumes_without_gp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed reset after independent Home proof consumes one budget slot."""

    _install_fast_mature_cursor(monkeypatch)
    identity = CampaignIdentityV2(
        CampaignRoleV2.PRIMARY,
        FP,
        RELEASE,
        tmp_path / "campaign",
        f"PRIMARY:{FP}",
        entry_mode=ENTRY_MODE_HOME_ONLY_V1,
    )
    campaign = V5CampaignV2.create(identity, wire_session_epoch=V5_WIRE_EPOCH)
    physical, optimizer, execution = _components(tmp_path, campaign)

    artifact_path = (tmp_path / "partial.r013life").resolve()
    artifact_path.write_bytes(b"R013LIFE-partial")
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    partial_receipt = {
        "schema": "step5d.autotune-v4/r013-force-lifecycle-receipt-v1",
        "status": "incomplete",
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "artifact_row_count": 0,
        "coverage_complete": False,
        "home_verified": False,
    }
    partial_receipt_path = (tmp_path / "partial.r013life.json").resolve()
    partial_receipt_path.write_text(
        json.dumps(partial_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    release_receipt = {
        "schema": "step6.autotune/figure8-writer-release-receipt-v1",
        "home_verified": True,
        "stopped": True,
        "writer_released": True,
    }
    release_path = (tmp_path / "writer_release_receipt.json").resolve()
    release_path.write_text(
        json.dumps(release_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )

    def make_failure(request) -> V5RecoverableOwnerFailure:
        receipt = V5RecoverableFailureReceiptV1(
            chain_id=request.chain_id,
            failure_class=V5RecoverableFailureClass.CONNECTION_RESET,
            reason="canonical V5 RTDE output read failed: [Errno 104] Connection reset by peer",
            partial_receipt_path=str(partial_receipt_path),
            partial_receipt_sha256=hashlib.sha256(
                partial_receipt_path.read_bytes()
            ).hexdigest(),
            partial_artifact_path=str(artifact_path),
            partial_artifact_sha256=artifact_sha256,
            writer_release_receipt_path=str(release_path),
            writer_release_receipt_sha256=hashlib.sha256(
                release_path.read_bytes()
            ).hexdigest(),
            partial_receipt=partial_receipt,
            writer_release_receipt=release_receipt,
        )
        return V5RecoverableOwnerFailure(receipt)

    class _ResetOwner:
        def __init__(self) -> None:
            self.calls = 0

        def execute_chain(self, request):
            self.calls += 1
            raise make_failure(request)

    owner = _ResetOwner()
    runner = V5PhysicalCampaignRunnerV1(
        campaign=campaign,
        owner=owner,
        physical_ledger=physical,
        optimizer_journal=optimizer,
        execution_journal=execution,
    )
    with pytest.raises(V5RecoverableOwnerFailure):
        runner.run_next_chain()

    assert owner.calls == 1
    assert execution.ambiguous_chain_ids == ()
    assert execution.recoverable_chain_ids == (next(iter(execution._dispatches)),)
    assert campaign.exact_novel_count == 0
    assert campaign.dispatched_novel_count == 1
    assert campaign._candidate_observations(  # non-GP failure is excluded
        campaign.plans[0].stage
    ) == ()

    resumed_campaign = V5CampaignV2.resume(identity, wire_session_epoch=2)
    resumed_physical, resumed_optimizer, resumed_execution = _components(
        tmp_path, resumed_campaign
    )
    resumed_owner = _NeverOwner()
    resumed = V5PhysicalCampaignRunnerV1(
        campaign=resumed_campaign,
        owner=resumed_owner,
        physical_ledger=resumed_physical,
        optimizer_journal=resumed_optimizer,
        execution_journal=resumed_execution,
    )
    assert resumed.reconcile_durable_results() == 0
    next_plan = resumed_campaign.plan_next()
    assert next_plan is not None
    assert next_plan.budget_ordinal == 2
    assert next_plan.dispatch_index == 2
    assert resumed_owner.calls == 0
