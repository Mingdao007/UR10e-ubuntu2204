from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "tools"))

if (
    os.environ.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1"
    and "kunwei_rtde_bridge" not in sys.modules
):
    from step5d_v3_parser_ci_stubs import install as install_parser_ci_stubs

    install_parser_ci_stubs()

import preflight_step5d_autotune_v3 as preflight  # noqa: E402
import prepare_step5d_autotune_launch as launch_preparer  # noqa: E402
import run_step5d_autotune_v3_live as live  # noqa: E402
import run_step5d_autotune_v3_coordinator as coordinator  # noqa: E402
from step5d_autotune_v3.release_identity import (  # noqa: E402
    LAUNCH_PROFILE_PATH,
    SAFETY_ENVELOPE_PATH,
    ReleaseIdentityError,
    release_payload_path,
)


INITIAL_MANIFEST_PATH = "config/step5d/parameter_receiver_initial.json"
WORKTREE_ROOT = ROOT.parents[1]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _runtime_pointer() -> dict[str, object]:
    return {
        "bundle_id": "a" * 64,
        "attestation_sha256": "b" * 64,
        "profiles": {
            profile: {
                "root": f"/runtime/{profile}",
                "python_executable": f"/runtime/{profile}/bin/python",
                "environment_id": ("c" if profile == "control" else "d") * 64,
                "record_tree_sha256": ("e" if profile == "control" else "f")
                * 64,
                "profile_tree_sha256": ("1" if profile == "control" else "2")
                * 64,
            }
            for profile in ("control", "optimizer")
        },
    }


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _release_bundle(root: Path) -> tuple[Any, dict[str, Path], dict[str, bytes]]:
    manifest_bytes = b'{"schema":"immutable-routing-test"}\n'
    manifest_sha = _sha256(manifest_bytes)
    manifest_path = root / f"config/step5d/releases/{manifest_sha}/manifest.json"
    _write(manifest_path, manifest_bytes)

    contents = {
        LAUNCH_PROFILE_PATH: b'{"source":"immutable-launch"}\n',
        SAFETY_ENVELOPE_PATH: b'{"source":"immutable-contract"}\n',
        INITIAL_MANIFEST_PATH: b'{"source":"immutable-initial-ten"}\n',
    }
    paths = {
        relative: manifest_path.parent / relative for relative in contents
    }
    for relative, value in contents.items():
        _write(paths[relative], value)
        _write(root / relative, f"tampered-mirror:{relative}\n".encode())

    release = SimpleNamespace(
        manifest_path=manifest_path.relative_to(root).as_posix(),
        manifest_sha256=manifest_sha,
        generated_files={
            LAUNCH_PROFILE_PATH: _sha256(contents[LAUNCH_PROFILE_PATH]),
            INITIAL_MANIFEST_PATH: _sha256(contents[INITIAL_MANIFEST_PATH]),
        },
        safety_envelope={
            "path": SAFETY_ENVELOPE_PATH,
            "sha256": _sha256(contents[SAFETY_ENVELOPE_PATH]),
        },
        artifacts={},
        program_id="step5d_strict_rnn_autotune_v3_r999",
    )
    return release, paths, contents


def _seed_git_reference(source_root: Path, fixture_root: Path) -> None:
    source = source_root / ".git"
    if source.is_file():
        raw = source.read_text(encoding="utf-8").splitlines()
        if not raw or not raw[0].startswith("gitdir:"):
            raise ValueError(f"Invalid Git worktree metadata: {source}")
        target = Path(raw[0][len("gitdir:") :].strip())
        if not target.is_absolute():
            target = source.parent / target
        content = f"gitdir: {target.resolve()}\n"
    elif source.is_dir():
        content = f"gitdir: {source.resolve(strict=True)}\n"
    else:
        raise FileNotFoundError(f"Git metadata is unavailable: {source}")
    (fixture_root / ".git").write_text(content, encoding="utf-8")


def _seed_campaign_source_contract(root: Path) -> None:
    from step5d_autotune_backend import Step5dV35Backend

    def _copy_path(src: Path, dst: Path) -> None:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    for path in Step5dV35Backend.SOURCE_PATHS:
        _copy_path(ROOT / path, root / path)
    for path in Step5dV35Backend.CONFIG_PATHS:
        _copy_path(ROOT / path, root / path)
    _copy_path(
        ROOT / "config" / "step5d" / "manifests",
        root / "config" / "step5d" / "manifests",
    )
    _copy_path(
        ROOT / "programs" / "step5" / "step5d",
        root / "programs" / "step5" / "step5d",
    )
    _seed_git_reference(WORKTREE_ROOT, root)


def test_release_payload_uses_bundle_when_mutable_mirrors_are_tampered(
    tmp_path: Path,
) -> None:
    release, paths, contents = _release_bundle(tmp_path)

    for relative in (
        LAUNCH_PROFILE_PATH,
        SAFETY_ENVELOPE_PATH,
        INITIAL_MANIFEST_PATH,
    ):
        resolved = release_payload_path(tmp_path, release, relative)
        assert resolved == paths[relative].resolve()
        assert resolved.read_bytes() == contents[relative]
        assert (tmp_path / relative).read_bytes() != contents[relative]


def test_release_payload_tamper_fails_closed(tmp_path: Path) -> None:
    release, paths, _ = _release_bundle(tmp_path)
    paths[LAUNCH_PROFILE_PATH].write_bytes(b"tampered immutable payload\n")

    with pytest.raises(ReleaseIdentityError, match="payload fingerprint drifted"):
        release_payload_path(tmp_path, release, LAUNCH_PROFILE_PATH)


def test_release_payload_rejects_existing_but_unbound_path(tmp_path: Path) -> None:
    release, paths, _ = _release_bundle(tmp_path)
    unbound = paths[LAUNCH_PROFILE_PATH].parent / "unbound.json"
    unbound.write_text("{}\n", encoding="utf-8")
    relative = unbound.relative_to(
        tmp_path / f"config/step5d/releases/{release.manifest_sha256}"
    ).as_posix()

    with pytest.raises(ReleaseIdentityError, match="not manifest-bound"):
        release_payload_path(tmp_path, release, relative)


def test_preflight_routes_both_configs_through_immutable_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, paths, contents = _release_bundle(tmp_path)
    observed: dict[str, Any] = {}

    class RoutingObserved(RuntimeError):
        pass

    def load_contract(path: Path) -> object:
        observed["contract_path"] = path
        observed["contract_bytes"] = path.read_bytes()
        return observed

    def load_profile(path: Path, *, contract: object | None = None, expected_tp_program_id: str | None = None) -> Any:
        observed["launch_path"] = path
        observed["launch_bytes"] = path.read_bytes()
        observed["profile_contract"] = contract
        return SimpleNamespace(fingerprint="immutable-profile")

    def stop_after_config_routing(*_args: Any, **kwargs: Any) -> list[str]:
        observed["bridge_contract"] = kwargs.get("contract")
        observed["bridge_profile"] = kwargs.get("launch_profile")
        raise RoutingObserved

    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "load_runtime_release", lambda _root: release)
    monkeypatch.setattr(
        preflight, "load_delivery_observation", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(preflight, "release_runtime_contract", lambda *_args: {})
    monkeypatch.setattr(preflight, "load_contract", load_contract)
    monkeypatch.setattr(preflight, "load_launch_profile", load_profile)
    monkeypatch.setattr(preflight, "build_bridge_argv", stop_after_config_routing)

    args = SimpleNamespace(
        delivery_observation=tmp_path / "delivery.json",
        mailbox=tmp_path / "runtime/command.json",
    )
    with pytest.raises(RoutingObserved):
        preflight.run_preflight(args)

    assert observed["contract_path"] == paths[SAFETY_ENVELOPE_PATH].resolve()
    assert observed["launch_path"] == paths[LAUNCH_PROFILE_PATH].resolve()
    assert observed["contract_bytes"] == contents[SAFETY_ENVELOPE_PATH]
    assert observed["launch_bytes"] == contents[LAUNCH_PROFILE_PATH]
    assert observed["profile_contract"] is observed
    assert observed["bridge_contract"] is observed
    assert observed["bridge_profile"].fingerprint == "immutable-profile"


def test_live_routes_both_configs_through_immutable_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_step5d_autotune_v3_coordinator as coordinator

    release, paths, contents = _release_bundle(tmp_path)
    observed: dict[str, Any] = {}

    class RoutingObserved(RuntimeError):
        pass

    def load_contract(path: Path) -> object:
        observed["contract_path"] = path
        observed["contract_bytes"] = path.read_bytes()
        return observed

    def load_profile(path: Path, *, contract: object | None = None, expected_tp_program_id: str | None = None) -> Any:
        observed["launch_path"] = path
        observed["launch_bytes"] = path.read_bytes()
        observed["profile_contract"] = contract
        return SimpleNamespace(fingerprint="immutable-profile")

    monkeypatch.setattr(live, "ROOT", tmp_path)
    monkeypatch.setattr(
        live, "require_runtime_profile", lambda _profile: _runtime_pointer()
    )
    monkeypatch.setattr(live, "load_runtime_release", lambda _root: release)
    monkeypatch.setattr(live, "load_delivery_observation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(live, "load_contract", load_contract)
    monkeypatch.setattr(live, "load_launch_profile", load_profile)
    monkeypatch.setattr(
        live,
        "check_effective_config",
        lambda **_kwargs: {"effective_config": {"robot_host": "192.0.2.1"}},
    )
    monkeypatch.setattr(
        live,
        "build_bridge_argv",
        lambda *_args, **_kwargs: ["python", "bridge"],
    )
    output_root = tmp_path / "run"
    output_root.mkdir()
    plan_path = tmp_path / "campaign-plan.json"
    overlay_path = tmp_path / "trial-overlay-plan.json"
    plan_path.write_text('{"revision":1}\n', encoding="utf-8")
    overlay_path.write_text('{"revision":1}\n', encoding="utf-8")
    basis_path = tmp_path / "launch-basis.json"
    delivery_path = tmp_path / "delivery.json"
    admission_path = tmp_path / "admission.json"
    campaign_prepare_path = tmp_path / "campaign-prepare.json"
    for path in (basis_path, delivery_path, admission_path, campaign_prepare_path):
        path.write_text("{}\n", encoding="utf-8")
    args = SimpleNamespace(
        output_root=output_root,
        preflight=tmp_path / "preflight.json",
        delivery_observation=delivery_path,
        admission=admission_path,
        authority_epoch=7,
        launch_basis=basis_path,
        launch_basis_sha256="b" * 64,
        campaign_prepare=campaign_prepare_path,
        campaign_root=tmp_path / "campaign",
        launch_profile=tmp_path / LAUNCH_PROFILE_PATH,
        owner_pid=1,
        owner_starttime=2,
        attempt_id="attempt-immutable-routing",
        canonical_owner_pid=1,
        canonical_owner_starttime=2,
    )
    coordinator._create_coordinator_runtime_root(args)
    monkeypatch.setattr(
        live,
        "_validate_active_launch_identity",
        lambda *_args, **_kwargs: (
            {
                "launch_nonce": "attempt-immutable-routing",
                "basis_sha256": "b" * 64,
                "delivery_observation_sha256": "d" * 64,
                "authority_epoch": 7,
            },
            {"campaign_fingerprint": "c" * 64},
            {
                "result": {
                    "candidate_plan": str(plan_path),
                    "trial_overlay_plan": str(overlay_path),
                    "campaign_id": "campaign-immutable-routing",
                    "campaign_epoch": 1,
                    "campaign_fingerprint": "c" * 64,
                    "receiver_root": str(tmp_path / "receiver"),
                }
            },
        ),
    )

    def stop_after_config_routing(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise RoutingObserved

    monkeypatch.setattr(live, "_validate_preflight", stop_after_config_routing)
    with pytest.raises(RoutingObserved):
        live._run_live_session(args, _runtime_pointer())

    assert observed["contract_path"] == paths[SAFETY_ENVELOPE_PATH].resolve()
    assert observed["launch_path"] == paths[LAUNCH_PROFILE_PATH].resolve()
    assert observed["contract_bytes"] == contents[SAFETY_ENVELOPE_PATH]
    assert observed["launch_bytes"] == contents[LAUNCH_PROFILE_PATH]
    assert observed["profile_contract"] is observed


def test_live_prepare_only_ignores_mutable_launch_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, paths, contents = _release_bundle(tmp_path)
    observed: dict[str, Any] = {}

    def load_contract(path: Path) -> object:
        observed["contract_path"] = path
        observed["contract_bytes"] = path.read_bytes()
        return observed

    def load_profile(path: Path, *, contract: object | None = None, expected_tp_program_id: str | None = None) -> Any:
        observed["launch_path"] = path
        observed["launch_bytes"] = path.read_bytes()
        observed["profile_contract"] = contract
        return SimpleNamespace(fingerprint="immutable-profile")

    monkeypatch.setattr(live, "ROOT", tmp_path)
    monkeypatch.setattr(live, "_require_canonical_launcher", lambda: None)
    monkeypatch.setattr(live, "_parent_death_guard", lambda *_args: None)
    monkeypatch.setattr(
        live, "require_runtime_profile", lambda _profile: _runtime_pointer()
    )
    monkeypatch.setattr(live, "load_current_release", lambda _root: release)
    monkeypatch.setattr(live, "load_contract", load_contract)
    monkeypatch.setattr(live, "load_launch_profile", load_profile)
    monkeypatch.setattr(
        launch_preparer,
        "prepare",
        lambda *_args, **_kwargs: {"ok": True},
    )

    result = live.main(
        [
            "--prepare-only",
            "--campaign-root",
            str(tmp_path / "campaign"),
            "--launch-profile",
            str(tmp_path / LAUNCH_PROFILE_PATH),
            "--canonical-owner-pid",
            "1",
            "--canonical-owner-starttime",
            "1",
        ]
    )

    assert result == 0
    assert observed["contract_path"] == paths[SAFETY_ENVELOPE_PATH].resolve()
    assert observed["launch_path"] == paths[LAUNCH_PROFILE_PATH].resolve()
    assert observed["contract_bytes"] == contents[SAFETY_ENVELOPE_PATH]
    assert observed["launch_bytes"] == contents[LAUNCH_PROFILE_PATH]
    assert observed["profile_contract"] is observed


def test_no_arm_live_composition_uses_one_real_campaign_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from step5d_autotune_v3 import bridge_admission

    experiment = tmp_path / "experiment"
    _seed_campaign_source_contract(experiment)
    release, paths, _contents = _release_bundle(experiment)
    (experiment / "config/step5d").mkdir(parents=True, exist_ok=True)
    (experiment / "config/step5d/current.json").write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/current-release-pointer-v1",
                "manifest_path": release.manifest_path,
                "manifest_sha256": release.manifest_sha256,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    expected_program = (
        "/programs/andyl/kunwei/step5/"
        "step5d_strict_rnn_autotune_v3_r012.urp"
    )
    delivery_observation = experiment / "runs/delivery-observation.json"
    delivery_observation.parent.mkdir(parents=True, exist_ok=True)
    delivery_observation.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/delivery-observation-v1",
                "transaction_id": "0123456789abcdef0123456789abcdef",
                "fresh_controller_checked_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    publication_lineage = experiment / "runs/publication-lineage.json"
    publication_lineage.write_text('{"ok":true}\n', encoding="utf-8")

    monkeypatch.setattr(
        bridge_admission,
        "load_runtime_release",
        lambda _root: release,
    )
    monkeypatch.setattr(
        bridge_admission,
        "release_runtime_contract",
        lambda *_args: {"expected_loaded_program": expected_program},
    )
    monkeypatch.setattr(
        bridge_admission,
        "release_contract_reference",
        lambda *_args: {
            "certificate_path": "runs/certificate.json",
            "certificate_sha256": "c" * 64,
            "evidence_path": "runs/contract.json",
            "evidence_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        bridge_admission,
        "resolve_publication_lineage",
        lambda _root, **_kwargs: (publication_lineage, {"ok": True}),
    )
    monkeypatch.setattr(
        bridge_admission,
        "resolve_delivery_observation",
        lambda _root, **_kwargs: (
            delivery_observation,
            {"transaction_id": "0123456789abcdef0123456789abcdef"},
        ),
    )
    monkeypatch.setattr(
        bridge_admission,
        "load_delivery_observation",
        lambda *_args, **_kwargs: {"transaction_id": "0123456789abcdef0123456789abcdef"},
    )
    monkeypatch.setattr(bridge_admission, "release_robot_host", lambda *_args: "robot")
    monkeypatch.setattr(
        bridge_admission,
        "dashboard_exchange",
        lambda _host, _requested, **_kwargs: {
            "programState": f"STOPPED {expected_program}",
            "get loaded program": f"Loaded program: {expected_program}",
        },
    )

    admission = bridge_admission.observe_bridge_admission(
        experiment,
        dashboard_reader=lambda _host, _requested, **_kwargs: {
            "programState": f"STOPPED {expected_program}",
            "get loaded program": f"Loaded program: {expected_program}",
        },
    )
    admission_path = bridge_admission.write_indexed_bridge_admission(experiment, admission)
    assert admission["campaign_fingerprint"] and len(admission["campaign_fingerprint"]) == 64

    coordinator_output = tmp_path / "coordinator-output"
    authority_root = experiment / "authority"
    campaign_root = experiment / "campaign"

    monkeypatch.setattr(coordinator, "load_runtime_release", lambda _root: release)
    monkeypatch.setattr(
        coordinator,
        "release_payload_path",
        lambda _root, _release, relative: experiment / relative,
    )
    monkeypatch.setattr(
        coordinator,
        "resolve_bridge_admission",
        lambda *_args, **_kwargs: (admission_path, admission),
    )
    monkeypatch.setattr(
        coordinator.authority,
        "load_current",
        lambda _root: {
            "state": "ACTIVE",
            "attempt_id": "attempt-no-arm-composition",
            "sequence": 7,
            "owner": {"pid": 123, "starttime_ticks": 456},
        },
    )
    monkeypatch.setattr(
        coordinator,
        "load_contract",
        lambda _path: {"deployment_identity": "synthetic"},
    )
    monkeypatch.setattr(
        coordinator,
        "load_launch_profile",
        lambda *_args, **_kwargs: {"execution_profile_id": "nf100-slew050-a050"},
    )
    monkeypatch.setattr(
        coordinator,
        "check_effective_config",
        lambda **_kwargs: {"effective_config": {"robot_host": "192.0.2.1"}},
    )
    monkeypatch.setattr(
        coordinator,
        "release_runtime_contract",
        lambda *_args, **_kwargs: {"tp_runtime_identity": {"protocol_version": 1}},
    )
    monkeypatch.setattr(live, "ROOT", experiment)
    monkeypatch.setattr(
        launch_preparer,
        "load_current_release",
        lambda _root: release,
    )
    monkeypatch.setattr(
        launch_preparer,
        "release_payload_path",
        lambda _root, _release, relative: experiment / relative,
    )

    coordinator_args = SimpleNamespace(
        experiment_root=experiment,
        admission=admission_path,
        authority_root=authority_root,
        attempt_id="attempt-no-arm-composition",
        authority_epoch=7,
        owner_pid=123,
        owner_starttime=456,
        output_root=coordinator_output,
        campaign_root=campaign_root,
        delivery_observation=delivery_observation,
        preflight=tmp_path / "preflight.json",
        launch_basis=tmp_path / "launch-basis.json",
        launch_basis_sha256=None,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        basis_ttl_s=60,
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
    )
    coordinator_output.mkdir(parents=True, exist_ok=True)
    basis = coordinator._basis(coordinator_args)

    prepare_args = launch_preparer.LaunchPreparationRequest(
        experiment_root=experiment,
        campaign_root=campaign_root,
        binding_file=experiment / "campaign-binding.json",
        binding_source="canonical-no-arm-composition",
        launch_profile_path=paths[LAUNCH_PROFILE_PATH],
        candidate_batch_size=5,
        rolling_plan=False,
        campaign_fingerprint=basis["campaign_fingerprint"],
    )
    campaign_prepare = launch_preparer.prepare(prepare_args)
    campaign_prepare_payload = coordinator._build_campaign_prepare_payload(
        basis=basis,
        result=campaign_prepare,
        created_at_unix_ns=basis["issued_at_unix_ns"],
    )
    campaign_prepare_path = tmp_path / "campaign-prepare.json"
    campaign_prepare_path.write_text(
        json.dumps(campaign_prepare_payload, separators=(",", ":")),
        encoding="utf-8",
    )

    attempt_args = SimpleNamespace(**vars(coordinator_args))
    attempt_args.output_root = coordinator_output / "attempt-0001"
    attempt_args._coordinator_output_root = coordinator_output
    attempt_args.launch_basis_sha256 = basis["basis_sha256"]
    attempt_args.campaign_prepare = campaign_prepare_path
    attempt_args.output_root.mkdir(parents=True, exist_ok=True)

    checked_basis, checked_admission, checked_campaign = live._validate_active_launch_identity(
        attempt_args,
        release,
    )
    assert checked_basis["campaign_fingerprint"] == admission["campaign_fingerprint"]
    assert checked_admission["campaign_fingerprint"] == checked_basis["campaign_fingerprint"]
    assert (
        checked_campaign["identity"]["campaign_fingerprint"]
        == checked_basis["campaign_fingerprint"]
    )
    assert (
        checked_campaign["result"]["campaign_fingerprint"]
        == checked_basis["campaign_fingerprint"]
    )
