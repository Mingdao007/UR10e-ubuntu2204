from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step6_figure8_autotune_v1.v5_campaign import (  # noqa: E402
    CampaignIdentityV2,
    CampaignRoleV2,
    ENTRY_MODE_HOME_ONLY_V1,
    V5CampaignV2,
)
from step6_figure8_autotune_v1.v5_campaign_runner import (  # noqa: E402
    V5ExecutionJournalV1,
    V5PhysicalCampaignRunnerV1,
)
from step6_figure8_autotune_v1.v5_production_bundle import (  # noqa: E402
    V5ProductionBundleError,
    V5ProductionBundleV1,
    derive_v5_campaign_fingerprint,
    derive_v5_release_identity,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    LedgerRole,
    V5PhysicalAdmissionLedgerV2,
)
from step6_figure8_autotune_v1.v5_optimizer_journal import (  # noqa: E402
    V5OptimizerTellJournalV1,
)
from test_step6_autotuner_v5_campaign import (  # noqa: E402
    _install_fast_mature_cursor,
)


def test_release_and_role_fingerprints_are_isolated_and_v4_immutable() -> None:
    release = derive_v5_release_identity(
        source_identity={"source_sha256": "1" * 64},
        controller_triplet_sha256={
            "script": "2" * 64,
            "txt": "3" * 64,
            "urp": "4" * 64,
        },
        controller_readback_manifest_sha256="5" * 64,
        compatibility_fingerprint_sha256="6" * 64,
        home_calibration_receipt_sha256="7" * 64,
        composition_config_sha256="8" * 64,
        campaign_config_sha256="9" * 64,
        sidecars_config_sha256="a" * 64,
    )
    assert release["layout"] == 607
    assert release["v4_layout_606_mutated"] is False
    primary = derive_v5_campaign_fingerprint(
        role=CampaignRoleV2.PRIMARY,
        release_identity_sha256=release["release_identity_sha256"],
        home_calibration_receipt_sha256="7" * 64,
    )
    parent = {
        "primary_campaign_fingerprint": primary,
        "primary_closeout_sha256": "b" * 64,
        "primary_physical_ledger_head_sha256": "c" * 64,
        "primary_winner_controller_sha256": "d" * 64,
    }
    correction = derive_v5_campaign_fingerprint(
        role=CampaignRoleV2.CORRECTION,
        release_identity_sha256=release["release_identity_sha256"],
        home_calibration_receipt_sha256="7" * 64,
        parent=parent,
    )
    assert primary != correction
    with pytest.raises(V5ProductionBundleError, match="parent"):
        derive_v5_campaign_fingerprint(
            role=CampaignRoleV2.CORRECTION,
            release_identity_sha256=release["release_identity_sha256"],
            home_calibration_receipt_sha256="7" * 64,
            parent={"primary_campaign_fingerprint": primary},
        )


def test_completed_role_requires_real_home_stopped_writer_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = object.__new__(V5ProductionBundleV1)
    bundle.state_root = tmp_path
    identity = CampaignIdentityV2(
        CampaignRoleV2.PRIMARY,
        "a" * 64,
        "b" * 64,
        tmp_path / "primary" / "campaign",
        "PRIMARY:" + "a" * 64,
    )
    Path(identity.state_root).mkdir(parents=True)
    (Path(identity.state_root) / "report.json").write_text("{}\n", encoding="utf-8")
    phase_path = bundle._phase_runtime_path(CampaignRoleV2.PRIMARY)
    phase_path.parent.mkdir(parents=True, exist_ok=True)
    phase_path.write_text(
        json.dumps({"status": "released_stopped_home"}) + "\n",
        encoding="utf-8",
    )
    release_path = tmp_path / "primary" / "writer_release_receipt.json"
    assert bundle._completed_role(identity) is None
    release_path.write_text(
        json.dumps(
            {
                "home_verified": True,
                "stopped": False,
                "writer_released": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert bundle._completed_role(identity) is None

    expected_report = object()
    physical = SimpleNamespace(head_sha256="c" * 64)
    campaign = SimpleNamespace(report=lambda: expected_report)
    monkeypatch.setattr(
        bundle,
        "_campaign_components",
        lambda **_kwargs: (campaign, physical, object(), object()),
    )
    release = {
        "home_verified": True,
        "stopped": True,
        "writer_released": True,
    }
    release_path.write_text(json.dumps(release) + "\n", encoding="utf-8")
    assert bundle._completed_role(identity) == (expected_report, physical, release)


def test_stale_owner_open_or_partial_lifecycle_root_is_not_resumable(tmp_path: Path) -> None:
    bundle = object.__new__(V5ProductionBundleV1)
    bundle.state_root = tmp_path
    phase = tmp_path / "capability" / "phase_runtime.json"
    phase.parent.mkdir(parents=True)
    phase.write_text(json.dumps({"status": "owner_open"}) + "\n", encoding="utf-8")
    with pytest.raises(V5ProductionBundleError, match="ambiguous owner phase"):
        bundle._reject_ambiguous_state_root()

    phase.write_text(json.dumps({"status": "released_stopped_home"}) + "\n", encoding="utf-8")
    partial = tmp_path / "capability" / "live" / "000001.r013life.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    with pytest.raises(V5ProductionBundleError, match="partial lifecycle"):
        bundle._reject_ambiguous_state_root()


def test_home_only_connection_reset_receipts_repair_before_ambiguous_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production adapter closes the old dispatch and advances the next slot."""

    _install_fast_mature_cursor(monkeypatch)
    fp = "a" * 64
    release_sha = "b" * 64
    identity = CampaignIdentityV2(
        CampaignRoleV2.PRIMARY,
        fp,
        release_sha,
        tmp_path / "primary" / "campaign",
        f"PRIMARY:{fp}",
        entry_mode=ENTRY_MODE_HOME_ONLY_V1,
    )
    campaign = V5CampaignV2.create(identity, wire_session_epoch=1)
    plan = campaign.plan_next()
    assert plan is not None
    physical = V5PhysicalAdmissionLedgerV2(
        tmp_path / "primary" / "physical.jsonl",
        campaign_fingerprint=fp,
        release_identity_sha256=release_sha,
        role=LedgerRole.PRIMARY,
    )
    optimizer = V5OptimizerTellJournalV1(
        tmp_path / "primary" / "optimizer.jsonl",
        campaign_fingerprint=fp,
        release_identity_sha256=release_sha,
        role=LedgerRole.PRIMARY,
    )
    execution = V5ExecutionJournalV1(
        tmp_path / "primary" / "execution.jsonl",
        campaign_fingerprint=fp,
        release_identity_sha256=release_sha,
        role=LedgerRole.PRIMARY,
    )
    execution.append_dispatch("reset-chain", (plan,))

    live = tmp_path / "live" / "primary-resident-001"
    live.mkdir(parents=True)
    artifact = live / "000001-reset-chain.r013life"
    artifact.write_bytes(b"partial-r013life")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    partial = live / "000001-reset-chain.r013life.json"
    partial_payload = {
        "schema": "step5d.autotune-v4/r013-force-lifecycle-receipt-v1",
        "status": "incomplete",
        "artifact_path": str(artifact.resolve()),
        "artifact_sha256": artifact_sha,
        "artifact_row_count": 0,
        "coverage_complete": False,
        "home_verified": False,
        "errors": [
            "V5 live chain failed: canonical V5 RTDE output read failed: [Errno 104] Connection reset by peer",
        ],
    }
    partial.write_text(json.dumps(partial_payload, sort_keys=True) + "\n", encoding="utf-8")
    release = tmp_path / "primary" / "writer_release_receipt.json"
    release_payload = {
        "home_verified": True,
        "stopped": True,
        "writer_released": True,
    }
    release.write_text(json.dumps(release_payload, sort_keys=True) + "\n", encoding="utf-8")

    class _NeverOwner:
        def execute_chain(self, _request):
            raise AssertionError("repair must not dispatch motion")

    reconciler = V5PhysicalCampaignRunnerV1(
        campaign=campaign,
        owner=_NeverOwner(),
        physical_ledger=physical,
        optimizer_journal=optimizer,
        execution_journal=execution,
    )
    bundle = object.__new__(V5ProductionBundleV1)
    bundle.state_root = tmp_path
    receipts = bundle._repair_recoverable_home_only_dispatches(
        identity=identity,
        reconciler=reconciler,
        execution=execution,
    )

    assert len(receipts) == 1
    assert execution.ambiguous_chain_ids == ()
    assert execution.recoverable_chain_ids == ("reset-chain",)
    assert campaign.exact_novel_count == 0
    next_plan = campaign.plan_next()
    assert next_plan is not None and next_plan.budget_ordinal == 2
