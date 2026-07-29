from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as builder  # noqa: E402
import promote_step5d_r009_atomic_release as promoter  # noqa: E402
from step5d_autotune_v3.admission import verify_first_row_admission  # noqa: E402
from step5d_autotune_v3.atomic_release import (  # noqa: E402
    AtomicReleaseError,
    AtomicReleasePublisher,
    canonical_bytes,
)
from step5d_autotune_v3.release_identity import (  # noqa: E402
    CURRENT_POINTER_SCHEMA,
    RELEASE_MANIFEST_SCHEMA,
    REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS,
    REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS,
    ReleaseIdentityError,
    identity_from_manifest,
    load_current_release,
    load_current_release_for_compatible_readback,
    release_runtime_environment_binding,
)
from step5d_autotune_v3.release_verifier import (  # noqa: E402
    ReleaseVerificationError,
    verify_release_manifest,
)
from step5d_autotune_v3.campaign_prepare import prepare_campaign  # noqa: E402
from step5d_autotune_v3.profile import contract_sha256, load_contract  # noqa: E402
from step5d_autotune_v3.runtime_profile import load_launch_profile  # noqa: E402
from step5d_autotune_v3.runtime_identity import (  # noqa: E402
    bind_final_script,
    canonicalize_script_identity,
    derive_runtime_identity,
    runtime_identity_assignment_block,
)


PROGRAM = "step5d_strict_rnn_autotune_v3_r999"


def _sha(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _rebind_script(script: str) -> str:
    basis, _ = canonicalize_script_identity(script)
    identity = derive_runtime_identity(
        program_id=PROGRAM,
        protocol_id="v3_full_home_rolling_arm_v1",
        script_identity_basis=basis,
    )
    start = script.index("global codex_step5d_runtime_protocol_version = ")
    end = script.index("\n\ndef codex_step5d_publish_runtime_identity():", start)
    return script[:start] + runtime_identity_assignment_block(identity) + script[end:]


def _release_fixture(
    root: Path,
    *,
    bad_registers: bool = False,
    bad_write_order: bool = False,
    execution_profile_id: str = "nf100-slew050-a050",
) -> Path:
    stamp = "2026-07-23T0000HKT_" + PROGRAM.upper()
    script = builder.build_package_script(
        stamp,
        program_id=PROGRAM,
        execution_profile_id=execution_profile_id,
    )
    numeric = {
        "input_integer_registers": list(range(24, 32)),
        "output_integer_registers": list(range(24, 38)),
    }
    if bad_registers:
        script = script.replace("read_input_integer_register(31)", "read_input_integer_register(30)")
        script = script.replace("write_output_integer_register(34,", "write_output_integer_register(33,")
        numeric = {
            "input_integer_registers": list(range(24, 31)),
            "output_integer_registers": list(range(24, 38)),
        }
    if bad_write_order:
        script = script.replace(
            "  write_output_integer_register(26, state)\n"
            "  write_output_integer_register(30, consumed_command_seq)",
            "  write_output_integer_register(30, consumed_command_seq)\n"
            "  write_output_integer_register(26, state)",
        )
    if bad_registers or bad_write_order:
        script = _rebind_script(script)
    txt = builder.build_txt(stamp, program_id=PROGRAM).encode()
    script_bytes = script.encode()
    urp = builder.v1.build_urp(script, PROGRAM, builder.CONTROLLER_DIR)
    artifact_bytes = {".script": script_bytes, ".txt": txt, ".urp": urp}
    artifacts: dict[str, dict[str, str]] = {}
    for extension, encoded in artifact_bytes.items():
        relative = f"programs/step5/{PROGRAM}{extension}"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        artifacts[extension] = {"path": relative, "sha256": _sha(encoded)}

    source_fingerprints: dict[str, str] = {}
    for relative in sorted(REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS):
        source_path = root / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"# canonical source: {relative}\n", encoding="utf-8")
        source_fingerprints[relative] = _sha(source_path.read_bytes())
    repository_source_fingerprints: dict[str, str] = {}
    for relative in sorted(REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS):
        source_path = root / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"# repository source: {relative}\n", encoding="utf-8")
        repository_source_fingerprints[relative] = _sha(source_path.read_bytes())
    triplet = {extension: reference["sha256"] for extension, reference in artifacts.items()}
    _, tp_runtime_identity = bind_final_script(
        script,
        program_id=PROGRAM,
        protocol_id="v3_full_home_rolling_arm_v1",
    )
    generated_payloads = {
        f"programs/step5/{PROGRAM}.deploy-manifest.json": {
            "schema_version": 2,
            "basename": PROGRAM,
            "controller_directory": "/programs/andyl/kunwei/step5",
            "tp_runtime_identity": tp_runtime_identity,
            "artifacts": [
                {
                    "filename": f"{PROGRAM}{extension}",
                    "source": f"{PROGRAM}{extension}",
                    "sha256": triplet[extension],
                }
                for extension in (".script", ".txt", ".urp")
            ],
        },
        f"programs/step5/{PROGRAM}.numeric-sanity.json": numeric,
        f"config/step5d/manifests/{PROGRAM}/local_candidate.json": {
            "program": PROGRAM,
            "triplet_sha256": triplet,
        },
        "config/step5/step5d_autotune_v3_launch_profile.json": {
            "launch_profile": "fixture"
        },
    }
    generated_files: dict[str, str] = {}
    for relative, payload in generated_payloads.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_bytes(payload))
        generated_files[relative] = _sha(path.read_bytes())
    deploy_sha256 = generated_files[f"programs/step5/{PROGRAM}.deploy-manifest.json"]
    numeric_sha256 = generated_files[f"programs/step5/{PROGRAM}.numeric-sanity.json"]
    local_triplet = str(artifacts[".script"]["path"]).removesuffix(".script")
    controller_target = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"
    readback = {
        "status": "controller read-back verified",
        "verified": True,
        "program": PROGRAM,
        "controller_target": controller_target,
        "triplet_sha256": triplet,
    }
    readback_path = root / "config/readback.json"
    readback_path.write_bytes(canonical_bytes(readback))

    current_stage = {
        "controller_readback_manifest": "config/readback.json",
        "controller_readback_manifest_sha256": _sha(readback_path.read_bytes()),
        "controller_script": controller_target.removesuffix(".urp") + ".script",
        "controller_target": controller_target,
        "delivery_manifest": "config/readback.json",
        "evidence": {"sha256": triplet},
        "local_candidate": {
            "manifest": f"config/step5d/manifests/{PROGRAM}/local_candidate.json",
            "deploy_manifest_sha256": deploy_sha256,
            "numeric_sanity_sha256": numeric_sha256,
            "program": PROGRAM,
            "triplet_sha256": triplet,
        },
        "local_triplet": local_triplet,
        "program": "step5d_strict_rnn_autotune_v3",
        "sha256": triplet,
        "status": "step5d_autotune_v3_r010_controller_readback_verified",
    }
    stage_table = {
        "stages": [
            {
                "id": "step5d_strict_rnn_autotune_v3",
                "current_binding": {
                    "program": "step5d_strict_rnn_autotune_v3",
                    "controller_target": controller_target,
                },
                "operator_lifecycle": {"expected_program": controller_target},
                "package_delivery": {
                    "controller_readback_manifest": "config/readback.json",
                    "controller_readback_manifest_sha256": _sha(
                        readback_path.read_bytes()
                    ),
                    "controller_target": controller_target,
                    "local_triplet": local_triplet,
                    "program_basename": PROGRAM,
                    "sha256": triplet,
                    "tp_fingerprint": deploy_sha256,
                    "local_candidate": {
                        "deploy_manifest_sha256": deploy_sha256,
                        "local_triplet": local_triplet,
                        "numeric_sanity_sha256": numeric_sha256,
                        "program_basename": PROGRAM,
                        "sha256": triplet,
                    },
                },
            }
        ]
    }
    current_stage_path = root / "config/current_stage.json"
    current_stage_path.parent.mkdir(parents=True, exist_ok=True)
    current_stage_path.write_bytes(canonical_bytes(current_stage))
    stage_table_path = root / "config/step5_stage_table.json"
    stage_table_path.write_bytes(canonical_bytes(stage_table))
    safety_path = root / "config/step5/step5d_autotune_v3_control_contract.json"
    safety_path.parent.mkdir(parents=True, exist_ok=True)
    safety_path.write_bytes(canonical_bytes({"safety_envelope": "fixture"}))
    for relative in source_fingerprints:
        source_fingerprints[relative] = _sha((root / relative).read_bytes())
    mirror_path = root / "config/mirror.json"
    mirror_path.write_text('{"program":"r010"}\n', encoding="utf-8")

    profile_bindings = {
        "nf100-slew050-a050": (0.1, 633),
        "nf500-slew250-a250": (0.5, 744),
        "nf1000-slew250-a250": (1.0, 844),
        "nf2000-slew250-a250": (2.0, 944),
        "nf5000-slew250-a250": (5.0, 1044),
    }
    normal_rate, integer_id = profile_bindings[execution_profile_id]
    manifest = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "identity": {
            "program_id": PROGRAM,
            "release_stage_id": "step5d_strict_rnn_autotune_v3",
            "control_profile_id": "step5d_strict_rnn_autotune_v1",
            "protocol_id": "v3_full_home_rolling_arm_v1",
            "normal_max_rate_rad_s": normal_rate,
            "execution_profile_id": execution_profile_id,
            "execution_profile_integer_id": integer_id,
        },
        "artifacts": artifacts,
        "controller_target": controller_target,
        "tp_runtime_identity": tp_runtime_identity,
        "safety_envelope": {
            "path": "config/step5/step5d_autotune_v3_control_contract.json",
            "sha256": _sha(safety_path.read_bytes()),
        },
        "runtime_environment": release_runtime_environment_binding(
            source_fingerprints
        ),
        "source_fingerprints": source_fingerprints,
        "generated_files": generated_files,
        "verification": {
            "canonical_verifier": "independent_script_urp_v3",
            "staged_bytes_required": True,
            "pointer_switched_last": True,
            "runtime_identity_derivation": (
                "canonical_script_identity_basis_sha256_plus_"
                "final_artifact_sha256_v1"
            ),
            "repository_source_root_depth": 0,
            "repository_source_fingerprints": repository_source_fingerprints,
        },
    }
    encoded = canonical_bytes(manifest)
    digest = _sha(encoded)
    manifest_path = root / f"config/step5d/releases/{digest}/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload_paths = [
        *(reference["path"] for reference in artifacts.values()),
        *generated_files,
        "config/step5/step5d_autotune_v3_control_contract.json",
    ]
    for relative in payload_paths:
        source = root / relative
        destination = manifest_path.parent / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    manifest_path.write_bytes(encoded)
    pointer = {
        "schema": CURRENT_POINTER_SCHEMA,
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_sha256": digest,
    }
    pointer_path = root / "config/step5d/current.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_bytes(pointer))
    return manifest_path


def test_independent_verifier_rejects_joint_generator_and_numeric_register_drift(
    tmp_path: Path,
) -> None:
    manifest = _release_fixture(tmp_path, bad_registers=True)
    with pytest.raises(ReleaseVerificationError, match="input-register contract"):
        verify_release_manifest(tmp_path, manifest)


def test_independent_verifier_rejects_non_atomic_tp_state_write_order(
    tmp_path: Path,
) -> None:
    manifest = _release_fixture(tmp_path, bad_write_order=True)
    with pytest.raises(ReleaseVerificationError, match="consumed-sequence commit"):
        verify_release_manifest(tmp_path, manifest)


def test_independent_verifier_accepts_release_without_cached_dynamic_readiness(
    tmp_path: Path,
) -> None:
    manifest = _release_fixture(tmp_path)

    report = verify_release_manifest(tmp_path, manifest)

    assert report["ok"] is True


def test_release_manifest_binds_exact_dual_runtime_environment_identity(
    tmp_path: Path,
) -> None:
    manifest_path = _release_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release = load_current_release(tmp_path)

    expected = release_runtime_environment_binding(manifest["source_fingerprints"])
    assert release.runtime_environment == expected
    assert expected["required_environment_id"] != (
        expected["profile_environment_ids"]["control"]
    )
    assert expected["profile_environment_ids"]["control"] != (
        expected["profile_environment_ids"]["optimizer"]
    )
    for role in ("runtime_contract", "uv_lock", "dependency_manifest"):
        reference = expected[role]
        assert reference["sha256"] == manifest["source_fingerprints"][
            reference["path"]
        ]


@pytest.mark.parametrize("tamper", ["bundle", "profile", "lock_reference"])
def test_release_identity_rejects_tampered_runtime_environment_binding(
    tmp_path: Path,
    tamper: str,
) -> None:
    manifest_path = _release_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if tamper == "bundle":
        manifest["runtime_environment"]["required_environment_id"] = "0" * 64
    elif tamper == "profile":
        manifest["runtime_environment"]["profile_environment_ids"][
            "optimizer"
        ] = "0" * 64
    else:
        manifest["runtime_environment"]["uv_lock"]["sha256"] = "0" * 64

    with pytest.raises(ReleaseIdentityError, match="release .*environment|uv_lock"):
        identity_from_manifest(
            manifest,
            manifest_path=manifest_path.relative_to(tmp_path).as_posix(),
            manifest_sha256=_sha(manifest_path.read_bytes()),
        )


@pytest.mark.parametrize("coverage_change", ["missing", "extra"])
def test_independent_verifier_requires_exact_central_source_fingerprint_set(
    tmp_path: Path,
    coverage_change: str,
) -> None:
    manifest_path = _release_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if coverage_change == "missing":
        manifest["source_fingerprints"].pop(
            next(iter(sorted(REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS)))
        )
    else:
        manifest["source_fingerprints"]["tools/not-a-production-source.py"] = "0" * 64
    encoded = canonical_bytes(manifest)
    digest = _sha(encoded)
    changed_manifest = tmp_path / f"config/step5d/releases/{digest}/manifest.json"
    changed_manifest.parent.mkdir(parents=True, exist_ok=True)
    changed_manifest.write_bytes(encoded)

    with pytest.raises(
        ReleaseVerificationError,
        match="experiment source fingerprint coverage differs",
    ):
        verify_release_manifest(tmp_path, changed_manifest)


def test_dynamic_readiness_content_does_not_participate_in_release_verification(
    tmp_path: Path,
) -> None:
    manifest_path = _release_fixture(tmp_path)
    current_path = tmp_path / "config/current_stage.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["readiness"] = {
        "host_runtime_disposition": "stale_chat_derived_claim",
        "bench_ready": True,
    }
    current_path.write_bytes(canonical_bytes(current))

    report = verify_release_manifest(tmp_path, manifest_path)

    assert report["ok"] is True


def test_exact_first_row_admission_closes_plan_overlay_wrapper_and_tp_commit(
    tmp_path: Path,
) -> None:
    _release_fixture(
        tmp_path,
        execution_profile_id="nf5000-slew250-a250",
    )
    release = load_current_release(tmp_path)
    campaign_root = tmp_path / "campaign"
    launch_profile_path = tmp_path / "launch-profile.json"
    launch_profile = json.loads(
        (
            ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
        ).read_text(encoding="utf-8")
    )
    launch_profile["tp_program_id"] = PROGRAM
    control_contract_path = launch_profile_path.with_name(
        "step5d_autotune_v3_control_contract.json"
    )
    shutil.copy2(
        ROOT / "config/step5/step5d_autotune_v3_control_contract.json",
        control_contract_path,
    )
    launch_profile["control_contract_sha256"] = contract_sha256(
        load_contract(control_contract_path)
    )
    launch_profile_path.write_bytes(canonical_bytes(launch_profile))
    prepare_campaign(
        campaign_root,
        campaign_id="admission-campaign",
        launch_profile=load_launch_profile(
            launch_profile_path, expected_tp_program_id=PROGRAM
        ),
    )
    report = verify_first_row_admission(
        tmp_path,
        campaign_root=campaign_root,
        launch_profile_path=launch_profile_path,
        campaign_epoch=7,
        ready_consumed_command_seq=12,
        release=release,
    )
    assert report["ok"] is True
    assert report["protocol_id"] == "v3_full_home_rolling_arm_v1"
    assert report["control_candidate_uid"].startswith("control:v2:")
    assert report["occurrence_uid"].startswith("occurrence:v2:")
    assert report["transport_candidate_uid"].startswith("transport:v2:")
    assert report["would_be_arm_packet"]["command_seq"] == 13
    assert report["state_write_order"][-2:] == [26, 30]


def test_current_pointer_has_no_fallback_and_ignores_mirror_drift(
    tmp_path: Path,
) -> None:
    manifest = _release_fixture(tmp_path)
    release = load_current_release(tmp_path)
    assert release.manifest_path == manifest.relative_to(tmp_path).as_posix()
    (tmp_path / "config/mirror.json").write_text('{"program":"drift"}\n', encoding="utf-8")
    assert load_current_release(tmp_path).manifest_sha256 == release.manifest_sha256
    pointer = tmp_path / "config/step5d/current.json"
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload["latest"] = True
    pointer.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseIdentityError, match="manifest path and SHA only"):
        load_current_release(tmp_path)


def test_verifier_ignores_semantically_stale_compatibility_mirror(
    tmp_path: Path,
) -> None:
    manifest_path = _release_fixture(tmp_path)
    current_path = tmp_path / "config/current_stage.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["local_candidate"]["program"] = (
        "step5d_strict_rnn_autotune_v3_r009"
    )
    current_path.write_bytes(canonical_bytes(current))

    assert verify_release_manifest(tmp_path, manifest_path)["ok"] is True


def test_promotion_rewrites_selected_release_mirrors_to_r012() -> None:
    triplet = {".script": "1" * 64, ".txt": "2" * 64, ".urp": "3" * 64}
    current = promoter._render_current_stage(
        json.loads((ROOT / "config/current_stage.json").read_text(encoding="utf-8")),
        program_id=PROGRAM,
        local_candidate_path=promoter._local_candidate_path(PROGRAM),
        triplet_sha256=triplet,
        deploy_manifest_sha256="4" * 64,
        numeric_sanity_sha256="5" * 64,
        readback_sha256="6" * 64,
    )
    assert current["local_candidate"]["program"] == PROGRAM
    assert current["evidence"]["sha256"] == triplet
    assert current["status"] == f"{PROGRAM}_controller_readback_verified"
    assert "bridge_trigger" in current
    assert {
        "execution_state",
        "live_run_status",
        "liveprep_status",
        "readiness",
    }.isdisjoint(current)

    table = promoter._render_stage_table(
        json.loads(
            (ROOT / "config/step5_stage_table.json").read_text(encoding="utf-8")
        ),
        program_id=PROGRAM,
        triplet_sha256=triplet,
        deploy_manifest_sha256="4" * 64,
        numeric_sanity_sha256="5" * 64,
        readback_sha256="6" * 64,
    )
    row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_autotune_v3")
    assert row["package_delivery"]["program_basename"] == PROGRAM
    assert row["package_delivery"]["sha256"] == triplet
    assert row["package_delivery"]["controller_readback_manifest_sha256"] == "6" * 64
    assert row["package_delivery"]["fresh_controller_sha_at"] is None
    assert "execution_readiness" not in row
    assert "live_authorized" not in row["current_binding"]
    assert "live_readiness_state" not in row["operator_lifecycle"]

    contract = promoter._render_contract(
        ROOT,
        json.loads(
            (
                ROOT / "config/step5/step5d_autotune_v3_control_contract.json"
            ).read_text(encoding="utf-8")
        ),
        program_id=PROGRAM,
        triplet_sha256=triplet,
        deploy_manifest_sha256="4" * 64,
        numeric_sanity_sha256="5" * 64,
        readback_sha256="6" * 64,
    )
    assert contract["candidate_tp_identity"]["program"] == PROGRAM
    assert contract["deployment_tp_identity"]["program"] == PROGRAM
    launch = promoter._render_launch_profile(
        json.loads(
            (
                ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
            ).read_text(encoding="utf-8")
        ),
        contract_sha256="7" * 64,
        tp_program_id=PROGRAM,
    )
    assert launch["tp_program_id"] == PROGRAM
def test_promotion_outputs_contain_identity_without_cached_live_state() -> None:
    candidate = promoter._local_candidate(
        program_id=PROGRAM,
        triplet_sha256={
            ".script": "1" * 64,
            ".txt": "2" * 64,
            ".urp": "3" * 64,
        },
        deploy_manifest_sha256="4" * 64,
        numeric_sanity_sha256="5" * 64,
        readback_sha256="6" * 64,
    )
    assert {
        "bridge_context_allowed",
        "motion_authorized",
        "next_legal_action",
    }.isdisjoint(candidate)
    source = Path(promoter.__file__).read_text(encoding="utf-8")
    for stale_field in (
        '"bridge_start_ready"',
        '"motion_arm_ready"',
        '"host_runtime_disposition"',
        '"terminal_scope"',
    ):
        assert stale_field not in source


def test_active_surface_recovery_ids_roll_from_program_revision() -> None:
    source = json.loads(
        (ROOT / "config/step5d/v3_active_surface.json").read_text(
            encoding="utf-8"
        )
    )

    rendered = promoter._render_active_surface(
        source,
        program_id="step5d_strict_rnn_autotune_v3_r014",
    )

    assert rendered["recovery_loaded_program_ids"] == [
        "step5d_strict_rnn_autotune_v3_r011",
        "step5d_strict_rnn_autotune_v3_r012",
        "step5d_strict_rnn_autotune_v3_r013",
    ]
    assert all(
        rendered["release_claims"][program].startswith("historical_")
        for program in rendered["recovery_loaded_program_ids"]
    )


def test_active_release_source_fingerprint_drift_fails_closed(
    tmp_path: Path,
) -> None:
    _release_fixture(tmp_path)
    relative = "tools/run_step5d_parameter_campaign.py"
    source = tmp_path / relative
    source.write_text("# drifted receiver source\n", encoding="utf-8")

    with pytest.raises(ReleaseIdentityError, match="source file fingerprint drifted"):
        load_current_release(tmp_path)


def test_compatible_readback_loads_historical_identity_but_verifies_bundle(
    tmp_path: Path,
) -> None:
    _release_fixture(tmp_path)
    release = load_current_release(tmp_path)
    source = tmp_path / "tools/step5d_autotune_coordinator.py"
    source.write_text("# source rebind candidate\n", encoding="utf-8")

    historical = load_current_release_for_compatible_readback(tmp_path)
    assert historical.manifest_sha256 == release.manifest_sha256
    assert historical.artifact_sha256 == release.artifact_sha256

    manifest_path = tmp_path / release.manifest_path
    script_path = manifest_path.parent / release.artifacts[".script"]["path"]
    script_path.write_text("# immutable artifact drift\n", encoding="utf-8")
    with pytest.raises(ReleaseIdentityError, match="artifact file fingerprint drifted"):
        load_current_release_for_compatible_readback(tmp_path)


def test_compatible_readback_accepts_historical_source_coverage(
    tmp_path: Path,
) -> None:
    manifest_path = _release_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    historical_source = next(
        relative
        for relative in sorted(manifest["source_fingerprints"])
        if relative.startswith("tools/step5d_autotune_v3/")
    )
    manifest["source_fingerprints"].pop(historical_source)
    encoded = canonical_bytes(manifest)
    digest = _sha(encoded)
    historical_path = tmp_path / f"config/step5d/releases/{digest}/manifest.json"
    shutil.copytree(manifest_path.parent, historical_path.parent)
    historical_path.write_bytes(encoded)
    pointer = {
        "schema": CURRENT_POINTER_SCHEMA,
        "manifest_path": historical_path.relative_to(tmp_path).as_posix(),
        "manifest_sha256": digest,
    }
    (tmp_path / "config/step5d/current.json").write_bytes(canonical_bytes(pointer))

    with pytest.raises(
        ReleaseIdentityError,
        match="experiment source fingerprint coverage differs",
    ):
        load_current_release(tmp_path)

    historical = load_current_release_for_compatible_readback(tmp_path)
    assert historical.manifest_sha256 == digest
    assert set(historical.source_fingerprints) == set(
        manifest["source_fingerprints"]
    )


def test_candidate_defaults_to_current_immutable_artifacts(tmp_path: Path) -> None:
    manifest_path = _release_fixture(tmp_path)

    artifact_dir = promoter.current_release_artifact_dir(tmp_path)

    assert artifact_dir == (
        manifest_path.parent / "programs/step5"
    )
    assert {
        path.name for path in artifact_dir.glob(f"{PROGRAM}*")
    } >= {
        f"{PROGRAM}.script",
        f"{PROGRAM}.txt",
        f"{PROGRAM}.urp",
        f"{PROGRAM}.deploy-manifest.json",
        f"{PROGRAM}.numeric-sanity.json",
    }


def test_candidate_prefers_canonical_repository_artifacts(tmp_path: Path) -> None:
    canonical = tmp_path / promoter.PACKAGE_DIR
    canonical.mkdir(parents=True)
    builder.write_triplet(
        canonical,
        "2026-07-23T0000HKT_" + PROGRAM.upper(),
        program_id=PROGRAM,
        execution_profile_id="nf5000-slew250-a250",
    )

    assert promoter.default_release_artifact_dir(tmp_path) == canonical


ATOMIC_RUNTIME_REGISTERS = {
    "protocol_version": 35,
    "digest_hi": 36,
    "digest_lo": 37,
}


def _atomic_manifest(label: str) -> dict[str, Any]:
    one = f"{label}:one\n".encode()
    two = f"{label}:two\n".encode()
    return {
        "schema": "atomic-release-fixture-v1",
        "label": label,
        "identity": {
            "program_id": f"program-{label}",
            "protocol_id": "rolling-protocol-v1",
            "release_stage_id": "step5d-autotune-v3",
        },
        "tp_runtime_identity": {
            "protocol_version": 1,
            "registers": dict(ATOMIC_RUNTIME_REGISTERS),
        },
        "compatibility_mirrors": {
            "active/one.txt": _sha(one),
            "active/two.txt": _sha(two),
        },
    }


def _publish_atomic_fixture(
    publisher: AtomicReleasePublisher,
    label: str,
    *,
    manifest: dict[str, Any] | None = None,
    crash_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    return publisher.publish(
        manifest=manifest or _atomic_manifest(label),
        bundle_files={
            "active/one.txt": f"{label}:one\n".encode(),
            "active/two.txt": f"{label}:two\n".encode(),
        },
        compatibility_targets={
            "active/one.txt": "active/one.txt",
            "active/two.txt": "active/two.txt",
        },
        stage_verifier=lambda stage, path, digest: None,
        crash_hook=crash_hook,
    )


def _load_atomic_manifest(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer = json.loads(
        (root / "config/step5d/current.json").read_text(encoding="utf-8")
    )
    manifest_path = root / str(pointer["manifest_path"])
    encoded = manifest_path.read_bytes()
    assert _sha(encoded) == pointer["manifest_sha256"]
    assert manifest_path.parent.name == pointer["manifest_sha256"]
    return pointer, json.loads(encoded)


def test_atomic_publisher_requires_verifier_and_commits_before_mirrors(
    tmp_path: Path,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    manifest = _atomic_manifest("new")
    with pytest.raises(AtomicReleaseError, match="verifier is required"):
        publisher.publish(
            manifest=manifest,
            bundle_files={"active/one.txt": b"new:one\n"},
            compatibility_targets={"active/one.txt": "active/one.txt"},
        )
    order: list[str] = []
    result = publisher.publish(
        manifest=manifest,
        bundle_files={
            "active/one.txt": b"new:one\n",
            "active/two.txt": b"new:two\n",
        },
        compatibility_targets={
            "active/one.txt": "active/one.txt",
            "active/two.txt": "active/two.txt",
        },
        stage_verifier=lambda stage, path, digest: order.append("verified"),
        crash_hook=order.append,
    )
    assert order.index("verified") < order.index("staging_verified")
    assert order.index("staging_verified") < order.index("bundle_renamed")
    assert order.index("pointer_directory_fsynced") < order.index(
        "compatibility_mirror_1"
    )
    assert (tmp_path / "active/one.txt").read_bytes() == b"new:one\n"
    pointer, _ = _load_atomic_manifest(tmp_path)
    assert pointer["manifest_sha256"] == result["manifest_sha256"]
    assert result["compatibility_mirror_errors"] == []


def test_atomic_candidate_stage_never_changes_pointer_or_compatibility_mirrors(
    tmp_path: Path,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    _publish_atomic_fixture(publisher, "old")
    pointer_before = publisher.pointer.read_bytes()
    mirror_before = (tmp_path / "active/one.txt").read_bytes()

    result = publisher.stage_candidate(
        manifest=_atomic_manifest("candidate"),
        bundle_files={
            "active/one.txt": b"candidate:one\n",
            "active/two.txt": b"candidate:two\n",
        },
        stage_verifier=lambda stage, path, digest: None,
    )

    assert result["current_pointer_changed"] is False
    assert result["compatibility_mirrors_changed"] is False
    assert result["pointer"] is None
    assert publisher.pointer.read_bytes() == pointer_before
    assert (tmp_path / "active/one.txt").read_bytes() == mirror_before
    bundle = Path(result["bundle"])
    assert bundle.name == result["manifest_sha256"]
    assert (bundle / "manifest.json").is_file()


@pytest.mark.parametrize(
    "cut",
    [
        "staging_fsynced",
        "staging_verified",
        "bundle_renamed",
        "compatibility_prepared",
        "before_pointer_write",
    ],
)
def test_atomic_publish_precommit_crash_keeps_old_release_loadable(
    tmp_path: Path,
    cut: str,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    _publish_atomic_fixture(publisher, "old")
    old_pointer = publisher.pointer.read_bytes()

    def crash(name: str) -> None:
        if name == cut:
            raise RuntimeError(cut)

    with pytest.raises(RuntimeError, match=cut):
        _publish_atomic_fixture(
            publisher,
            f"new-{cut}",
            crash_hook=crash,
        )
    assert publisher.pointer.read_bytes() == old_pointer
    _, manifest = _load_atomic_manifest(tmp_path)
    assert manifest["label"] == "old"
    assert (tmp_path / "active/one.txt").read_bytes() == b"old:one\n"
    assert (tmp_path / "active/two.txt").read_bytes() == b"old:two\n"


@pytest.mark.parametrize(
    "cut",
    [
        "pointer_written",
        "pointer_directory_fsynced",
        "compatibility_mirror_1",
        "compatibility_mirror_2",
    ],
)
def test_atomic_publish_postcommit_crash_keeps_new_release_authoritative(
    tmp_path: Path,
    cut: str,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    _publish_atomic_fixture(publisher, "old")

    def crash(name: str) -> None:
        if name == cut:
            raise RuntimeError(cut)

    with pytest.raises(RuntimeError, match=cut):
        _publish_atomic_fixture(publisher, "new", crash_hook=crash)
    _, manifest = _load_atomic_manifest(tmp_path)
    assert manifest["label"] == "new"
    if cut in {"pointer_written", "pointer_directory_fsynced"}:
        assert (tmp_path / "active/one.txt").read_bytes() == b"old:one\n"
        assert (tmp_path / "active/two.txt").read_bytes() == b"old:two\n"
    elif cut == "compatibility_mirror_1":
        assert (tmp_path / "active/one.txt").read_bytes() == b"new:one\n"
        assert (tmp_path / "active/two.txt").read_bytes() == b"old:two\n"
    else:
        assert (tmp_path / "active/one.txt").read_bytes() == b"new:one\n"
        assert (tmp_path / "active/two.txt").read_bytes() == b"new:two\n"


def test_atomic_postcommit_mirror_failure_is_best_effort(tmp_path: Path) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    (tmp_path / "blocked").write_text("not a directory\n", encoding="utf-8")
    result = publisher.publish(
        manifest=_atomic_manifest("new"),
        bundle_files={"projection/value.txt": b"new\n"},
        compatibility_targets={"blocked/value.txt": "projection/value.txt"},
        stage_verifier=lambda stage, path, digest: None,
    )

    _, manifest = _load_atomic_manifest(tmp_path)
    assert manifest["label"] == "new"
    assert [entry["path"] for entry in result["compatibility_mirror_errors"]] == [
        "blocked/value.txt"
    ]


def test_atomic_rollback_uses_verified_compatible_immutable_release(
    tmp_path: Path,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    old = _publish_atomic_fixture(publisher, "old")
    current = _publish_atomic_fixture(publisher, "current")
    verified: list[str] = []
    order: list[str] = []

    result = publisher.rollback(
        target_manifest_sha256=str(old["manifest_sha256"]),
        release_verifier=lambda bundle, path, digest: verified.append(digest),
        crash_hook=order.append,
    )

    assert verified == [current["manifest_sha256"], old["manifest_sha256"]]
    assert result["rolled_back_from_manifest_sha256"] == current["manifest_sha256"]
    assert result["manifest_sha256"] == old["manifest_sha256"]
    _, manifest = _load_atomic_manifest(tmp_path)
    assert manifest["label"] == "old"
    assert manifest["identity"]["program_id"] == "program-old"
    assert order.index("rollback_pointer_directory_fsynced") < order.index(
        "rollback_compatibility_mirror_1"
    )
    assert (tmp_path / "active/one.txt").read_bytes() == b"old:one\n"
    assert (tmp_path / "active/two.txt").read_bytes() == b"old:two\n"


@pytest.mark.parametrize(
    "cut",
    [
        "rollback_current_verified",
        "rollback_target_verified",
        "rollback_compatibility_verified",
        "rollback_compatibility_prepared",
        "rollback_before_pointer_write",
    ],
)
def test_atomic_rollback_precommit_crash_keeps_current_release_loadable(
    tmp_path: Path,
    cut: str,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    old = _publish_atomic_fixture(publisher, "old")
    current = _publish_atomic_fixture(publisher, "current")
    current_pointer = publisher.pointer.read_bytes()

    def crash(name: str) -> None:
        if name == cut:
            raise RuntimeError(cut)

    with pytest.raises(RuntimeError, match=cut):
        publisher.rollback(
            target_manifest_sha256=str(old["manifest_sha256"]),
            release_verifier=lambda bundle, path, digest: None,
            crash_hook=crash,
        )
    assert publisher.pointer.read_bytes() == current_pointer
    pointer, manifest = _load_atomic_manifest(tmp_path)
    assert pointer["manifest_sha256"] == current["manifest_sha256"]
    assert manifest["label"] == "current"
    assert (tmp_path / "active/one.txt").read_bytes() == b"current:one\n"
    assert (tmp_path / "active/two.txt").read_bytes() == b"current:two\n"


@pytest.mark.parametrize(
    "cut",
    [
        "rollback_pointer_written",
        "rollback_pointer_directory_fsynced",
        "rollback_compatibility_mirror_1",
        "rollback_compatibility_mirror_2",
    ],
)
def test_atomic_rollback_postcommit_crash_keeps_target_release_authoritative(
    tmp_path: Path,
    cut: str,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    old = _publish_atomic_fixture(publisher, "old")
    _publish_atomic_fixture(publisher, "current")

    def crash(name: str) -> None:
        if name == cut:
            raise RuntimeError(cut)

    with pytest.raises(RuntimeError, match=cut):
        publisher.rollback(
            target_manifest_sha256=str(old["manifest_sha256"]),
            release_verifier=lambda bundle, path, digest: None,
            crash_hook=crash,
        )
    pointer, manifest = _load_atomic_manifest(tmp_path)
    assert pointer["manifest_sha256"] == old["manifest_sha256"]
    assert manifest["label"] == "old"


@pytest.mark.parametrize(
    "change",
    [
        "protocol_id",
        "release_stage_id",
        "protocol_version",
        "registers",
        "missing_protocol_id",
        "missing_release_stage_id",
        "missing_protocol_version",
        "missing_registers",
    ],
)
def test_atomic_rollback_rejects_incompatible_or_incomplete_manifest(
    tmp_path: Path,
    change: str,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    target_manifest = _atomic_manifest("old")
    identity = target_manifest["identity"]
    runtime_identity = target_manifest["tp_runtime_identity"]
    assert isinstance(identity, dict)
    assert isinstance(runtime_identity, dict)
    if change == "protocol_id":
        identity["protocol_id"] = "incompatible"
    elif change == "release_stage_id":
        identity["release_stage_id"] = "incompatible"
    elif change == "protocol_version":
        runtime_identity["protocol_version"] = 2
    elif change == "registers":
        runtime_identity["registers"] = {
            **ATOMIC_RUNTIME_REGISTERS,
            "digest_lo": 38,
        }
    elif change.startswith("missing_"):
        field = change.removeprefix("missing_")
        (identity if field in identity else runtime_identity).pop(field)
    old = _publish_atomic_fixture(
        publisher,
        "old",
        manifest=target_manifest,
    )
    current = _publish_atomic_fixture(publisher, "current")

    with pytest.raises(
        AtomicReleaseError,
        match="rollback compatibility fields|not protocol-compatible",
    ):
        publisher.rollback(
            target_manifest_sha256=str(old["manifest_sha256"]),
            release_verifier=lambda bundle, path, digest: None,
        )
    pointer, manifest = _load_atomic_manifest(tmp_path)
    assert pointer["manifest_sha256"] == current["manifest_sha256"]
    assert manifest["label"] == "current"


def test_atomic_rollback_requires_both_manifests_to_verify(tmp_path: Path) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    old = _publish_atomic_fixture(publisher, "old")
    current = _publish_atomic_fixture(publisher, "current")

    with pytest.raises(AtomicReleaseError, match="verifier is required"):
        publisher.rollback(target_manifest_sha256=str(old["manifest_sha256"]))

    def reject_target(bundle: Path, path: Path, digest: str) -> None:
        if digest == old["manifest_sha256"]:
            raise AtomicReleaseError("target verification rejected")

    with pytest.raises(AtomicReleaseError, match="target verification rejected"):
        publisher.rollback(
            target_manifest_sha256=str(old["manifest_sha256"]),
            release_verifier=reject_target,
        )
    pointer, manifest = _load_atomic_manifest(tmp_path)
    assert pointer["manifest_sha256"] == current["manifest_sha256"]
    assert manifest["label"] == "current"
