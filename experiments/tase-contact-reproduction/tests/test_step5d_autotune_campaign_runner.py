#!/usr/bin/env python3
"""Contract tests for the production Step5d autotune campaign launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_step5d_autotune_campaign import (  # noqa: E402
    CampaignEpochLayout,
    StopAfterCurrentRequested,
    _campaign_binding,
    _campaign_spec,
    _observe_pending_identity_commit,
    _publish_runner_ready,
    _zero_identity_preplay_row,
    _v3_stop_requested,
    _wait_for_codex_candidate,
    closure_sample_from_bridge_row,
    discover_campaign_epochs,
    ensure_mailbox_parent,
    profile_from_epoch,
    tp_snapshot_from_bridge_row,
    validate_legacy_campaign_adoption,
)
from step5d_autotune_contract import ExecutionProfile  # noqa: E402
from step5d_autotune_journal import (  # noqa: E402
    CampaignIdentity,
    HighWaterMarks,
    JournalState,
    SupervisorJournal,
)
from step5d_autotune_supervisor import execution_profile_integer_id  # noqa: E402
from step5d_autotune_v3.state import CampaignPaths, set_stop_latch  # noqa: E402


def test_campaign_spec_accepts_single_trial_success_policy() -> None:
    campaign = _campaign_spec(ROOT, "a" * 64, 9)

    assert campaign.campaign_epoch == 9
    assert campaign.success_mae_n == 0.3


def test_machine_campaign_binding_is_plan_identity_not_authorization(
    tmp_path: Path,
) -> None:
    campaign = _campaign_spec(ROOT, "a" * 64, 9)
    payload = {
        "schema_version": "step5d_autotune_campaign_binding_v3",
        "campaign_id": campaign.campaign_id,
        "campaign_epoch": campaign.campaign_epoch,
        "campaign_fingerprint": campaign.campaign_fingerprint,
        "candidate_plan_revision": 1,
        "candidate_plan_sha256": "b" * 64,
        "trial_overlay_plan_sha256": "c" * 64,
        "binding_source": "test machine plan",
        "generated_at": "2026-07-20T08:00:00+08:00",
    }
    path = tmp_path / "machine-binding.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    binding = _campaign_binding(
        path.resolve(),
        campaign=campaign,
        campaign_fingerprint=campaign.campaign_fingerprint,
    )

    assert binding.candidate_plan_revision == 1
    assert not hasattr(binding, "live_authorized")
    assert not hasattr(binding, "authorization_ref_sha256")


def test_campaign_epoch_discovery_selects_every_epoch_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "campaign"
        campaign_id = "stable-campaign-id"
        for epoch, epoch_root in (
            (1, root),
            (2, root / "epochs" / "0000000002"),
            (3, root / "epochs" / "0000000003"),
        ):
            store = epoch_root / "store"
            store.mkdir(parents=True)
            campaign = _campaign_spec(
                ROOT,
                str(epoch) * 64,
                epoch,
                campaign_id=campaign_id,
            )
            (store / "campaign.json").write_text(
                json.dumps({"campaign": campaign.__dict__}),
                encoding="utf-8",
            )

        layouts = discover_campaign_epochs(root)

        assert [row.epoch for row in layouts] == [1, 2, 3]
        assert layouts[-1].root.name == "0000000003"
        assert all(row.campaign.campaign_id == campaign_id for row in layouts)


def test_prior_epoch_profile_comes_from_durable_manifest_not_current_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        epoch_root = Path(tmp) / "epoch"
        store_root = epoch_root / "store"
        store_root.mkdir(parents=True)
        campaign = _campaign_spec(ROOT, "a" * 64, 1)
        retained = ExecutionProfile("nf020-slew020-a020", 0.020, 0.2, 0.2)
        manifest = {
            "campaign": campaign.__dict__,
            "execution_profile": retained.payload(),
        }
        (store_root / "campaign.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        journal = SupervisorJournal(epoch_root / "journal")
        journal.append(
            JournalState(
                campaign=CampaignIdentity(
                    campaign_id=campaign.campaign_id,
                    campaign_epoch=campaign.campaign_epoch,
                    campaign_fingerprint=campaign.campaign_fingerprint,
                    backend_id="step5d_v35_native",
                    source_fingerprint="b" * 64,
                    config_fingerprint="c" * 64,
                ),
                phase="home",
                high_water=HighWaterMarks(),
                candidate_tokens={},
                plant_epoch=1,
                execution_profile_id=retained.profile_id,
                execution_profile_integer_id=execution_profile_integer_id(retained),
            )
        )
        layout = CampaignEpochLayout(
            epoch=1,
            root=epoch_root,
            store_root=store_root,
            journal_root=epoch_root / "journal",
            manifest=manifest,
            campaign=campaign,
        )

        assert profile_from_epoch(layout) == retained


def _write_home_epoch(
    epoch_root: Path,
    *,
    epoch: int,
    campaign_id: str,
) -> None:
    store_root = epoch_root / "store"
    store_root.mkdir(parents=True)
    campaign = _campaign_spec(
        ROOT,
        str(epoch % 10) * 64,
        epoch,
        campaign_id=campaign_id,
    )
    retained = ExecutionProfile("nf050-slew050-a050", 0.05, 0.5, 0.5)
    manifest = {
        "campaign": campaign.__dict__,
        "execution_profile": retained.payload(),
        "frozen_fingerprint": {
            "backend_id": "step5d_v35_native_backend_v1",
            "source_fingerprint": "b" * 64,
            "config_fingerprint": "c" * 64,
        },
        "selection_policy": "codex_batches",
    }
    (store_root / "campaign.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    SupervisorJournal(epoch_root / "journal").append(
        JournalState(
            campaign=CampaignIdentity(
                campaign_id=campaign.campaign_id,
                campaign_epoch=campaign.campaign_epoch,
                campaign_fingerprint=campaign.campaign_fingerprint,
                backend_id="step5d_v35_native_backend_v1",
                source_fingerprint="b" * 64,
                config_fingerprint="c" * 64,
            ),
            phase="home",
            high_water=HighWaterMarks(),
            candidate_tokens={},
            plant_epoch=1,
            execution_profile_id=retained.profile_id,
            execution_profile_integer_id=execution_profile_integer_id(retained),
        )
    )


def test_explicit_legacy_parent_preflight_crosses_real_process_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    _write_home_epoch(root, epoch=16, campaign_id="retained-campaign")
    failed = root / "epochs" / "0000000017"
    _write_home_epoch(failed, epoch=17, campaign_id="retained-campaign")
    os.link(failed / "journal" / ".journal.lock", tmp_path / "linked-lock")

    retained = validate_legacy_campaign_adoption(root, campaign_epoch=16)
    assert retained["campaign_epoch"] == 16
    assert retained["phase"] == "home"
    with pytest.raises(Exception, match="singly-linked"):
        validate_legacy_campaign_adoption(root, campaign_epoch=17)

    command = [
        sys.executable,
        "-c",
        (
            "import json,sys; from pathlib import Path; "
            "from run_step5d_autotune_campaign import "
            "validate_legacy_campaign_adoption as validate; "
            "print(json.dumps(validate(Path(sys.argv[1]), campaign_epoch=16)))"
        ),
        str(root),
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(ROOT / "tools"),
                str(ROOT.parents[1] / "src/ur10e_experiment_runtime"),
            ]
        ),
    }
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["campaign_epoch"] == 16


def bridge_row() -> dict[str, str]:
    row = {
        "ur_timestamp": "10.5",
        "ur_safety_mode": "1",
        "ur_output_int_register_24": "1",
        "ur_output_int_register_25": "2",
        "ur_output_int_register_26": "70",
        "ur_output_int_register_27": "3",
        "ur_output_int_register_28": "1",
        "ur_output_int_register_29": "111",
        "ur_output_int_register_30": "4",
        "ur_output_double_register_35": "40.3",
        "ur_output_double_register_36": "0.001",
        "ur_output_double_register_37": "0.002",
        "ur_output_double_register_38": "0.003",
        "ur_output_double_register_39": "3",
        "ur_output_double_register_40": "0.0",
        "ur_output_double_register_41": "0.0",
        "ur_output_double_register_42": "0.05",
        "ur_output_double_register_43": "0.1",
        "ur_output_double_register_44": "0.002",
    }
    for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd"):
        for index in range(6):
            row[f"ur_{name}_{index}"] = str(index / 1000.0)
    return row


def test_bridge_row_maps_to_exact_tp_snapshot() -> None:
    snapshot = tp_snapshot_from_bridge_row(bridge_row())
    assert snapshot.state == "WAIT_ACK"
    assert snapshot.campaign_epoch_echo == 1
    assert snapshot.trial_id_echo == 2
    assert snapshot.candidate_token_echo == 3
    assert snapshot.execution_profile_integer_id_echo == 111
    assert snapshot.consumed_command_seq == 4


def test_preplay_zero_state_is_explicit_and_cannot_impersonate_ready_home() -> None:
    row = bridge_row()
    for register in range(24, 35):
        row[f"ur_output_int_register_{register}"] = "0"

    assert _zero_identity_preplay_row(row) is True
    with pytest.raises(RuntimeError, match="unknown TP state 0"):
        tp_snapshot_from_bridge_row(row)


def test_preplay_state_with_identity_echo_is_rejected() -> None:
    row = bridge_row()
    for register in range(24, 35):
        row[f"ur_output_int_register_{register}"] = "0"
    row["ur_output_int_register_27"] = "9"

    assert _zero_identity_preplay_row(row) is False
    with pytest.raises(RuntimeError, match="unknown TP state 0"):
        tp_snapshot_from_bridge_row(row)


def test_bridge_row_maps_to_safe_closure_input_shape() -> None:
    sample = closure_sample_from_bridge_row(bridge_row())
    assert sample["safety_mode"] == 1
    assert sample["actual_TCP_pose"] == [index / 1000.0 for index in range(6)]
    assert sample["output_int_register_26"] == 70
    assert sample["output_double_register_38"] == 0.003
    assert sample["output_double_register_35"] == 40.3
    assert sample["output_double_register_39"] == 3.0
    assert sample["output_double_register_44"] == 0.002


def test_campaign_creates_its_own_runtime_mailbox_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bridge_run = Path(tmp) / "bridge"
        bridge_run.mkdir()
        mailbox = bridge_run / "runtime" / "command.json"

        ensure_mailbox_parent(mailbox, bridge_run)

        assert mailbox.parent.is_dir()
        with pytest.raises(RuntimeError, match="selected bridge run"):
            ensure_mailbox_parent(Path(tmp) / "other" / "command.json", bridge_run)


def test_runner_ready_distinguishes_preplay_from_durable_home() -> None:
    campaign = _campaign_spec(ROOT, "a" * 64, 9)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ready = root / "bridge" / "runtime" / "campaign_runner_ready.json"
        _publish_runner_ready(
            ready,
            durable_state_ready=False,
            bridge_run=root / "bridge",
            campaign_root=root / "campaign",
            campaign=campaign,
            campaign_fingerprint=campaign.campaign_fingerprint,
            selection_policy="codex_batches",
        )
        payload = json.loads(ready.read_text(encoding="utf-8"))
        assert payload["durable_state_ready"] is False
        assert payload["state"] == "waiting_for_play"

        _publish_runner_ready(
            ready,
            durable_state_ready=True,
            bridge_run=root / "bridge",
            campaign_root=root / "campaign",
            campaign=campaign,
            campaign_fingerprint=campaign.campaign_fingerprint,
            selection_policy="codex_batches",
        )
        payload = json.loads(ready.read_text(encoding="utf-8"))
        assert payload["durable_state_ready"] is True
        assert payload["state"] == "ready_home"


def test_v3_stop_latch_is_default_off_exactly_scoped_and_durable(
    tmp_path: Path,
) -> None:
    campaign_root = tmp_path / "campaign"
    expected = campaign_root / "control" / "stop_after_current.json"
    assert _v3_stop_requested(None, campaign_root=campaign_root) is False
    assert _v3_stop_requested(expected, campaign_root=campaign_root) is False

    set_stop_latch(CampaignPaths(campaign_root), armed=True)
    assert _v3_stop_requested(expected, campaign_root=campaign_root) is True
    with pytest.raises(RuntimeError, match="campaign_root/control"):
        _v3_stop_requested(tmp_path / "other.json", campaign_root=campaign_root)


def test_waiting_at_ready_home_observes_v3_latch_before_selecting_candidate(
    tmp_path: Path,
) -> None:
    with pytest.raises(StopAfterCurrentRequested):
        _wait_for_codex_candidate(
            plan_path=tmp_path / "not-read.json",
            campaign_id="not-read",
            supervisor=None,  # type: ignore[arg-type]
            coordinator=None,  # type: ignore[arg-type]
            bridge_csv=tmp_path / "not-read.csv",
            campaign_root=tmp_path / "campaign",
            previous_plan=None,
            timeout_s=1.0,
            stop_requested=lambda: True,
        )


def test_terminal_identity_commit_budget_starts_at_first_pending_row() -> None:
    deadline = _observe_pending_identity_commit(None, observed_at_s=1_000.0)
    assert deadline == pytest.approx(1_000.25)
    assert (
        _observe_pending_identity_commit(deadline, observed_at_s=1_000.20)
        == deadline
    )
    with pytest.raises(RuntimeError, match="identity commit exceeded"):
        _observe_pending_identity_commit(deadline, observed_at_s=1_000.251)


def test_v3_derived_queue_hook_is_after_direct_commit() -> None:
    source = (ROOT / "tools" / "run_step5d_autotune_campaign.py").read_text(
        encoding="utf-8"
    )
    finalized = source.index("result = finalize_produced_bundle_direct(")
    committed = source.index('"direct_ready_committed"', finalized)
    queued = source.index("derived_postprocess.submit(", committed)
    assert finalized < committed < queued


def test_each_v3_arm_rechecks_full_runtime_binding_before_issue() -> None:
    source = (ROOT / "tools" / "run_step5d_autotune_campaign.py").read_text(
        encoding="utf-8"
    )
    guard = source.index("RuntimeEnvironmentBindingGuard.full(")
    next_sequence = source.index("next_arm_command_seq = (", guard)
    recheck = source.index(
        "runtime_environment_guard.recheck(next_arm_command_seq)",
        next_sequence,
    )
    issue = source.index("arm = coordinator.issue_arm(", recheck)
    identity_check = source.index(
        "if arm.command_seq != next_arm_command_seq:", issue
    )

    assert guard < next_sequence < recheck < issue < identity_check


def test_live_children_reuse_supervisor_attestation_before_arm_rechecks() -> None:
    supervisor = (ROOT / "tools" / "run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )
    bridge = (ROOT / "tools" / "run_step5d_autotune_v3_bridge.py").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "tools" / "run_step5d_autotune_campaign.py").read_text(
        encoding="utf-8"
    )

    assert 'args._runtime_pointer = require_runtime_profile("control")' in supervisor
    assert (
        'require_runtime_profile("control", full_integrity=False)'
        in bridge
    )
    assert (
        'require_runtime_profile(\n            "optimizer", full_integrity=False\n        )'
        in runner
    )
    assert "RuntimeEnvironmentBindingGuard.full(" in runner
    assert "runtime_environment_guard.recheck(next_arm_command_seq)" in runner


def test_campaign_runner_has_no_implicit_plot_or_network_publisher() -> None:
    source = (ROOT / "tools" / "run_step5d_autotune_campaign.py").read_text(
        encoding="utf-8"
    )
    active = json.loads(
        (ROOT / "config/step5d/v3_active_surface.json").read_text(encoding="utf-8")
    )

    assert "publish_step5d_autotune_plot.py" not in source
    assert "trial_plot_published" not in source
    assert "trial_plot_publish_failed" not in source
    assert "subprocess" not in source
    assert "ssh" not in source
    assert "scp" not in source
    assert (ROOT / "tools/publish_step5d_autotune_plot.py").is_file()
    assert (
        "tools/publish_step5d_autotune_plot.py"
        not in active["active_orchestration_paths"]
    )


def test_campaign_runner_import_does_not_load_matplotlib() -> None:
    import_paths = [
        str(ROOT / "tools"),
        str(ROOT.parents[1] / "src/ur10e_experiment_runtime"),
    ]
    code = f"""
import builtins
import sys

sys.path[:0] = {import_paths!r}
original_import = builtins.__import__

def reject_matplotlib(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".", 1)[0] == "matplotlib":
        raise AssertionError(f"matplotlib imported by campaign runner: {{name}}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = reject_matplotlib
import run_step5d_autotune_campaign

loaded = sorted(
    name for name in sys.modules
    if name == "matplotlib" or name.startswith("matplotlib.")
)
if loaded:
    raise AssertionError(f"matplotlib loaded by campaign runner: {{loaded}}")
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
