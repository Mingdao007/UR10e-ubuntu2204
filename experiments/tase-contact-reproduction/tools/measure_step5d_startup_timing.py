#!/usr/bin/env python3
"""Measure real Step5d V3 offline startup paths without touching hardware.

Cold samples start a new interpreter. Warm samples use one persistent worker
interpreter. Hardware-facing preflight calls are replaced inside that worker
with deterministic observations; the campaign-preparation CLI is executed
unchanged. Coordinator lanes and the bridge-ready barrier use real subprocess
boundaries and temporary fixture files.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Mapping


_REAL_POPEN = subprocess.Popen

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
PREPARE = ROOT / "tools/prepare_step5d_autotune_launch.py"
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
SCHEMA = "step5d.autotune-v3/startup-timing-v1"
MAX_OUTPUT_BYTES = 32 * 1024
RUNS = 20
THRESHOLDS_MS = {
    "campaign_prepare": 2000.0,
    "preflight_worker": 2000.0,
    "live_handoff_to_bridge_popen": 2000.0,
    "bridge_ready_p95": 6000.0,
    "bridge_ready_max": 8000.0,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _fixture_fingerprint() -> tuple[str, dict[str, str]]:
    paths = {
        "experiment_config": ROOT / "config/step5d_autotune_campaign_v1.json",
        "launch_profile": ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
        "campaign_prepare": PREPARE,
        "timing_harness": SCRIPT,
    }
    digests = {name: _sha256(path) for name, path in paths.items()}
    encoded = json.dumps(digests, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), digests


def _offline_runtime_environment(sandbox: Path) -> dict[str, str]:
    return {
        "HOME": str(sandbox / "home"),
        "XDG_CONFIG_HOME": str(sandbox / "xdg-config"),
        "XDG_DATA_HOME": str(sandbox / "xdg-data"),
        "XDG_STATE_HOME": str(sandbox / "xdg-state"),
        "XDG_CACHE_HOME": str(sandbox / "xdg-cache"),
    }


@contextlib.contextmanager
def _environment_overlay(additions: Mapping[str, str]) -> Any:
    previous = {key: os.environ.get(key) for key in additions}
    os.environ.update(additions)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _seed_offline_runtime_identity(sandbox: Path) -> dict[str, str]:
    """Create the O(1) runtime identity needed only by this temp fixture."""

    from step5d_autotune_v3.runtime_installation import (
        ATTESTATION_SCHEMA,
        POINTER_SCHEMA,
        PROFILES,
        current_pointer_path,
        lock_sha256,
        runtime_bundle_id,
        runtime_contract_sha256,
    )

    environment = _offline_runtime_environment(sandbox)
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    bundle_id = runtime_bundle_id()
    contract_sha256_value = runtime_contract_sha256()
    lock_sha256_value = lock_sha256()
    installation_manifest_sha256 = _sha256_json(
        {
            "schema": "step5d.autotune-v3/offline-timing-runtime-v1",
            "bundle_id": bundle_id,
        }
    )
    store = (
        Path(environment["XDG_DATA_HOME"])
        / "step5d-autotune-v3"
        / "runtimes"
        / bundle_id
    )
    profiles = {
        profile: {
            "environment_id": _sha256_json(
                {"profile": profile, "role": "environment"}
            ),
            "root": str(store / profile),
            "python_executable": str(store / profile / "bin" / "python"),
            "record_tree_sha256": _sha256_json(
                {"profile": profile, "role": "record-tree"}
            ),
            "profile_tree_sha256": _sha256_json(
                {"profile": profile, "role": "profile-tree"}
            ),
        }
        for profile in PROFILES
    }
    attestation_root = (
        Path(environment["XDG_STATE_HOME"])
        / "step5d-autotune-v3"
        / bundle_id
        / "attestations"
    )
    attestation_root.mkdir(parents=True, exist_ok=True)
    attestation_path = attestation_root / "runtime-attestation-offline-timing.json"
    attestation = {
        "schema": ATTESTATION_SCHEMA,
        "bundle_id": bundle_id,
        "contract_sha256": contract_sha256_value,
        "lock_sha256": lock_sha256_value,
        "installation_manifest_sha256": installation_manifest_sha256,
        "observed_at_unix_ns": time.time_ns(),
        "host": {
            "fixture": "offline-startup-timing",
            "production_authority": False,
        },
        "profiles": profiles,
    }
    attestation_path.write_text(
        json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    attestation_path.chmod(0o400)
    pointer = {
        "schema": POINTER_SCHEMA,
        "bundle_id": bundle_id,
        "contract_sha256": contract_sha256_value,
        "lock_sha256": lock_sha256_value,
        "attestation_path": str(attestation_path),
        "attestation_sha256": _sha256(attestation_path),
        "installation_manifest_sha256": installation_manifest_sha256,
        "profiles": profiles,
    }
    pointer_path = current_pointer_path(environment)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(
        json.dumps(pointer, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return environment


def _make_fixture(index: int) -> dict[str, Any]:
    import build_step5d_autotune_tp_v3 as builder
    from step5d_autotune_v3.bridge_admission import (
        SCHEMA as BRIDGE_ADMISSION_SCHEMA,
        compute_program_admission,
        release_contract_reference,
        resolve_publication_lineage,
        validate_bridge_admission,
        write_indexed_bridge_admission,
    )
    from step5d_autotune_v3.delivery_observation import build_delivery_observation
    from step5d_autotune_v3.governance import read_proc_starttime_ticks
    from step5d_autotune_v3.launch_basis import make_launch_basis, write_launch_basis
    from step5d_autotune_v3.profile import contract_sha256, load_contract
    from step5d_autotune_v3.release_identity import (
        CURRENT_POINTER_SCHEMA,
        RELEASE_MANIFEST_SCHEMA,
        REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS,
        REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS,
        load_runtime_release,
        release_runtime_environment_binding,
    )
    from step5d_autotune_v3.release_contract import (
        CANONICAL_LAUNCH_ENV,
        run_release_contract_check,
    )
    from step5d_autotune_v3.runtime_identity import bind_final_script
    from step5d_autotune_v3.runtime_gate import release_runtime_contract
    from prepare_step5d_autotune_launch import _campaign_fingerprint
    from step5d_autotune_v3.release_transition import (
        create_delivery_basis,
        write_publication_lineage,
    )
    from step5d_autotune_v3.state import atomic_json

    sandbox = Path(tempfile.mkdtemp(prefix=f"step5d-timing-{index:02d}-"))
    runtime_environment = _seed_offline_runtime_identity(sandbox)
    fixture_git_root = sandbox / "repo"
    root = fixture_git_root / "experiments" / "tase-contact-reproduction"
    fixture_git_root.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    release_root = root / "release"
    release_root.mkdir(parents=True)
    (root / "config/step5").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "config/step5d_autotune_campaign_v1.json",
        root / "config/step5d_autotune_campaign_v1.json",
    )
    launch_profile_path = root / "config/step5/step5d_autotune_v3_launch_profile.json"
    shutil.copyfile(ROOT / "config/step5/step5d_autotune_v3_launch_profile.json", launch_profile_path)
    program = "step5d_strict_rnn_autotune_v3_r021"
    protocol = "v3_full_home_rolling_arm_v1"
    contract_path = root / "config/step5/step5d_autotune_v3_control_contract.json"
    shutil.copyfile(
        ROOT / "config/step5/step5d_autotune_v3_control_contract.json",
        contract_path,
    )
    contract = load_contract(contract_path)
    profile_payload = json.loads(launch_profile_path.read_text(encoding="utf-8"))
    profile_payload["tp_program_id"] = program
    profile_payload["control_contract_sha256"] = contract_sha256(contract)
    launch_profile_path.write_text(
        json.dumps(profile_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    release_profile_path = release_root / "config/step5/step5d_autotune_v3_launch_profile.json"
    release_profile_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(launch_profile_path, release_profile_path)

    stamp = f"2026-07-25T0000HKT_{program.upper()}"
    script = builder.build_package_script(stamp, program_id=program)
    txt = builder.build_txt(stamp, program_id=program).encode()
    script_bytes = script.encode()
    urp = builder.v1.build_urp(script, program, builder.CONTROLLER_DIR)
    artifacts: dict[str, dict[str, str]] = {}
    for extension, encoded in {".script": script_bytes, ".txt": txt, ".urp": urp}.items():
        relative = f"programs/step5/{program}{extension}"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        artifacts[extension] = {"path": relative, "sha256": _sha256(path)}
    source_fingerprints: dict[str, str] = {}
    for relative in sorted(REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# offline fixture source: {relative}\n", encoding="utf-8")
        source_fingerprints[relative] = _sha256(path)
    campaign_config = root / "config/step5d_autotune_campaign_v1.json"
    campaign_config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "config/step5d_autotune_campaign_v1.json", campaign_config)
    if "config/step5d_autotune_campaign_v1.json" in source_fingerprints:
        source_fingerprints["config/step5d_autotune_campaign_v1.json"] = _sha256(campaign_config)
    repository_sources: dict[str, str] = {}
    for relative in sorted(REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# offline fixture repository source: {relative}\n", encoding="utf-8")
        repository_sources[relative] = _sha256(path)
    source_launch_script = root / "scripts/step5d-autotune-v3.sh"
    source_launch_script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "scripts/step5d-autotune-v3.sh", source_launch_script)
    source_launch_script.chmod(source_launch_script.stat().st_mode | 0o111)
    if "scripts/step5d-autotune-v3.sh" in source_fingerprints:
        source_fingerprints["scripts/step5d-autotune-v3.sh"] = _sha256(source_launch_script)
    shutil.copyfile(
        ROOT / "config/step5/step5d_autotune_v3_control_contract.json",
        contract_path,
    )
    contract = load_contract(contract_path)
    profile_payload["tp_program_id"] = program
    profile_payload["control_contract_sha256"] = contract_sha256(contract)
    launch_profile_path.write_text(
        json.dumps(profile_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(launch_profile_path, release_profile_path)
    for relative, path in {
        "config/step5/step5d_autotune_v3_control_contract.json": contract_path,
        "config/step5/step5d_autotune_v3_launch_profile.json": launch_profile_path,
    }.items():
        if relative in source_fingerprints:
            source_fingerprints[relative] = _sha256(path)
    _, runtime_identity = bind_final_script(
        script,
        program_id=program,
        protocol_id=protocol,
    )
    generated_files: dict[str, str] = {}
    generated_payloads: dict[str, bytes] = {
        f"config/step5/step5d_autotune_v3_launch_profile.json": release_profile_path.read_bytes(),
        "config/step5d/parameter_receiver_initial.json": json.dumps(
            {"schema": "step5d.parameter-receiver/initial-v1", "revision": 1},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        f"programs/step5/{program}.deploy-manifest.json": json.dumps(
            {"program": program, "artifacts": artifacts, "tp_runtime_identity": runtime_identity},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        f"programs/step5/{program}.numeric-sanity.json": b'{"ok":true}\n',
        f"config/step5d/manifests/{program}/local_candidate.json": json.dumps(
            {"program": program, "triplet_sha256": {key: value["sha256"] for key, value in artifacts.items()}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    }
    for relative, encoded in generated_payloads.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        generated_files[relative] = _sha256(path)
        if relative in source_fingerprints:
            source_fingerprints[relative] = _sha256(path)
    controller_target = f"/programs/andyl/kunwei/step5/{program}.urp"
    manifest = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "identity": {
            "program_id": program,
            "release_stage_id": "step5d_strict_rnn_autotune_v3",
            "control_profile_id": "step5d_strict_rnn_autotune_v1",
            "protocol_id": protocol,
            "normal_max_rate_rad_s": 0.1,
            "execution_profile_id": "nf100-slew050-a050",
            "execution_profile_integer_id": 633,
        },
        "artifacts": artifacts,
        "controller_target": controller_target,
        "tp_runtime_identity": runtime_identity,
        "safety_envelope": {
            "path": "config/step5/step5d_autotune_v3_control_contract.json",
            "sha256": _sha256(contract_path),
        },
        "runtime_environment": release_runtime_environment_binding(source_fingerprints),
        "source_fingerprints": source_fingerprints,
        "generated_files": generated_files,
        "verification": {
            "canonical_verifier": "independent_script_urp_v3",
            "staged_bytes_required": True,
            "pointer_switched_last": True,
            "runtime_identity_derivation": (
                "canonical_script_identity_basis_sha256_plus_final_artifact_sha256_v1"
            ),
            "repository_source_root_depth": 0,
            "repository_source_fingerprints": repository_sources,
        },
    }
    encoded_manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_sha256 = hashlib.sha256(encoded_manifest).hexdigest()
    manifest_path = release_root / manifest_sha256 / "manifest.json"
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
    manifest_path.write_bytes(encoded_manifest)
    current_pointer_path = root / "config/step5d/current.json"
    current_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    current_pointer_path.write_text(
        json.dumps(
            {
                "schema": CURRENT_POINTER_SCHEMA,
                "manifest_path": f"release/{manifest_sha256}/manifest.json",
                "manifest_sha256": manifest_sha256,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release = load_runtime_release(root)
    campaign_fingerprint = _campaign_fingerprint(
        release_manifest_sha256=release.manifest_sha256,
        launch_profile_sha256=_sha256(release_profile_path),
    )
    runtime_contract = release_runtime_contract(root, release)
    expected_program = str(runtime_contract["expected_loaded_program"])
    triplet = {
        extension: reference["sha256"]
        for extension, reference in release.artifacts.items()
    }
    transaction_id = "0" * 32
    prior_receipt = root / "prior-full-readback.json"
    prior_receipt.write_text(
        json.dumps(
            {
                "status": "controller read-back verified",
                "delivery_mode": "full_upload_readback",
                "readback_source": "fresh_controller_get",
                "fresh_controller_sha_verified": True,
                "upload_transaction_id": transaction_id,
                "target_dir": "/programs/andyl/kunwei/step5",
                "sha256": {
                    "local": triplet,
                    "controller": triplet,
                    "readback": triplet,
                },
                "validation": {
                    "program": release.program_id,
                    "target_dir": "/programs/andyl/kunwei/step5",
                    "script_node_path": release.controller_target.removesuffix(".urp") + ".script",
                    "script_sha256": triplet[".script"],
                    "txt_sha256": triplet[".txt"],
                    "urp_sha256": triplet[".urp"],
                },
                "fresh_controller_checked_at": "2026-07-25T00:00:00+00:00",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    delivery_path = root / "delivery-observation.json"
    delivery = build_delivery_observation(
        root,
        receipt_path=prior_receipt,
        receipt_sha256=_sha256(prior_receipt),
        transaction_id=transaction_id,
        release=release,
    )
    atomic_json(delivery_path, delivery)
    dashboard = {
        "programState": "STOPPED",
        "get loaded program": expected_program,
        "safetymode": "NORMAL",
    }
    computed = compute_program_admission(dashboard, expected_program=expected_program)
    if not fixture_git_root.resolve().is_relative_to(sandbox.resolve()):
        raise RuntimeError("fixture Git root escaped the temp sandbox")
    if not root.resolve().is_relative_to(sandbox.resolve()):
        raise RuntimeError("fixture experiment root escaped the temp sandbox")
    subprocess.run(
        ["git", "init", str(fixture_git_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    certificate_root = root / "runs/step5d_autotune_v3"
    launch_env_script = root / "scripts/step5d-autotune-v3.sh"
    launch_env_script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "scripts/step5d-autotune-v3.sh", launch_env_script)
    launch_env_script.chmod(launch_env_script.stat().st_mode | 0o111)
    run_release_contract_check(
        root,
        certificate_root,
        release_identity=release,
        environment={
            **runtime_environment,
            CANONICAL_LAUNCH_ENV: str(launch_env_script),
        },
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(fixture_git_root),
            "-c",
            "user.name=step5d-timing-fixture",
            "-c",
            "user.email=step5d-timing-fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    create_delivery_basis(
        root,
        candidate_release=release,
        basis_release=release,
        prior_full_receipt=prior_receipt,
    )
    write_publication_lineage(root, release=release)
    lineage_path, _lineage = resolve_publication_lineage(root, release=release)
    release_contract = release_contract_reference(
        root,
        release,
        environment=runtime_environment,
    )
    admission = {
        "schema": BRIDGE_ADMISSION_SCHEMA,
        "observed_at_unix_ns": time.time_ns(),
        "state": computed["state"],
        "ok": computed["ok"],
        "reason_code": computed["reason_code"],
        "checks": computed["checks"],
        "program_state": dashboard["programState"],
        "loaded_program": dashboard["get loaded program"],
        "expected_loaded_program": expected_program,
        "operator_action": computed["operator_action"],
        "release": {
            "manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
        },
        "release_contract": release_contract,
        "publication_lineage": {
            "path": lineage_path.relative_to(root.resolve(strict=True)).as_posix(),
            "sha256": _sha256(lineage_path),
        },
        "delivery_observation": {
            "path": "delivery-observation.json",
            "sha256": _sha256(delivery_path),
            "transaction_id": transaction_id,
        },
        "dashboard": dashboard,
        "authority_acquired": False,
        "attempt_created": False,
        "campaign_fingerprint": campaign_fingerprint,
    }
    validate_bridge_admission(
        root,
        admission,
        release=release,
        environment=runtime_environment,
    )
    admission_path = write_indexed_bridge_admission(root, admission)
    basis_root = root / "basis"
    basis_path = basis_root / "launch-basis.json"
    now = time.time_ns()
    owner_starttime = read_proc_starttime_ticks(os.getpid())
    if owner_starttime is None:
        raise RuntimeError("cannot bind timing fixture to the current process")
    basis = make_launch_basis(
        release_manifest_sha256=release.manifest_sha256,
        runtime_identity_sha256=_sha256_json(runtime_identity),
        campaign_fingerprint=campaign_fingerprint,
        delivery_observation_sha256=_sha256(delivery_path),
        owner_pid=os.getpid(),
        owner_starttime=owner_starttime,
        authority_epoch=1,
        launch_nonce=f"{index + 1:064x}"[-32:],
        argv_sha256="e" * 64,
        effective_config_sha256="f" * 64,
        issued_at_unix_ns=now - 1_000_000,
        expires_at_unix_ns=now + 120_000_000_000,
    )
    write_launch_basis(basis_path, basis)
    authority_root = root / "authority"
    output_root = root / "coordinator"
    return {
        "root": str(sandbox),
        "experiment_root": str(root),
        "release_root": str(release_root),
        "release_manifest": str(manifest_path),
        "release_contract": str(contract_path),
        "release_profile": str(release_profile_path),
        "launch_profile": str(launch_profile_path),
        "admission": str(admission_path),
        "authority_root": str(authority_root),
        "output_root": str(output_root),
        "campaign_root": str(root / "campaign"),
        "binding_file": str(root / "campaign-binding.json"),
        "basis": str(basis_path),
        "basis_sha256": basis["basis_sha256"],
        "delivery": str(delivery_path),
        "owner_pid": os.getpid(),
        "owner_starttime": owner_starttime,
        "campaign_fingerprint": basis["campaign_fingerprint"],
        "runtime_identity": runtime_identity,
        "runtime_environment": runtime_environment,
    }


def _campaign_command(fixture: Mapping[str, Any]) -> list[str]:
    return [
        sys.executable,
        str(PREPARE),
        "--experiment-root",
        str(fixture["experiment_root"]),
        "--campaign-root",
        str(fixture["campaign_root"]),
        "--binding-file",
        str(fixture["binding_file"]),
        "--binding-source",
        "offline_startup_timing_fixture",
        "--campaign-fingerprint",
        str(fixture["campaign_fingerprint"]),
        "--launch-profile",
        str(fixture["launch_profile"]),
        "--launch-basis",
        str(fixture["basis"]),
        "--launch-basis-sha256",
        str(fixture["basis_sha256"]),
        "--owner-pid",
        str(fixture["owner_pid"]),
        "--owner-starttime",
        str(fixture["owner_starttime"]),
    ]


def _worker_command(operation: str, fixture: Mapping[str, Any]) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--worker",
        operation,
        "--fixture",
        str(fixture["experiment_root"]),
    ]


def _python_env() -> dict[str, str]:
    env = dict(os.environ)
    entries = [str(ROOT / "tools"), str(RUNTIME_SRC)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _fixture_from_root(root: Path) -> dict[str, Any]:
    from step5d_autotune_v3.bridge_admission import resolve_bridge_admission
    from step5d_autotune_v3.release_identity import load_runtime_release

    coordinator_root = root / "coordinator"
    basis_path = coordinator_root / "launch-basis.json"
    if not basis_path.is_file():
        basis_path = root / "basis/launch-basis.json"
    basis = json.loads(basis_path.read_text(encoding="utf-8"))
    release = load_runtime_release(root)
    runtime_environment = _offline_runtime_environment(root.resolve(strict=True).parents[2])
    admission_path, _admission = resolve_bridge_admission(
        root,
        release=release,
        environment=runtime_environment,
    )
    return {
        "root": str(root),
        "experiment_root": str(root),
        "release_contract": str(root / "release/control-contract.json"),
        "release_profile": str(root / "release/launch-profile.json"),
        "launch_profile": str(root / "config/step5/step5d_autotune_v3_launch_profile.json"),
        "admission": str(admission_path),
        "authority_root": str(root / "authority"),
        "output_root": str(coordinator_root),
        "preflight_output": str(coordinator_root / "preflight.json"),
        "campaign_root": str(root / "campaign"),
        "binding_file": str(root / "campaign-binding.json"),
        "basis": str(basis_path),
        "basis_sha256": basis["basis_sha256"],
        "delivery": str(root / "delivery-observation.json"),
        "owner_pid": basis["owner_pid"],
        "owner_starttime": basis["owner_starttime"],
        "campaign_fingerprint": basis["campaign_fingerprint"],
        "runtime_identity": {
            "protocol_version": 1,
            "digest_hi": 2,
            "digest_lo": 3,
        },
        "runtime_environment": runtime_environment,
    }


def _run_campaign_function(fixture: Mapping[str, Any]) -> dict[str, Any]:
    from prepare_step5d_autotune_launch import LaunchPreparationRequest, prepare

    result = prepare(
        LaunchPreparationRequest(
            experiment_root=Path(str(fixture["experiment_root"])),
            campaign_root=Path(str(fixture["campaign_root"])),
            binding_file=Path(str(fixture["binding_file"])),
            binding_source="offline_startup_timing_fixture",
            launch_profile_path=Path(str(fixture["launch_profile"])),
            candidate_batch_size=5,
            rolling_plan=False,
        )
    )
    return {"ok": result.get("ok") is True, "campaign_fingerprint": result.get("campaign_fingerprint")}


def _offline_preflight(fixture: Mapping[str, Any]) -> dict[str, Any]:
    import preflight_step5d_autotune_v3 as module
    from step5d_autotune_v3.release_identity import load_runtime_release as load_fixture_release
    from step5d_autotune_v3.runtime_gate import (
        release_runtime_contract as load_fixture_runtime_contract,
    )

    fixture_root = Path(str(fixture["experiment_root"]))
    fixture_release = load_fixture_release(fixture_root)
    runtime_contract = load_fixture_runtime_contract(fixture_root, fixture_release)

    release = SimpleNamespace(
        manifest_sha256=fixture_release.manifest_sha256,
        program_id=fixture_release.program_id,
        release_stage_id=fixture_release.release_stage_id,
        control_profile_id=fixture_release.control_profile_id,
        generated_files=fixture_release.generated_files,
    )
    launch = SimpleNamespace(fingerprint="c" * 64)
    contract = {"schema": "offline-contract"}
    dashboard = {
        "programState": "STOPPED",
        "get loaded program": runtime_contract["expected_loaded_program"],
        "safetymode": "NORMAL",
    }
    rtde = {field: ([0.0] * 6 if field in {"actual_TCP_pose", "actual_TCP_speed", "actual_TCP_force", "actual_q", "tcp_offset"} else ([0.0] * 3 if field == "payload_cog" else 0.0)) for field in module.support.RTDE_FIELDS}
    rtde["actual_TCP_pose"] = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    rtde["actual_qd"] = [0.0] * 6
    module.load_runtime_release = lambda _root: release
    module.load_delivery_observation = lambda *_args, **_kwargs: {"transaction_id": "offline"}
    module.release_runtime_contract = lambda *_args, **_kwargs: runtime_contract
    module.release_payload_path = lambda _root, _release, _name: Path(str(fixture["experiment_root"])) / "offline.json"
    module.load_contract = lambda _path: contract
    module.load_launch_profile = lambda *_args, **_kwargs: launch
    module.build_bridge_argv = lambda *_args, **_kwargs: [sys.executable, "offline-bridge"]
    module.require_runtime_profile = lambda _profile: {"profile": "offline"}
    module.dependency_observation = lambda: {"ok": True, "offline": True}
    module.dashboard_exchange = lambda *_args, **_kwargs: dashboard
    module._read_rtde_with_recipe_proof = lambda *_args, **_kwargs: rtde
    module.bridge_wrapper.check_v3_runtime_prewarm = lambda *_args, **_kwargs: {"ok": True, "offline": True}
    module.support.run_command = lambda *_args, **_kwargs: {"ok": True, "offline": True}
    module.support.no_existing_writer = lambda: {"ok": True, "offline": True}
    module.support.realtime_capability = lambda: {"ok": True, "offline": True}
    module.support.probe_port = lambda *_args, **_kwargs: {"ok": True, "open": True, "offline": True}
    module.support.tcp_connect_only = lambda *_args, **_kwargs: {"ok": True, "open": True, "offline": True}
    module.support.rtde_predicate = lambda _value: {"ok": True, "offline": True}
    output = Path(str(fixture.get("preflight_output", Path(str(fixture["experiment_root"])) / "preflight.json")))
    argv = [
        "--mailbox", str(Path(str(fixture["experiment_root"])) / "runtime" / "command.json"),
        "--delivery-observation", str(fixture["delivery"]),
        "--output", str(output),
        "--launch-basis", str(fixture["basis"]),
        "--launch-basis-sha256", str(fixture["basis_sha256"]),
        "--owner-pid", str(fixture["owner_pid"]),
        "--owner-starttime", str(fixture["owner_starttime"]),
        "--json",
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        code = module.main(argv)
    if code != 0:
        raise RuntimeError(f"offline preflight returned {code}")
    payload = json.loads(output.read_text(encoding="utf-8"))
    return {"ok": payload.get("ok") is True, "launch_basis_sha256": payload.get("launch_basis_sha256")}


class _TimedPopen:
    events: list[dict[str, Any]] = []
    transform: Any = staticmethod(lambda command: command)

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        if "env" not in kwargs:
            kwargs["env"] = _python_env()
        self.requested_command = [str(value) for value in command]
        command = self.transform(command)
        self.command = [str(value) for value in command]
        self.spawn_started_ns = time.perf_counter_ns()
        self._process = _REAL_POPEN(command, **kwargs)
        self.events.append({
            "command": self.command,
            "requested_command": self.requested_command,
            "spawn_started_ns": self.spawn_started_ns,
        })

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def wait(self, *args: Any, **kwargs: Any) -> int:
        return self._process.wait(*args, **kwargs)

    def terminate(self) -> None:
        self._process.terminate()

    def __enter__(self) -> "_TimedPopen":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.wait()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)


def _run_handoff(fixture: Mapping[str, Any]) -> dict[str, Any]:
    with _environment_overlay(fixture.get("runtime_environment", {})):
        return _run_handoff_with_environment(fixture)


def _run_handoff_with_environment(fixture: Mapping[str, Any]) -> dict[str, Any]:
    import run_step5d_autotune_v3_coordinator as coordinator
    import run_step5d_autotune_v3_live as live
    import step5d_bridge_authority as authority
    from step5d_autotune_v3.governance import read_proc_starttime_ticks
    from step5d_autotune_v3.launch_basis import (
        validate_strict_bridge_ready,
    )

    root = Path(str(fixture["experiment_root"]))
    stub_root = Path(str(fixture["root"])) / "stub-bridge"
    bridge_ready = stub_root / "bridge_ready.json"
    bridge_context = stub_root / "context.json"
    bridge_context.parent.mkdir(parents=True, exist_ok=True)
    _TimedPopen.events = []
    output_root = root / "coordinator"
    output_root.mkdir(parents=True, exist_ok=True)
    basis_path = output_root / "launch-basis.json"
    handoff_started_ns = time.perf_counter_ns()
    owner_pid = os.getpid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    if owner_starttime is None:
        raise RuntimeError("timing handoff owner starttime is unavailable")
    proof = {
        "production_coordinator": False,
        "production_basis": False,
        "production_lane_commands": False,
        "campaign_sibling_validator": 0,
        "preflight_sibling_validator": 0,
        "launch_basis_verifier": 0,
        "strict_bridge_ready_validator": False,
        "authority_owner_binding": False,
    }

    release = coordinator.load_runtime_release(root)
    admission_path = Path(str(fixture["admission"]))
    admission = json.loads(admission_path.read_text(encoding="utf-8"))
    old_popen = coordinator.subprocess.Popen

    production_path_proof = {
        "basis": False,
        "lane_commands": False,
        "campaign_prepare_validator": False,
        "preflight_validator": False,
    }
    proof["production_path_proof"] = production_path_proof
    traced_codes = {
        coordinator._basis.__code__: "basis",
        coordinator._lane_commands.__code__: "lane_commands",
        coordinator._validate_campaign_prepare.__code__: "campaign_prepare_validator",
        coordinator._validate_preflight.__code__: "preflight_validator",
    }
    traced_failures: dict[int, bool] = {}

    def trace_production_path(frame: Any, event: str, arg: Any) -> Any:
        name = traced_codes.get(frame.f_code)
        if name is None:
            return trace_production_path
        frame_id = id(frame)
        if event == "call":
            traced_failures[frame_id] = False
        elif event == "exception":
            traced_failures[frame_id] = True
        elif event == "return":
            failed = traced_failures.pop(frame_id, True)
            if not failed:
                production_path_proof[name] = True
        return trace_production_path

    def coordinator_popen_transform(command: list[str]) -> list[str]:
        if any(str(value).endswith("preflight_step5d_autotune_v3.py") for value in command):
            return [sys.executable, str(SCRIPT), "--worker", "preflight_worker", "--fixture", str(root)]
        return command

    _TimedPopen.transform = staticmethod(coordinator_popen_transform)
    coordinator.subprocess.Popen = _TimedPopen
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/step5d_bridge_authority.py"),
            "begin",
            "--authority-root",
            str(fixture["authority_root"]),
            "--attempt-id",
            "a" * 32,
            "--owner-pid",
            str(owner_pid),
            "--owner-starttime",
            str(owner_starttime),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    proof["authority_owner_binding"] = True
    active_authority = authority.load_current(Path(str(fixture["authority_root"])))
    if not isinstance(active_authority, dict) or active_authority.get("state") != "ACTIVE":
        raise RuntimeError("timing fixture authority is not ACTIVE after begin")
    active_owner = active_authority.get("owner")
    if not isinstance(active_owner, dict):
        raise RuntimeError("timing fixture authority owner is malformed")
    if active_owner != {"pid": owner_pid, "starttime_ticks": owner_starttime}:
        raise RuntimeError("timing handoff authority owner differs")
    args = SimpleNamespace(
        experiment_root=root,
        admission=admission_path,
        authority_root=Path(str(fixture["authority_root"])),
        attempt_id="a" * 32,
        authority_epoch=int(active_authority["sequence"]),
        owner_pid=owner_pid,
        owner_starttime=owner_starttime,
        output_root=output_root,
        campaign_root=Path(str(fixture["campaign_root"])),
        delivery_observation=Path(str(fixture["delivery"])),
        preflight=output_root / "preflight.json",
        launch_basis=basis_path,
        basis_ttl_s=120,
    )
    coordinator_started = time.perf_counter_ns()
    old_trace = sys.gettrace()
    sys.settrace(trace_production_path)
    try:
        coordinator_code = coordinator.run(args)
        if coordinator_code != 0:
            log_tails = {}
            for name in ("campaign-prepare.log", "preflight.log"):
                path = output_root / name
                log_tails[name] = path.read_text(encoding="utf-8", errors="replace")[-2000:] if path.is_file() else "<missing>"
            raise RuntimeError(
                f"production coordinator returned {coordinator_code}: {log_tails}"
            )
    finally:
        sys.settrace(old_trace)
        coordinator.subprocess.Popen = old_popen
    coordinator_elapsed_ms = (time.perf_counter_ns() - coordinator_started) / 1_000_000.0
    pre_bridge_spawn_events = len(_TimedPopen.events)

    basis = json.loads(basis_path.read_text(encoding="utf-8"))
    bridge_context.write_text(
        json.dumps(
            {
                "expected_profile": release.control_profile_id,
                "launch_nonce": basis["launch_nonce"],
                "ticket": {
                    "launch_id": basis["launch_nonce"],
                    "control_profile_id": release.control_profile_id,
                    "launch_basis": {
                        "path": basis_path.relative_to(root).as_posix(),
                        "sha256": basis["basis_sha256"],
                    },
                    "delivery_observation": {
                        "path": Path(fixture["delivery"]).relative_to(root).as_posix(),
                        "sha256": basis["delivery_observation_sha256"],
                    },
                    "manifest_sha256": release.manifest_sha256,
                    "tp_program_id": release.program_id,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    old_live_popen = live.subprocess.Popen
    bridge_command = [sys.executable, str(live.WRAPPER)]
    stub_bridge_command = [
        sys.executable,
        str(SCRIPT),
        "--stub-bridge",
        str(bridge_ready),
        "--stub-bridge-context",
        str(bridge_context),
    ]

    def _fake_bridge_popen(command: list[str], **kwargs: Any) -> Any:
        if len(command) >= 2 and command[1] == str(live.WRAPPER):
            command = stub_bridge_command
        return _TimedPopen(command, **kwargs)

    try:
        live.subprocess.Popen = _fake_bridge_popen
        bridge, bridge_launch_started_ns, bridge_ready_observed_ns = live._run_bridge_command_and_wait_for_readiness(
            bridge_command,
            bridge_ready_path=bridge_ready,
            timeout_s=2.0,
            role="offline stub bridge",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if len(_TimedPopen.events) <= pre_bridge_spawn_events:
            raise RuntimeError("bridge launch was not observed")
        bridge_event = _TimedPopen.events[-1]
        handoff_to_bridge_popen_ms = (
            bridge_event["spawn_started_ns"] - handoff_started_ns
        ) / 1_000_000.0
        bridge_ready_elapsed_ms = (
            bridge_ready_observed_ns - bridge_launch_started_ns
        ) / 1_000_000.0
        payload = json.loads(bridge_ready.read_text(encoding="utf-8"))
        if payload.get("ready_schema") != "step5d_bridge_ready_v2" or payload.get("ok") is not True:
            raise RuntimeError("stub bridge did not publish a real ready artifact")
        bridge_starttime = read_proc_starttime_ticks(bridge.pid)
        if bridge_starttime is None:
            raise RuntimeError("stub bridge starttime is unavailable")
        validate_strict_bridge_ready(
            payload,
            bridge_pid=bridge.pid,
            bridge_starttime_ticks=bridge_starttime,
            launch_nonce=basis["launch_nonce"],
            expected_profile=release.control_profile_id,
            ticket=json.loads(bridge_context.read_text(encoding="utf-8"))["ticket"],
            basis=basis,
            release=release,
            admission=admission,
        )
        proof["strict_bridge_ready_validator"] = True
        bridge.wait(timeout=2.0)
    finally:
        live.subprocess.Popen = old_live_popen
    coordinator._revoke(args, "completed")
    _TimedPopen.transform = staticmethod(lambda command: command)

    validator_passes = sum(
        int(production_path_proof[name])
        for name in ("campaign_prepare_validator", "preflight_validator")
    )
    production_path_proof = {
        "production_coordinator_run": coordinator_code == 0,
        "production_launch_basis_digest_validation": validator_passes,
        "production_launch_basis_freshness_validation": validator_passes,
        "production_launch_basis_owner_binding_validation": int(
            production_path_proof["basis"]
        ),
        "production_lane_command_builder": int(
            production_path_proof["lane_commands"]
        ),
        "campaign_output_validator": int(
            production_path_proof["campaign_prepare_validator"]
        ),
        "preflight_output_validator": int(
            production_path_proof["preflight_validator"]
        ),
    }
    threshold_pass = (
        coordinator_elapsed_ms < THRESHOLDS_MS["campaign_prepare"]
        and handoff_to_bridge_popen_ms < THRESHOLDS_MS["live_handoff_to_bridge_popen"]
        and bridge_ready_elapsed_ms <= THRESHOLDS_MS["bridge_ready_p95"]
        and bridge_ready_elapsed_ms <= THRESHOLDS_MS["bridge_ready_max"]
    )
    return {
        "schema": SCHEMA,
        "compact": True,
        "ok": True,
        "threshold_pass": threshold_pass,
        "coordinator_elapsed_ms": coordinator_elapsed_ms,
        "live_handoff_to_bridge_popen_ms": handoff_to_bridge_popen_ms,
        "bridge_ready_elapsed_ms": bridge_ready_elapsed_ms,
        "bridge_command": bridge_command,
        "bridge_ready_sha256": _sha256(bridge_ready),
        "spawn_events": list(_TimedPopen.events),
        "production_path_proof": production_path_proof,
    }


def _run_operation(operation: str, fixture_root: Path) -> dict[str, Any]:
    fixture = _fixture_from_root(fixture_root)
    with _environment_overlay(fixture["runtime_environment"]):
        if operation == "campaign_prepare":
            return _run_campaign_function(fixture)
        if operation == "preflight_worker":
            return _offline_preflight(fixture)
        if operation == "live_handoff_to_bridge_popen":
            return _run_handoff(fixture)
        raise ValueError(f"unknown timing operation: {operation}")


def _worker_main(operation: str, fixture_root: Path) -> int:
    result = _run_operation(operation, fixture_root)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


def _persistent_worker() -> int:
    for line in sys.stdin:
        request = json.loads(line)
        try:
            result = _run_operation(str(request["operation"]), Path(str(request["fixture"])))
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
        print(json.dumps(response, sort_keys=True), flush=True)
    return 0


def _run_cold(operation: str, index: int, fingerprint: str) -> dict[str, Any]:
    fixture = _make_fixture(index)
    started = time.perf_counter_ns()
    command = _campaign_command(fixture) if operation == "campaign_prepare" else _worker_command(operation, fixture)
    env = _python_env()
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    try:
        if completed.returncode != 0:
            raise RuntimeError(f"command returned {completed.returncode}: {completed.stderr[-500:]}")
        if operation == "campaign_prepare":
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        else:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        if result.get("ok") is not True:
            raise RuntimeError(f"offline operation failed: {result}")
        sample = {
            "index": index,
            "elapsed_ms": elapsed_ms,
            "command": command,
            "returncode": completed.returncode,
            "fixture_fingerprint": fingerprint,
            "result": result,
        }
        if operation == "live_handoff_to_bridge_popen":
            sample.update(result)
        return sample
    finally:
        shutil.rmtree(fixture["root"], ignore_errors=False)


def _run_warm(operation: str, index: int, fingerprint: str, worker: subprocess.Popen[str]) -> dict[str, Any]:
    fixture = _make_fixture(100 + index)
    request = {"operation": operation, "fixture": fixture["experiment_root"]}
    started = time.perf_counter_ns()
    assert worker.stdin is not None and worker.stdout is not None
    worker.stdin.write(json.dumps(request) + "\n")
    worker.stdin.flush()
    response = json.loads(worker.stdout.readline())
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    try:
        if response.get("ok") is not True or response.get("result", {}).get("ok") is not True:
            raise RuntimeError(f"persistent offline operation failed: {response}")
        result = response["result"]
        sample = {
            "index": index,
            "elapsed_ms": elapsed_ms,
            "command": [sys.executable, str(SCRIPT), "--persistent-worker"],
            "request": request,
            "returncode": 0,
            "fixture_fingerprint": fingerprint,
            "result": result,
        }
        if operation == "live_handoff_to_bridge_popen":
            sample.update(result)
        return sample
    finally:
        shutil.rmtree(fixture["root"], ignore_errors=False)


def _summary(samples: list[dict[str, Any]], field: str, threshold: float, *, inclusive: bool = False) -> dict[str, Any]:
    values = [float(sample[field]) for sample in samples]
    ordered = sorted(values)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    maximum = max(values)
    passed = (p95 <= threshold if inclusive else maximum < threshold)
    return {
        "count": len(values),
        "samples_ms": values,
        "p95_ms": p95,
        "max_ms": maximum,
        "threshold_ms": threshold,
        "passed": passed,
    }


def _run_measurement(output: Path) -> dict[str, Any]:
    fingerprint, files = _fixture_fingerprint()
    operations = ("campaign_prepare", "preflight_worker", "live_handoff_to_bridge_popen")
    cold = {operation: [_run_cold(operation, index, fingerprint) for index in range(RUNS)] for operation in operations}
    env = _python_env()
    worker = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--persistent-worker"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        warm = {operation: [_run_warm(operation, index, fingerprint, worker) for index in range(RUNS)] for operation in operations}
    finally:
        if worker.stdin is not None:
            worker.stdin.close()
        worker.wait(timeout=10.0)
    for operation in operations:
        if any(sample.get("result", {}).get("ok") is not True for sample in cold[operation] + warm[operation]):
            raise RuntimeError(f"{operation} produced an unsuccessful sample")
    cold_metrics = {
        operation: _summary(samples, "elapsed_ms", THRESHOLDS_MS[operation])
        for operation, samples in cold.items()
    }
    warm_metrics = {
        operation: _summary(samples, "elapsed_ms", THRESHOLDS_MS[operation])
        for operation, samples in warm.items()
    }
    bridge_samples = cold["live_handoff_to_bridge_popen"] + warm["live_handoff_to_bridge_popen"]
    handoff_to_bridge_metric = _summary(
        bridge_samples,
        "live_handoff_to_bridge_popen_ms",
        THRESHOLDS_MS["live_handoff_to_bridge_popen"],
    )
    bridge_ready_metric = _summary(
        bridge_samples,
        "bridge_ready_elapsed_ms",
        THRESHOLDS_MS["bridge_ready_p95"],
        inclusive=True,
    )
    bridge_ready_metric["max_threshold_ms"] = THRESHOLDS_MS["bridge_ready_max"]
    bridge_ready_metric["max_passed"] = bridge_ready_metric["max_ms"] <= THRESHOLDS_MS["bridge_ready_max"]

    def _preflight_before_stub_bridge(sample: Mapping[str, Any]) -> bool:
        events = sample["spawn_events"]
        coordinator_indexes = [
            index
            for index, item in enumerate(events)
            if any(
                "preflight_step5d_autotune_v3.py" in value
                for value in item["requested_command"]
            )
        ]
        bridge_indexes = [
            index
            for index, item in enumerate(events)
            if "--stub-bridge" in item["command"]
        ]
        return bool(coordinator_indexes and bridge_indexes) and max(
            coordinator_indexes
        ) < min(bridge_indexes)

    handoff_proofs = [sample["production_path_proof"] for sample in bridge_samples]
    order_passes = [
        _preflight_before_stub_bridge(sample) for sample in bridge_samples
    ]
    production_path_proof = {
        "handoff_samples": len(handoff_proofs),
        "production_coordinator_run_count": sum(
            int(item["production_coordinator_run"]) for item in handoff_proofs
        ),
        "launch_basis_digest_validation_min": min(
            item["production_launch_basis_digest_validation"] for item in handoff_proofs
        ),
        "launch_basis_freshness_validation_min": min(
            item["production_launch_basis_freshness_validation"]
            for item in handoff_proofs
        ),
        "launch_basis_owner_binding_validation_min": min(
            item["production_launch_basis_owner_binding_validation"]
            for item in handoff_proofs
        ),
        "lane_command_builder_count": sum(
            item["production_lane_command_builder"] for item in handoff_proofs
        ),
        "campaign_output_validator_count": sum(
            item["campaign_output_validator"] for item in handoff_proofs
        ),
        "preflight_output_validator_count": sum(
            item["preflight_output_validator"] for item in handoff_proofs
        ),
        "preflight_before_stub_bridge_count": sum(map(int, order_passes)),
        "preflight_before_stub_bridge_all": all(order_passes),
    }
    return {
        "schema": SCHEMA,
        "compact": True,
        "offline_only": True,
        "hardware_touched": False,
        "runs_per_mode": RUNS,
        "fixture": {"fingerprint": fingerprint, "files": files},
        "commands": {
            "campaign_prepare": "actual prepare_step5d_autotune_launch.py CLI with launch-basis fixture",
            "preflight_worker": "persistent/cold worker calls preflight_step5d_autotune_v3.main with patched offline hardware observations",
            "live_handoff_to_bridge_popen": "coordinator.run with real sibling subprocesses, then build live bridge launch command and intercept bridge Popen with stub",
            "stub_bridge": [sys.executable, str(SCRIPT), "--stub-bridge", "<fixture>/bridge_ready.json"],
        },
        "cold_metrics": cold_metrics,
        "warm_metrics": warm_metrics,
        "bridge_handoff_to_popen_ms": handoff_to_bridge_metric,
        "bridge_ready_ms": bridge_ready_metric,
        "production_path_proof": production_path_proof,
        "all_thresholds_passed": all(metric["passed"] for metric in cold_metrics.values())
        and all(metric["passed"] for metric in warm_metrics.values())
        and handoff_to_bridge_metric["passed"]
        and bridge_ready_metric["passed"]
        and bridge_ready_metric["max_passed"]
        and production_path_proof["preflight_before_stub_bridge_all"],
    }


def _stub_bridge(path: Path, context_path: Path) -> int:
    from step5d_autotune_v3.governance import read_proc_starttime_ticks

    time.sleep(0.005)
    path.parent.mkdir(parents=True, exist_ok=True)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    starttime = read_proc_starttime_ticks(os.getpid())
    if starttime is None:
        raise RuntimeError("stub bridge starttime is unavailable")
    path.write_text(
        json.dumps(
            {
                "ready_schema": "step5d_bridge_ready_v2",
                "ok": True,
                "pid": os.getpid(),
                "bridge_starttime_ticks": starttime,
                "launch_nonce": context["launch_nonce"],
                "bridge_profile": context["expected_profile"],
                "rtde_connected": True,
                "rtde_send_succeeded": True,
                "sensor_stream_ready": True,
                "prewarm_status": "ok",
                "rtde_output_fields": ["actual_TCP_pose"],
                "rtde_output_types": ["VECTOR6D"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Keep the producer alive after publication so the readiness barrier
    # observes a live bridge process, matching the production wait contract.
    time.sleep(0.1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("campaign_prepare", "preflight_worker", "live_handoff_to_bridge_popen"))
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--persistent-worker", action="store_true")
    parser.add_argument("--stub-bridge", type=Path)
    parser.add_argument("--stub-bridge-context", type=Path)
    args = parser.parse_args(argv)
    if args.stub_bridge is not None:
        if args.stub_bridge_context is None:
            parser.error("--stub-bridge-context is required with --stub-bridge")
        return _stub_bridge(args.stub_bridge, args.stub_bridge_context)
    if args.persistent_worker:
        return _persistent_worker()
    if args.worker is not None and args.fixture is not None:
        return _worker_main(args.worker, args.fixture)
    if args.output is None:
        parser.error("--output is required for the measurement mode")
    report = _run_measurement(args.output)
    encoded = json.dumps(
        report, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(encoded) + 1 > MAX_OUTPUT_BYTES:
        raise RuntimeError(
            f"startup timing report exceeds {MAX_OUTPUT_BYTES} bytes: {len(encoded) + 1}"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded + b"\n")
    print(json.dumps({"schema": report["schema"], "all_thresholds_passed": report["all_thresholds_passed"]}, sort_keys=True))
    return 0 if report["all_thresholds_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
