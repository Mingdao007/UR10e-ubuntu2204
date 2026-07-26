from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_coordinator as coordinator  # noqa: E402
from step5d_autotune_v3.launch_basis import make_launch_basis  # noqa: E402


def _basis(now_ns: int) -> dict[str, Any]:
    runtime_identity = {"protocol_version": 1, "digest_hi": 2, "digest_lo": 3}
    basis = make_launch_basis(
        release_manifest_sha256="a" * 64,
        runtime_identity_sha256=coordinator._sha256_json(runtime_identity),
        campaign_fingerprint="c" * 64,
        delivery_observation_sha256="d" * 64,
        owner_pid=123,
        owner_starttime=456,
        authority_epoch=7,
        launch_nonce="attempt",
        argv_sha256="e" * 64,
        effective_config_sha256="f" * 64,
        issued_at_unix_ns=now_ns - 1_000_000,
        expires_at_unix_ns=now_ns + 60_000_000_000,
    )
    basis["_runtime_identity"] = runtime_identity
    return basis


def _campaign_payload(basis: dict[str, Any], created_at: int) -> dict[str, Any]:
    result = {
        "ok": True,
        "campaign_id": "campaign-1",
        "campaign_epoch": 1,
        "campaign_fingerprint": basis["campaign_fingerprint"],
        "campaign_root": "/tmp/campaign",
        "campaign_binding_file": "/tmp/campaign-binding.json",
        "launch_profile_path": "/tmp/launch-profile.json",
        "launch_profile_sha256": "1" * 64,
        "machine_binding_status": "pending_exact_candidate_and_overlay_plans",
        "candidate_plan": "/tmp/candidate-plan.json",
        "trial_overlay_plan": "/tmp/trial-overlay-plan.json",
        "receiver_root": "/tmp/receiver",
    }
    return {
        **result,
        "schema": coordinator.CAMPAIGN_PREPARE_SCHEMA,
        "ok": True,
        "fresh": True,
        "created_at_unix_ns": created_at,
        "launch_basis_sha256": basis["basis_sha256"],
        "identity": {
            "campaign_id": result["campaign_id"],
            "campaign_epoch": result["campaign_epoch"],
            "campaign_fingerprint": basis["campaign_fingerprint"],
            "release_manifest_sha256": basis["release_manifest_sha256"],
            "runtime_identity_sha256": basis["runtime_identity_sha256"],
        },
        "result": result,
    }


def _preflight_payload(basis: dict[str, Any], created_at: int) -> dict[str, Any]:
    payload = {key: {} for key in coordinator.PREFLIGHT_REQUIRED_FIELDS}
    payload.update(
        {
            "schema": coordinator.PREFLIGHT_SCHEMA,
            "ok": True,
            "fresh": True,
            "created_at": datetime.fromtimestamp(created_at / 1_000_000_000, timezone.utc).isoformat(),
            "release_manifest_sha256": basis["release_manifest_sha256"],
            "tp_runtime_identity": basis["_runtime_identity"],
            "launch_basis_sha256": basis["basis_sha256"],
            "predicates": {"safety_normal": {"ok": True}},
        }
    )
    return payload


def test_shell_rebinds_copied_admission_to_content_addressed_index() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")
    assert "from step5d_autotune_v3.bridge_admission import admission_index_path" in source
    assert "admission_index_path(root, payload)" in source
    assert '"${admission}" "${EXPERIMENT_ROOT}"' in source


def test_campaign_worker_code_preserves_bounded_campaign_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basis = {
        "basis_sha256": "a" * 64,
        "campaign_fingerprint": "c" * 64,
        "release_manifest_sha256": "d" * 64,
        "runtime_identity_sha256": "e" * 64,
    }
    args = SimpleNamespace(
        experiment_root=ROOT,
        output_root=ROOT / "run-output",
        campaign_root=ROOT / "tmp-campaign",
        launch_basis=tmp_path / "launch-basis.json",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery-observation.json",
        owner_pid=123,
        owner_starttime=456,
    )
    launch_profile_path = tmp_path / "launch-profile.json"
    launch_profile_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        coordinator,
        "load_runtime_release",
        lambda _root: SimpleNamespace(program_id="step5d_strict_rnn_autotune_v3_r999"),
    )
    monkeypatch.setattr(
        coordinator,
        "release_payload_path",
        lambda *_args, **_kwargs: launch_profile_path,
    )
    commands = coordinator._lane_commands(args, basis)
    campaign_command = commands[0]
    assert "--campaign-fingerprint" in campaign_command
    fingerprint_index = campaign_command.index("--campaign-fingerprint")
    assert campaign_command[fingerprint_index + 1] == basis["campaign_fingerprint"]
    worker_code = coordinator._campaign_worker_code(basis)
    assert basis["campaign_fingerprint"] in worker_code


def test_coordinator_runtime_root_composes_with_live_bridge_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import run_step5d_autotune_v3_live as live

    experiment_root = tmp_path / "experiment"
    experiment_root.mkdir()
    output_root = tmp_path / "output"
    output_root.mkdir()
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}\n", encoding="utf-8")
    delivery_path = experiment_root / "delivery.json"
    delivery_path.write_text("delivery\n", encoding="utf-8")
    args = SimpleNamespace(
        experiment_root=experiment_root,
        admission=admission_path,
        authority_root=tmp_path / "authority",
        attempt_id="attempt",
        authority_epoch=7,
        owner_pid=123,
        owner_starttime=456,
        output_root=output_root,
        campaign_root=tmp_path / "campaign",
        delivery_observation=delivery_path,
        preflight=output_root / "preflight.json",
        launch_basis=output_root / "launch-basis.json",
        basis_ttl_s=60,
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
        launch_basis_sha256=None,
        campaign_prepare=output_root / "campaign-prepare.json",
    )
    release = SimpleNamespace(manifest_sha256="a" * 64, program_id="program")
    monkeypatch.setattr(coordinator, "load_runtime_release", lambda _root: release)
    monkeypatch.setattr(
        coordinator,
        "resolve_bridge_admission",
        lambda _root, release: (
            admission_path,
            {
                "campaign_fingerprint": "c" * 64,
                "delivery_observation": {"path": "delivery.json"},
            },
        ),
    )
    monkeypatch.setattr(
        coordinator.authority,
        "load_current",
        lambda _root: {
            "state": "ACTIVE",
            "attempt_id": "attempt",
            "sequence": 7,
            "owner": {"pid": 123, "starttime_ticks": 456},
        },
    )
    monkeypatch.setattr(coordinator, "release_payload_path", lambda *_args: tmp_path / "payload.json")
    monkeypatch.setattr(coordinator, "load_contract", lambda _path: {})
    monkeypatch.setattr(coordinator, "load_launch_profile", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        coordinator,
        "check_effective_config",
        lambda **_kwargs: {"effective_config": {"runtime_root": "unused"}},
    )
    monkeypatch.setattr(
        coordinator,
        "release_runtime_contract",
        lambda *_args: {"tp_runtime_identity": {"protocol_version": 1}},
    )

    basis = coordinator._basis(args)
    assert basis["basis_sha256"]
    attempt_args = SimpleNamespace(**vars(args))
    attempt_args.output_root = tmp_path / "output" / "attempt-0001"
    attempt_args.output_root.mkdir()
    attempt_args._coordinator_output_root = output_root
    admission = {"campaign_fingerprint": basis["campaign_fingerprint"]}
    monkeypatch.setattr(live, "validate_bridge_admission", lambda *_args, **_kwargs: admission)
    monkeypatch.setattr(
        live,
        "read_strict_json",
        lambda _path, *, role: admission if role == "bridge admission" else {"result": {}},
    )
    monkeypatch.setattr(live, "validate_delivery_observation_binding", lambda *_args, **_kwargs: basis["delivery_observation_sha256"])
    monkeypatch.setattr(
        coordinator,
        "_validate_campaign_prepare",
        lambda payload, _basis: payload,
    )
    attempt_args.launch_basis_sha256 = basis["basis_sha256"]
    checked_basis, checked_admission, campaign = live._validate_active_launch_identity(
        attempt_args, release
    )
    assert checked_basis["basis_sha256"] == basis["basis_sha256"]
    assert checked_admission == admission
    assert campaign == {"result": {}}
    runtime_root = live._validate_coordinator_runtime_root(attempt_args)
    bridge_run, bridge_runtime = live._create_bridge_runtime(runtime_root)
    assert bridge_run.is_dir()
    assert bridge_runtime.is_dir()
    with pytest.raises(live.LiveLaunchError, match="unexpected session state"):
        live._validate_coordinator_runtime_root(attempt_args)


@pytest.mark.parametrize("state", ["prepopulated", "symlink"])
def test_coordinator_runtime_root_rejects_reuse_or_unsafe_state(
    tmp_path: Path, state: str
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    runtime_root = output_root / "runtime"
    outside = tmp_path / "outside"
    outside.mkdir()
    if state == "prepopulated":
        runtime_root.mkdir()
        (runtime_root / "bridge").mkdir()
    else:
        runtime_root.symlink_to(outside, target_is_directory=True)
    args = SimpleNamespace(
        output_root=output_root,
        owner_pid=123,
        owner_starttime=456,
        attempt_id="attempt",
        authority_epoch=7,
    )
    with pytest.raises(RuntimeError, match="already exists|unsafe"):
        coordinator._create_coordinator_runtime_root(args)


@pytest.mark.parametrize("field", ["ok", "fresh", "launch_basis_sha256"])
def test_campaign_prepare_rejects_false_stale_or_wrong_basis(field: str) -> None:
    now = 1_000_000_000_000
    basis = _basis(now)
    payload = _campaign_payload(basis, now)
    if field == "launch_basis_sha256":
        payload[field] = "0" * 64
    else:
        payload[field] = False
    with pytest.raises(RuntimeError):
        coordinator._validate_campaign_prepare(payload, basis, now_ns=now)


def test_campaign_prepare_rejects_old_timestamp() -> None:
    now = 1_000_000_000_000
    basis = _basis(now)
    payload = _campaign_payload(basis, now - coordinator.RESULT_MAX_AGE_NS - 1)
    with pytest.raises(RuntimeError, match="stale|outside"):
        coordinator._validate_campaign_prepare(payload, basis, now_ns=now)


def test_preflight_requires_exact_schema_and_identity() -> None:
    now = 1_000_000_000_000
    basis = _basis(now)
    payload = _preflight_payload(basis, now)
    assert coordinator._validate_preflight(payload, basis, now_ns=now)["ok"] is True
    payload["tp_runtime_identity"] = {"protocol_version": 99}
    with pytest.raises(RuntimeError, match="runtime identity"):
        coordinator._validate_preflight(payload, basis, now_ns=now)


def test_malformed_campaign_cancels_sibling_and_revokes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = 1_000_000_000_000
    basis = _basis(now)
    args = SimpleNamespace(
        experiment_root=tmp_path / "experiment",
        admission=tmp_path / "admission.json",
        authority_root=tmp_path / "authority",
        attempt_id="attempt",
        authority_epoch=7,
        owner_pid=123,
        owner_starttime=456,
        output_root=tmp_path / "output",
        campaign_root=tmp_path / "campaign",
        delivery_observation=tmp_path / "delivery.json",
        preflight=tmp_path / "output" / "preflight.json",
        launch_basis=tmp_path / "output" / "basis.json",
        basis_ttl_s=60,
    )

    class Process:
        def __init__(self, code: int | None) -> None:
            self.code = code
            self.terminated = False

        def poll(self) -> int | None:
            return self.code

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            self.terminated = True

    campaign = Process(0)
    sibling = Process(None)
    revocations: list[str] = []

    monkeypatch.setattr(coordinator, "_basis", lambda _args: basis)
    monkeypatch.setattr(coordinator, "_lane_commands", lambda _args, _basis: [["campaign"], ["preflight"]])

    def fake_popen(command: list[str], **kwargs: Any) -> Process:
        if command == ["campaign"]:
            stream = kwargs["stdout"]
            stream.write('{"schema":"malformed"}\n')
            stream.flush()
            return campaign
        return sibling

    monkeypatch.setattr(coordinator.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(coordinator, "_revoke", lambda _args, reason: revocations.append(reason))

    with pytest.raises(RuntimeError, match="schema"):
        coordinator.run(args)
    assert sibling.terminated is True
    assert revocations == ["failed"]


def test_revoke_requires_success_and_confirms_exact_fence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args = SimpleNamespace(
        authority_root=tmp_path / "authority",
        attempt_id="attempt",
        owner_pid=123,
        owner_starttime=456,
    )
    owner = {"pid": 123, "starttime_ticks": 456}
    active = {"state": "ACTIVE", "attempt_id": "attempt", "owner": owner, "sequence": 7}
    revoked = {**active, "state": "REVOKED", "reason": "failed", "sequence": 8}
    states = iter([active, revoked])
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(coordinator.authority, "load_current", lambda _root: next(states))

    def fake_run(_command: list[str], **kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(stdout="8\n")

    monkeypatch.setattr(coordinator.subprocess, "run", fake_run)
    coordinator._revoke(args, "failed")
    assert calls == [{"check": True, "capture_output": True, "text": True}]


def test_revoke_command_failure_is_hard_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args = SimpleNamespace(
        authority_root=tmp_path / "authority",
        attempt_id="attempt",
        owner_pid=123,
        owner_starttime=456,
    )
    active = {"state": "ACTIVE", "attempt_id": "attempt", "owner": {"pid": 123, "starttime_ticks": 456}, "sequence": 7}
    monkeypatch.setattr(coordinator.authority, "load_current", lambda _root: active)
    monkeypatch.setattr(
        coordinator.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("command failed")),
    )
    with pytest.raises(RuntimeError, match="command failed"):
        coordinator._revoke(args, "failed")
