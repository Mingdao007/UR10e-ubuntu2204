from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_autotune_v3 import runtime_gate as gate_module  # noqa: E402
from step5d_autotune_v3.runtime_gate import (  # noqa: E402
    ARM_GATE_WATCHDOG_INTERVAL_S,
    ArmGateProvider,
    CampaignLease,
    RuntimeEnvironmentBindingGuard,
    RuntimeGateError,
    process_starttime,
    publish_arm_observation,
    revoke_arm_observation,
    write_campaign_lease,
)
from step5d_autotune_v3.state import atomic_json  # noqa: E402


def test_process_starttime_rejects_zombie_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.read_text

    def fake_read_text(path: Path, *args, **kwargs) -> str:
        if path == Path("/proc/42/stat"):
            return "42 (zombie child) Z " + " ".join(["0"] * 18 + ["700"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with pytest.raises(RuntimeGateError, match="not live"):
        process_starttime(42)


def _runtime_binding_payload(*, bundle_id: str = "a" * 64) -> dict[str, object]:
    return {
        "schema": "step5d.autotune-v3/runtime-process-binding-v1",
        "bundle_id": bundle_id,
        "contract_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
        "runtime_attestation_sha256": "d" * 64,
        "gpu_uuid": "GPU-test-runtime-binding",
        "profiles": {
            profile: {
                "environment_id": character * 64,
                "python_executable": f"/runtime/{profile}/bin/python",
                "record_tree_sha256": "e" * 64,
                "profile_tree_sha256": "f" * 64,
            }
            for profile, character in (("control", "1"), ("optimizer", "2"))
        },
    }


@pytest.fixture(autouse=True)
def _stable_managed_runtime_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    state: dict[str, object] = {"binding": _runtime_binding_payload()}
    monkeypatch.setattr(
        gate_module,
        "load_runtime_pointer_identity",
        lambda: {"test_pointer": True},
    )
    monkeypatch.setattr(
        gate_module,
        "runtime_binding",
        lambda *, runtime_pointer=None: state["binding"],
    )
    return state


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, SimpleNamespace, dict, CampaignLease, Path, Path]:
    root = tmp_path / "experiment"
    (root / "config/step5d/releases/fake").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    launcher = root / "scripts/step5d-autotune-v3.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source = root / "tools/runtime_source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    repository_source = root / "repository_source.py"
    repository_source.write_text("REPOSITORY_VALUE = 1\n", encoding="utf-8")
    control = root / "config/step5d/releases/fake/safety_envelope.json"
    control.write_text('{"safety":"frozen"}\n', encoding="utf-8")
    script_sha = "a" * 64
    runtime_identity = {
        "schema": "step5d.autotune-v3/tp-runtime-identity-v1",
        "program_id": "fake_r010",
        "protocol_id": "rolling-v1",
        "protocol_version": 1,
        "digest_hi": 1234,
        "digest_lo": 5678,
        "script_basis_sha256": "b" * 64,
        "script_artifact_sha256": script_sha,
        "registers": {
            "protocol_version": 35,
            "digest_hi": 36,
            "digest_lo": 37,
        },
    }
    manifest = root / "config/step5d/releases/fake/manifest.json"
    atomic_json(
        manifest,
        {
            "tp_runtime_identity": runtime_identity,
            "safety_envelope": {
                "path": "safety_envelope.json",
                "sha256": _sha(control),
            },
        },
    )
    manifest_sha = _sha(manifest)
    atomic_json(
        root / "config/step5d/current.json",
        {
            "schema": "step5d.autotune-v3/current-release-pointer-v1",
            "manifest_path": "config/step5d/releases/fake/manifest.json",
            "manifest_sha256": manifest_sha,
        },
    )
    release = SimpleNamespace(
        manifest_path="config/step5d/releases/fake/manifest.json",
        manifest_sha256=manifest_sha,
        release_stage_id="step5d_strict_rnn_autotune_v3",
        program_id="fake_r010",
        protocol_id="rolling-v1",
        artifacts={".script": {"sha256": script_sha}},
        controller_target="/programs/andyl/kunwei/step5/fake_r010.urp",
        source_fingerprints={
            "scripts/step5d-autotune-v3.sh": _sha(launcher),
            "tools/runtime_source.py": _sha(source),
        },
        verification={
            "repository_source_root_depth": 0,
            "repository_source_fingerprints": {
                "repository_source.py": _sha(repository_source),
            },
        },
    )
    lease = CampaignLease.issue(
        lease_id="1" * 32,
        launch_id="2" * 32,
        manifest_sha256=manifest_sha,
        release_stage_id=release.release_stage_id,
        program_id=release.program_id,
        protocol_id=release.protocol_id,
        campaign_id="campaign-a",
        campaign_epoch=7,
        campaign_fingerprint="c" * 64,
        safety_envelope_sha256=_sha(control),
    )
    lease_path = root / "run/campaign_lease.json"
    lease_sha = write_campaign_lease(lease_path.absolute(), lease)
    gate_path = root / "run/arm_gate.json"
    contract = {
        "manifest_sha256": manifest_sha,
        "safety_envelope_sha256": _sha(control),
        "expected_loaded_program": "/programs/andyl/kunwei/step5/fake_r010.urp",
        "tp_runtime_identity": runtime_identity,
    }
    return root, release, contract, lease, lease_path, gate_path


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ur_timestamp": 100.0,
        "ur_runtime_state": 2,
        "ur_safety_mode": 1,
        "ur_output_int_register_26": 10,
        "ur_output_int_register_35": 1,
        "ur_output_int_register_36": 1234,
        "ur_output_int_register_37": 5678,
    }
    row.update(overrides)
    return row


def _dashboard(path: str = "/programs/andyl/kunwei/step5/fake_r010.urp") -> dict[str, str]:
    return {
        "get loaded program": f"Loaded program: {path}",
        "programState": "PLAYING fake_r010.urp",
        "safetymode": "Safetymode: NORMAL",
    }


def _fresh_get_binding(
    *, observed_at_unix_ns: int | None = None
) -> dict[str, object]:
    return {
        "delivery_transaction_id": "f" * 32,
        "fresh_get_observed_at_unix_ns": (
            time.time_ns()
            if observed_at_unix_ns is None
            else observed_at_unix_ns
        ),
    }


def _arm_binding(
    *,
    mailbox_sha256: str = "d" * 64,
    trial_id: int = 1,
    command_seq: int = 1,
) -> dict[str, object]:
    return {
        "mailbox_sha256": mailbox_sha256,
        "campaign_epoch": 7,
        "trial_id": trial_id,
        "command": 1,
        "candidate_token": 100 + trial_id,
        "execution_profile_id": 633,
        "command_seq": command_seq,
        "logical_batch_sequence": 1,
        "trial_uid": f"trial-{trial_id}",
    }


def _observe_provider(
    provider: ArmGateProvider,
    *,
    timestamp: float = 100.0,
    connection_epoch: int = 0,
) -> None:
    provider.observe_rtde(
        {
            "timestamp": timestamp,
            "runtime_state": 2,
            "safety_mode": 1,
            "output_int_register_35": 1,
            "output_int_register_36": 1234,
            "output_int_register_37": 5678,
        },
        connection_epoch=connection_epoch,
    )


def test_campaign_lease_binds_supervisor_starttime(tmp_path: Path) -> None:
    _root, _release, _contract, lease, _lease_path, _gate_path = _fixture(tmp_path)

    assert lease.supervisor_pid == os.getpid()
    assert lease.supervisor_starttime == process_starttime(os.getpid())
    assert lease.document["physical_play_stop_required"] is True
    assert lease.document["safety_boundary"].startswith("lease_is_not_an_estop")


def test_runtime_environment_guard_rechecks_once_per_new_arm_sequence() -> None:
    expected = _runtime_binding_payload()
    observed = {"binding": expected}
    calls: list[int] = []

    def load() -> object:
        calls.append(len(calls) + 1)
        return observed["binding"]

    guard = RuntimeEnvironmentBindingGuard(
        expected_binding=expected,
        binding_loader=load,
    )

    first_sha = guard.recheck(1)
    assert guard.recheck(1) == first_sha
    assert guard.recheck(3) == first_sha
    assert len(calls) == 2

    observed["binding"] = _runtime_binding_payload(bundle_id="9" * 64)
    with pytest.raises(RuntimeGateError, match="changed before ARM"):
        guard.recheck(5)
    assert len(calls) == 3

    observed["binding"] = expected
    assert guard.recheck(5) == first_sha
    assert len(calls) == 4
    with pytest.raises(RuntimeGateError, match="sequence regressed"):
        guard.recheck(3)
    assert len(calls) == 4


def test_full_runtime_guard_rehashes_instead_of_reusing_startup_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _runtime_binding_payload()
    startup_pointer = {"startup": "fully-verified"}
    rehashed_pointer = {"arm": "package-and-host-rehashed"}
    calls: list[object] = []

    def bind(*, runtime_pointer=None):
        calls.append(runtime_pointer)
        return expected

    monkeypatch.setattr(gate_module, "runtime_binding", bind)
    monkeypatch.setattr(
        gate_module,
        "load_runtime_pointer_integrity",
        lambda: rehashed_pointer,
    )
    guard = RuntimeEnvironmentBindingGuard.full(
        runtime_pointer=startup_pointer
    )
    guard.recheck(1)

    assert calls == [startup_pointer, rehashed_pointer]


def test_lightweight_runtime_guard_reloads_pointer_identity_for_new_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _runtime_binding_payload()
    identities = iter(({"generation": 1}, {"generation": 2}))
    identity_calls: list[object] = []
    binding_calls: list[object] = []

    def load_identity():
        identity = next(identities)
        identity_calls.append(identity)
        return identity

    def bind(*, runtime_pointer=None):
        binding_calls.append(runtime_pointer)
        return expected

    monkeypatch.setattr(gate_module, "load_runtime_pointer_identity", load_identity)
    monkeypatch.setattr(gate_module, "runtime_binding", bind)
    guard = RuntimeEnvironmentBindingGuard.lightweight()
    guard.recheck(1)

    assert identity_calls == [{"generation": 1}, {"generation": 2}]
    assert binding_calls == identity_calls


def test_identity_runtime_guard_reuses_startup_epoch_and_reloads_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _runtime_binding_payload()
    startup_pointer = {"generation": 1}
    reloaded_pointer = {"generation": 1}
    binding_calls: list[object] = []

    def bind(*, runtime_pointer=None):
        binding_calls.append(runtime_pointer)
        return expected

    monkeypatch.setattr(gate_module, "runtime_binding", bind)
    monkeypatch.setattr(
        gate_module,
        "load_runtime_pointer_identity",
        lambda: reloaded_pointer,
    )

    guard = RuntimeEnvironmentBindingGuard.identity(
        runtime_pointer=startup_pointer
    )
    guard.recheck(1)

    assert binding_calls == [startup_pointer, reloaded_pointer]


def test_fresh_identity_closed_observation_opens_the_actual_arm_gate(tmp_path: Path) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    lease_sha = lease.sha256
    observed = publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease_sha,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease_sha,
        release=release,
    )

    provider.observe_rtde(
        {
            "timestamp": 100.0,
            "runtime_state": 2,
            "safety_mode": 1,
            "output_int_register_35": 1,
            "output_int_register_36": 1234,
            "output_int_register_37": 5678,
        }
    )
    context = provider()

    assert observed["arm_permitted"] is True
    assert context is not None
    assert context.campaign_id == "campaign-a"


def test_readiness_observation_cannot_authorize_a_pending_arm(tmp_path: Path) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider)

    assert provider(_arm_binding(), connection_epoch=0) is None


def test_exact_command_bound_grant_authorizes_only_its_arm(tmp_path: Path) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    binding = _arm_binding()
    requested_at = time.time_ns()
    observed = publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
        arm_command=binding,
        command_observed_at_unix_ns=requested_at,
        rtde_row_wall_ns=requested_at + 1,
        connection_epoch=0,
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider)

    context = provider(binding, connection_epoch=0)

    assert observed["gate_kind"] == "arm_grant"
    assert context is not None
    assert context.grant_id == observed["grant_id"]
    assert context.mailbox_sha256 == binding["mailbox_sha256"]
    assert context.command_seq == binding["command_seq"]


def test_cached_arm1_grant_is_invalidated_immediately_for_arm2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    arm1 = _arm_binding()
    arm2 = _arm_binding(mailbox_sha256="e" * 64, trial_id=2, command_seq=2)
    requested_at = time.time_ns()
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
        arm_command=arm1,
        command_observed_at_unix_ns=requested_at,
        rtde_row_wall_ns=requested_at + 1,
        connection_epoch=0,
    )
    clock = [100.0]
    monkeypatch.setattr(gate_module.time, "monotonic", lambda: clock[0])
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider)
    reads = 0
    original = gate_module.read_strict_json

    def counted(path: Path, *, role: str) -> object:
        nonlocal reads
        reads += 1
        return original(path, role=role)

    monkeypatch.setattr(gate_module, "read_strict_json", counted)
    assert provider(arm1, connection_epoch=0) is not None
    reads_after_arm1 = reads

    assert provider(arm2, connection_epoch=0) is None
    assert reads > reads_after_arm1


def test_lightweight_runtime_binding_change_blocks_arm2_before_context(
    tmp_path: Path,
    _stable_managed_runtime_binding: dict[str, object],
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    arm1 = _arm_binding()
    arm2 = _arm_binding(mailbox_sha256="e" * 64, trial_id=2, command_seq=3)
    requested_at = time.time_ns()
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
        arm_command=arm1,
        command_observed_at_unix_ns=requested_at,
        rtde_row_wall_ns=requested_at + 1,
        connection_epoch=0,
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider)
    assert provider(arm1, connection_epoch=0) is not None

    arm2_requested_at = time.time_ns()
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(ur_timestamp=101.0),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
        arm_command=arm2,
        command_observed_at_unix_ns=arm2_requested_at,
        rtde_row_wall_ns=arm2_requested_at + 1,
        connection_epoch=0,
    )
    _observe_provider(provider, timestamp=101.0)
    _stable_managed_runtime_binding["binding"] = _runtime_binding_payload(
        bundle_id="9" * 64
    )

    with pytest.raises(RuntimeGateError, match="changed before ARM"):
        provider(arm2, connection_epoch=0)


def test_same_sequence_with_different_mailbox_sha_is_a_hard_failure(
    tmp_path: Path,
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    arm1 = _arm_binding()
    conflicting = _arm_binding(mailbox_sha256="e" * 64)
    requested_at = time.time_ns()
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
        arm_command=arm1,
        command_observed_at_unix_ns=requested_at,
        rtde_row_wall_ns=requested_at + 1,
        connection_epoch=0,
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider)

    with pytest.raises(RuntimeGateError, match="command binding differs"):
        provider(conflicting, connection_epoch=0)


def test_arm_grant_cannot_cross_an_rtde_connection_epoch(tmp_path: Path) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    binding = _arm_binding()
    requested_at = time.time_ns()
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
        arm_command=binding,
        command_observed_at_unix_ns=requested_at,
        rtde_row_wall_ns=requested_at + 1,
        connection_epoch=0,
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider, timestamp=1.0, connection_epoch=1)

    assert provider(binding, connection_epoch=1) is None


def test_arm_grant_rejects_an_rtde_row_that_predates_the_request(tmp_path: Path) -> None:
    _root, _release, contract, lease, _lease_path, gate_path = _fixture(tmp_path)
    requested_at = time.time_ns()

    with pytest.raises(RuntimeGateError, match="predates the command request"):
        publish_arm_observation(
            gate_path.absolute(),
            lease=lease,
            lease_sha256=lease.sha256,
            bridge_pid=os.getpid(),
            dashboard=_dashboard(),
            rtde_row=_row(),
            contract=contract,
            csv_age_s=0.01,
            **_fresh_get_binding(),
            arm_command=_arm_binding(),
            command_observed_at_unix_ns=requested_at,
            rtde_row_wall_ns=requested_at - 1,
            connection_epoch=0,
        )


def test_command_bound_arm_accepts_old_content_bound_controller_get(
    tmp_path: Path,
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    binding = _arm_binding()
    requested_at = time.time_ns()
    old_get_at = requested_at - 86_400_000_000_000

    observed = publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(observed_at_unix_ns=old_get_at),
        arm_command=binding,
        command_observed_at_unix_ns=requested_at,
        rtde_row_wall_ns=requested_at + 1,
        connection_epoch=0,
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider)

    assert observed["arm_permitted"] is True
    assert provider(binding, connection_epoch=0) is not None


@pytest.mark.parametrize("command_bound", (False, True), ids=("readiness", "arm"))
def test_arm_gate_cache_ignores_controller_get_age(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_bound: bool,
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    binding = _arm_binding() if command_bound else None
    wall_clock = [time.time_ns()]
    monotonic_clock = [500.0]
    fresh_get_at = wall_clock[0] - 86_400_000_000_000
    monkeypatch.setattr(gate_module.time, "time_ns", lambda: wall_clock[0])
    monkeypatch.setattr(
        gate_module.time, "monotonic", lambda: monotonic_clock[0]
    )
    observation_args: dict[str, object] = {}
    if binding is not None:
        observation_args = {
            "arm_command": binding,
            "command_observed_at_unix_ns": wall_clock[0],
            "rtde_row_wall_ns": wall_clock[0] + 1,
        }
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(observed_at_unix_ns=fresh_get_at),
        connection_epoch=0,
        **observation_args,
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    _observe_provider(provider)

    if binding is None:
        lookup = provider
    else:
        lookup = lambda: provider(binding, connection_epoch=0)
    assert lookup() is not None
    wall_clock[0] += 86_400_000_000_000
    assert lookup() is not None


@pytest.mark.parametrize(
    ("dashboard", "row", "reason"),
    (
        (_dashboard("/programs/andyl/kunwei/step5/old_r009.urp"), _row(), "dashboard_loaded_identity"),
        (_dashboard(), _row(ur_output_int_register_36=999), "TP runtime identity mismatch"),
    ),
)
def test_wrong_loaded_or_runtime_identity_never_opens_arm(
    tmp_path: Path,
    dashboard: dict[str, str],
    row: dict[str, object],
    reason: str,
) -> None:
    _root, _release, contract, lease, _lease_path, gate_path = _fixture(tmp_path)
    if reason.startswith("TP"):
        with pytest.raises(RuntimeGateError, match=reason):
            publish_arm_observation(
                gate_path.absolute(),
                lease=lease,
                lease_sha256=lease.sha256,
                bridge_pid=os.getpid(),
                dashboard=dashboard,
                rtde_row=row,
                contract=contract,
                csv_age_s=0.01,
                **_fresh_get_binding(),
            )
    else:
        observed = publish_arm_observation(
            gate_path.absolute(),
            lease=lease,
            lease_sha256=lease.sha256,
            bridge_pid=os.getpid(),
            dashboard=dashboard,
            rtde_row=row,
            contract=contract,
            csv_age_s=0.01,
            **_fresh_get_binding(),
        )
        assert observed["arm_permitted"] is False
        assert reason in observed["reason_codes"]


def test_program_switch_after_preflight_and_stale_heartbeat_fail_closed(tmp_path: Path) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    payload = json.loads(gate_path.read_text(encoding="utf-8"))
    payload["observed_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=2)
    ).isoformat()
    atomic_json(gate_path, payload)

    with pytest.raises(RuntimeGateError, match="heartbeat is stale"):
        provider()


def test_revoke_blocks_repeated_arm_without_reinterpreting_authority(tmp_path: Path) -> None:
    root, release, _contract, lease, lease_path, gate_path = _fixture(tmp_path)
    revoke_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        reason_code="campaign_terminal",
    )
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )

    assert provider() is None


@pytest.mark.parametrize("gate_present", (False, True))
def test_arm_gate_hot_path_has_one_io_refresh_per_monotonic_watchdog_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_present: bool
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    if gate_present:
        publish_arm_observation(
            gate_path.absolute(),
            lease=lease,
            lease_sha256=lease.sha256,
            bridge_pid=os.getpid(),
            dashboard=_dashboard(),
            rtde_row=_row(),
            contract=contract,
            csv_age_s=0.01,
            **_fresh_get_binding(),
        )
    clock = [100.0]
    monkeypatch.setattr(gate_module.time, "monotonic", lambda: clock[0])
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )

    counts = {"json": 0, "sha": 0, "proc": 0}
    original_json = gate_module.read_strict_json
    original_sha = gate_module._sha256_file
    original_proc = gate_module.process_starttime

    def counted_json(path: Path, *, role: str) -> object:
        counts["json"] += 1
        return original_json(path, role=role)

    def counted_sha(path: Path, role: str) -> str:
        counts["sha"] += 1
        return original_sha(path, role)

    def counted_proc(pid: int) -> int:
        counts["proc"] += 1
        return original_proc(pid)

    monkeypatch.setattr(gate_module, "read_strict_json", counted_json)
    monkeypatch.setattr(gate_module, "_sha256_file", counted_sha)
    monkeypatch.setattr(gate_module, "process_starttime", counted_proc)

    first = provider()
    first_counts = dict(counts)
    assert (first is not None) is gate_present
    assert all(value > 0 for value in first_counts.values())
    for _ in range(2_000):
        assert provider() == first
    assert counts == first_counts

    clock[0] += ARM_GATE_WATCHDOG_INTERVAL_S - 0.000001
    assert provider() == first
    assert counts == first_counts
    clock[0] += 0.000002
    assert (provider() is not None) is gate_present
    assert all(counts[name] > first_counts[name] for name in counts)


@pytest.mark.parametrize(
    ("invalidated_binding", "message"),
    (
        ("lease", "campaign lease canonical digest differs"),
        ("pointer", "effective release changed during campaign"),
        ("manifest", "release manifest changed during campaign"),
        ("safety", "safety envelope changed during campaign"),
        ("source", "release source tools/runtime_source.py changed"),
        ("repository_source", "repository source repository_source.py changed"),
        ("launcher", "canonical launcher changed"),
    ),
)
def test_watchdog_invalidations_fail_closed_no_later_than_cache_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalidated_binding: str,
    message: str,
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
    )
    clock = [500.0]
    monkeypatch.setattr(gate_module.time, "monotonic", lambda: clock[0])
    provider = ArmGateProvider(
        root=root,
        gate_path=gate_path.absolute(),
        lease_path=lease_path.absolute(),
        lease_sha256=lease.sha256,
        release=release,
    )
    context = provider()
    assert context is not None

    if invalidated_binding == "lease":
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        payload["authorization_source"] = "changed"
        atomic_json(lease_path, payload)
    elif invalidated_binding == "pointer":
        payload = json.loads(
            (root / "config/step5d/current.json").read_text(encoding="utf-8")
        )
        payload["manifest_sha256"] = "d" * 64
        atomic_json(root / "config/step5d/current.json", payload)
    elif invalidated_binding == "manifest":
        manifest = root / release.manifest_path
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    elif invalidated_binding == "safety":
        (root / "config/step5d/releases/fake/safety_envelope.json").write_text(
            '{"safety":"changed"}\n', encoding="utf-8"
        )
    elif invalidated_binding == "source":
        (root / "tools/runtime_source.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
    elif invalidated_binding == "repository_source":
        (root / "repository_source.py").write_text(
            "REPOSITORY_VALUE = 2\n", encoding="utf-8"
        )
    else:
        (root / "scripts/step5d-autotune-v3.sh").write_text(
            "#!/bin/sh\nexit 1\n", encoding="utf-8"
        )

    clock[0] += ARM_GATE_WATCHDOG_INTERVAL_S / 2
    assert provider() == context
    clock[0] += ARM_GATE_WATCHDOG_INTERVAL_S / 2 + 0.000001
    with pytest.raises(RuntimeGateError, match=message):
        provider()


def test_noncurrent_candidate_cannot_bypass_current(
    tmp_path: Path,
) -> None:
    root, release, contract, lease, lease_path, gate_path = _fixture(tmp_path)
    deployed = json.loads(
        (root / "config/step5d/current.json").read_text(encoding="utf-8")
    )
    deployed["manifest_sha256"] = "d" * 64
    atomic_json(root / "config/step5d/current.json", deployed)
    publish_arm_observation(
        gate_path.absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge_pid=os.getpid(),
        dashboard=_dashboard(),
        rtde_row=_row(),
        contract=contract,
        csv_age_s=0.01,
        **_fresh_get_binding(),
    )
    with pytest.raises(RuntimeGateError, match="current release differs"):
        ArmGateProvider(
            root=root,
            gate_path=gate_path.absolute(),
            lease_path=lease_path.absolute(),
            lease_sha256=lease.sha256,
            release=release,
        )
