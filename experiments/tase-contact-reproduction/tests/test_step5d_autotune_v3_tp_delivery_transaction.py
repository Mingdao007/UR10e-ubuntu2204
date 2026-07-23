from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as builder  # noqa: E402
import promote_step5d_r009_atomic_release as promotion  # noqa: E402
import run_step5d_autotune_v3_tp_transaction as transaction  # noqa: E402
from step5d_autotune_v3.delivery_observation import (  # noqa: E402
    DeliveryObservationError,
    build_delivery_observation,
    validate_delivery_observation,
)
from step5d_autotune_v3.release_identity import load_local_release_candidate  # noqa: E402


def _write_receipt(
    root: Path,
    local: Path,
    *,
    suffix: str,
    transaction_id: str,
    checked_at: str,
    stamp: str,
    controller: str = "root@192.168.1.18",
) -> Path:
    readback = root / "runs" / f"controller_readback_{promotion.PROGRAM}_{suffix}"
    readback.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for extension in promotion.EXTENSIONS:
        source = local / f"{promotion.PROGRAM}{extension}"
        data = source.read_bytes()
        (readback / source.name).write_bytes(data)
        hashes[extension] = hashlib.sha256(data).hexdigest()
    manifest = {
        "status": "controller read-back verified",
        "controller": controller,
        "target_dir": promotion.TARGET_DIR,
        "validation": {
            "stamp": stamp,
            "program": promotion.PROGRAM,
            "target_dir": promotion.TARGET_DIR,
            "script_node_path": (
                f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.script"
            ),
            "script_sha256": hashes[".script"],
            "txt_sha256": hashes[".txt"],
            "urp_sha256": hashes[".urp"],
        },
        "sha256": {
            role: dict(hashes) for role in ("local", "controller", "readback")
        },
        "delivery_mode": "full_upload_readback",
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": checked_at,
        "readback_source": "fresh_controller_get",
        "upload_transaction_id": transaction_id,
    }
    path = readback / "manifest.json"
    path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return path


def _fixture_manifest(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "experiment"
    local = root / promotion.PACKAGE_DIR
    local.mkdir(parents=True)
    builder.write_triplet(
        local,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    return root, _write_receipt(
        root,
        local,
        suffix="fixture",
        transaction_id="a" * 32,
        checked_at="2026-07-21T11:00:33+08:00",
        stamp="fixture",
    )


def _release_for_receipt(path: Path) -> SimpleNamespace:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    return SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=promotion.PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.urp",
        artifact_sha256=dict(receipt["sha256"]["readback"]),
    )


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _composition_fixture(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    root = repository / "experiments/tase-contact-reproduction"
    for relative in promotion.SOURCE_INPUTS:
        _copy_file(ROOT / relative, root / relative)
    contract = json.loads(
        (
            ROOT / "config/step5/step5d_autotune_v3_control_contract.json"
        ).read_text(encoding="utf-8")
    )
    for relative_text in contract["source_sha256"]:
        relative = Path(relative_text)
        _copy_file(ROOT / relative, root / relative)
    for relative in promotion.REPOSITORY_SOURCE_INPUTS:
        _copy_file(ROOT.parents[1] / relative, repository / relative)
    for relative in promotion.STATIC_PROJECTIONS:
        _copy_file(ROOT / relative, root / relative)
    artifact_dir = root / "runs/content-identity-artifacts"
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    return root, artifact_dir


def test_delivery_manifest_drives_only_exact_fresh_triplet(tmp_path: Path) -> None:
    root, manifest = _fixture_manifest(tmp_path)
    artifact_dir = root / promotion.PACKAGE_DIR
    payload, hashes = promotion.validate_delivery(root, manifest, artifact_dir)
    assert payload["upload_transaction_id"] == "a" * 32
    assert hashes == payload["sha256"]["readback"]
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()

    with pytest.raises(promotion.R009PromotionError, match="identity"):
        promotion.validate_delivery(
            root,
            manifest,
            artifact_dir,
            expected_transaction_id="b" * 32,
            expected_manifest_sha256=manifest_sha256,
        )
    with pytest.raises(promotion.R009PromotionError, match="handoff SHA-256"):
        promotion.validate_delivery(
            root,
            manifest,
            artifact_dir,
            expected_transaction_id="a" * 32,
            expected_manifest_sha256="0" * 64,
        )

    payload["sha256"]["controller"][".script"] = "0" * 64
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(promotion.R009PromotionError, match="SHA closure"):
        promotion.validate_delivery(root, manifest, artifact_dir)


def test_promoter_rejects_symlink_receipt_and_artifact_handoffs(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    receipt_link = root / "receipt-link.json"
    receipt_link.symlink_to(receipt)
    with pytest.raises(promotion.R009PromotionError, match="manifest handoff is unsafe"):
        promotion.validate_delivery(
            root,
            receipt_link,
            root / promotion.PACKAGE_DIR,
        )

    artifact_link = root / "artifact-link"
    artifact_link.symlink_to(root / promotion.PACKAGE_DIR, target_is_directory=True)
    with pytest.raises(
        promotion.R009PromotionError,
        match="pending artifact directory is unsafe",
    ):
        promotion.compose_release(root, receipt, artifact_link)


def test_delivery_observation_revalidates_receipt_and_release_closure(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = (
        now - timedelta(seconds=5)
    ).isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()

    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=receipt_sha256,
        transaction_id="a" * 32,
        release=release,
        now=now,
    )

    assert validate_delivery_observation(
        root,
        observation,
        release=release,
        now=now,
    ) == observation
    changed = json.loads(json.dumps(observation))
    changed["fresh_controller_checked_at"] = now.isoformat()
    with pytest.raises(DeliveryObservationError, match="receipt content differs"):
        validate_delivery_observation(root, changed, release=release, now=now)


def test_delivery_observation_rejects_receipt_triplet_not_bound_to_release(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = now.isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)
    release.artifact_sha256[".script"] = "0" * 64

    with pytest.raises(DeliveryObservationError, match="identity closure differs"):
        build_delivery_observation(
            root,
            receipt_path=receipt,
            receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            transaction_id="a" * 32,
            release=release,
            now=now,
        )


def test_delivery_observation_rejects_receipt_validation_sha_mismatch(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = now.isoformat()
    payload["validation"]["script_sha256"] = "0" * 64
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)

    with pytest.raises(DeliveryObservationError, match="identity closure differs"):
        build_delivery_observation(
            root,
            receipt_path=receipt,
            receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            transaction_id="a" * 32,
            release=release,
            now=now,
        )


def test_manifest_v3_and_bundle_ignore_receipt_time_and_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, artifact_dir = _composition_fixture(tmp_path)
    contract = json.loads(
        (
            root / "config/step5/step5d_autotune_v3_control_contract.json"
        ).read_text(encoding="utf-8")
    )
    dynamic_contract_fields = {
        "source_sha256",
        "candidate_tp_artifact_sha256",
        "tp_artifact_sha256",
        "candidate_tp_identity",
        "deployment_tp_identity",
        "promotion_status",
    }
    static_contract = {
        key: value
        for key, value in contract.items()
        if key not in dynamic_contract_fields
    }
    monkeypatch.setattr(
        promotion,
        "CONTRACT_STATIC_SHA256",
        hashlib.sha256(promotion.canonical_bytes(static_contract)).hexdigest(),
    )
    first_receipt = _write_receipt(
        root,
        artifact_dir,
        suffix="first",
        transaction_id="1" * 32,
        checked_at="2026-07-21T11:00:33+08:00",
        stamp="first-receipt",
        controller="root@controller-a",
    )
    second_receipt = _write_receipt(
        root,
        artifact_dir,
        suffix="second",
        transaction_id="2" * 32,
        checked_at="2026-07-21T11:07:45+08:00",
        stamp="second-receipt",
        controller="root@controller-b",
    )
    first_receipt_sha = hashlib.sha256(first_receipt.read_bytes()).hexdigest()
    second_receipt_sha = hashlib.sha256(second_receipt.read_bytes()).hexdigest()
    assert first_receipt_sha != second_receipt_sha

    local = promotion.compose_local_release(root, artifact_dir)
    for relative in (
        Path("config/tase_protocol_table.json"),
        Path("config/step5d/v3_active_surface.json"),
    ):
        assert local[1][relative.as_posix()] == (root / relative).read_bytes()
    first = promotion.compose_release(
        root,
        first_receipt,
        artifact_dir,
        expected_transaction_id="1" * 32,
        expected_manifest_sha256=first_receipt_sha,
    )
    second = promotion.compose_release(
        root,
        second_receipt,
        artifact_dir,
        expected_transaction_id="2" * 32,
        expected_manifest_sha256=second_receipt_sha,
    )
    first_manifest, first_bundle, first_targets = first
    second_manifest, second_bundle, second_targets = second

    assert promotion.canonical_bytes(local[0]) == promotion.canonical_bytes(
        first_manifest
    )
    assert local[1:] == first[1:]
    assert promotion.canonical_bytes(first_manifest) == promotion.canonical_bytes(
        second_manifest
    )
    assert first_bundle == second_bundle
    assert first_targets == second_targets
    immutable_bytes = promotion.canonical_bytes(first_manifest) + b"".join(
        first_bundle[path] for path in sorted(first_bundle)
    )
    for dynamic in (
        b"1" * 32,
        b"2" * 32,
        b"first-receipt",
        b"second-receipt",
        b"2026-07-21T11:00:33+08:00",
        b"2026-07-21T11:07:45+08:00",
    ):
        assert dynamic not in immutable_bytes

    staged = promotion.stage_local_candidate(root, artifact_dir)
    assert staged["schema"] == "step5d.autotune-v3/local-release-candidate-v1"
    assert staged["manifest_sha256"] == hashlib.sha256(
        promotion.canonical_bytes(first_manifest)
    ).hexdigest()
    assert staged["current_pointer_changed"] is False
    assert staged["compatibility_mirrors_changed"] is False
    assert not (root / "config/step5d/current.json").exists()
    descriptor_path = root / "runs/local-release-candidate.json"
    descriptor_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor_path.write_text(json.dumps(staged) + "\n", encoding="utf-8")
    loaded_candidate, loaded_descriptor = load_local_release_candidate(
        root,
        descriptor_path,
    )
    assert loaded_candidate.manifest_sha256 == staged["manifest_sha256"]
    assert loaded_descriptor == staged

    publisher = promotion.AtomicReleasePublisher(root)

    def publish(
        manifest: dict[str, Any],
        bundle: dict[str, bytes],
        targets: dict[str, str],
    ) -> dict[str, Any]:
        def verify(stage: Path, manifest_path: Path, digest: str) -> None:
            promotion.verify_release_manifest(
                root,
                manifest_path,
                expected_manifest_sha256=digest,
                path_overrides={
                    target: stage / source for target, source in targets.items()
                },
            )

        return publisher.publish(
            manifest=manifest,
            bundle_files=bundle,
            compatibility_targets=targets,
            stage_verifier=verify,
        )

    first_result = publish(first_manifest, first_bundle, first_targets)
    bundle_path = Path(first_result["bundle"])
    first_tree = {
        path.relative_to(bundle_path).as_posix(): path.read_bytes()
        for path in sorted(bundle_path.rglob("*"))
        if path.is_file()
    }
    second_result = publish(second_manifest, second_bundle, second_targets)
    second_tree = {
        path.relative_to(bundle_path).as_posix(): path.read_bytes()
        for path in sorted(bundle_path.rglob("*"))
        if path.is_file()
    }
    assert first_result["manifest_sha256"] == second_result["manifest_sha256"]
    assert first_result["bundle"] == second_result["bundle"]
    assert first_tree == second_tree


def test_transaction_passes_exact_uploader_manifest_to_promotion(tmp_path: Path) -> None:
    root = tmp_path / "experiment"
    root.mkdir()
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    events: list[str] = []
    exact: list[Path] = []
    upload_arguments: list[str] = []
    promotion_arguments: dict[str, Any] = {}
    evidence_output = root / "runs/campaign/delivery-observation.json"
    candidate_path = root / "candidate.json"
    qualification_path = root / "qualification.json"

    def fake_upload(arguments: list[str]) -> int:
        events.append("upload")
        upload_arguments.extend(arguments)
        token = arguments[arguments.index("--upload-transaction-id") + 1]
        result_path = Path(arguments[arguments.index("--manifest-path-output") + 1])
        manifest = root / "runs" / f"controller_readback_{promotion.PROGRAM}_exact" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"upload_transaction_id": token}) + "\n")
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "ur10e_upload_result_v1",
                    "upload_transaction_id": token,
                    "manifest_path": str(manifest.resolve()),
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                }
            )
            + "\n"
        )
        exact.append(manifest.resolve())
        return 0

    def fake_promote(
        _root: Path,
        manifest: Path,
        artifact_dir: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        events.append("promote")
        exact.extend((manifest, artifact_dir))
        promotion_arguments.update(kwargs)
        return {"manifest_sha256": "f" * 64}

    release = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=promotion.PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.urp",
    )

    with (
        mock.patch.object(
            transaction,
            "require_runtime_profile",
            return_value={"bundle_id": "a" * 64},
        ),
        mock.patch.object(
            transaction,
            "load_gpu_functional_attestation",
            return_value=({}, {"path": "/gpu.json", "sha256": "a" * 64}),
        ),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_qualification",
            side_effect=lambda *_args: events.append("qualify") or release,
        ),
        mock.patch.object(
            transaction,
            "owner_dependency",
            return_value={
                "owner_id": "ur10e-controller-access",
                "path": "/verified/controller-helper.py",
                "sha256": "a" * 64,
            },
        ),
        mock.patch.object(transaction, "acquire_controller_mutation_locks", side_effect=lambda: events.append("lock") or [object()]),
        mock.patch.object(transaction.upload, "_main", side_effect=fake_upload),
        mock.patch.object(transaction.promote, "promote", side_effect=fake_promote),
        mock.patch.object(
            transaction,
            "load_current_release",
            side_effect=lambda _root: events.append("load") or release,
        ),
        mock.patch.object(
            transaction,
            "_program_load_host",
            side_effect=lambda _root, _release: events.append("load_host")
            or "127.0.0.1",
        ),
        mock.patch.object(
            transaction,
            "ensure_exact_loaded_program",
            side_effect=lambda host, target: events.append("program_load")
            or {
                "schema": "step5d.autotune-v3/program-load-observation-v1",
                "ok": True,
                "host": host,
                "target": target,
            },
        ),
        mock.patch.object(
            transaction,
            "build_delivery_observation",
            side_effect=lambda *args, **kwargs: events.append("observe")
            or {"schema": "fixture"},
        ),
        mock.patch.object(
            transaction,
            "atomic_json",
            side_effect=lambda path, value: events.append(f"evidence:{path.name}")
            or exact.append(path),
        ),
        mock.patch.object(transaction, "release_controller_mutation_locks", side_effect=lambda _handles: events.append("release")),
    ):
        assert transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(root / promotion.PACKAGE_DIR),
                "--release-candidate",
                str(candidate_path),
                "--qualification-result",
                str(qualification_path),
                "--evidence-output",
                str(evidence_output),
            ]
        ) == 0

    assert events == [
        "qualify",
        "lock",
        "upload",
        "promote",
        "load",
        "load_host",
        "program_load",
        "evidence:program-load-observation.json",
        "observe",
        "evidence:delivery-observation.json",
        "release",
    ]
    assert upload_arguments[0] == builder.PROGRAM_NAME
    assert any(
        value.startswith("manifest-driven step5d_strict_rnn_autotune_v3_r012")
        for value in upload_arguments
    )
    assert upload_arguments[
        upload_arguments.index("--controller-helper") + 1
    ] == "/verified/controller-helper.py"
    assert upload_arguments[
        upload_arguments.index("--controller-helper-sha256") + 1
    ] == "a" * 64
    assert exact[0] == exact[1]
    assert exact[2] == (root / promotion.PACKAGE_DIR).resolve()
    assert exact[3] == evidence_output.with_name("program-load-observation.json").resolve()
    assert exact[4] == evidence_output.resolve()
    token = upload_arguments[upload_arguments.index("--upload-transaction-id") + 1]
    assert promotion_arguments == {
        "expected_transaction_id": token,
        "expected_manifest_sha256": hashlib.sha256(exact[0].read_bytes()).hexdigest(),
        "expected_candidate_manifest_sha256": release.manifest_sha256,
    }


def test_program_load_failure_evidence_is_persisted_before_nonzero_return(
    tmp_path: Path,
) -> None:
    release = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=promotion.PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.urp",
    )
    evidence = tmp_path / "run/program-load-observation.json"
    external = {
        "schema": "step5d.autotune-v3/program-load-observation-v1",
        "ok": False,
        "reason_code": "DASHBOARD_POST_LOAD_MISMATCH",
        "blocker_class": "BLOCKED_EXTERNAL",
        "observed_at_unix_ns": 1,
        "expected_program": release.controller_target,
    }

    with (
        mock.patch.object(transaction, "_program_load_host", return_value="robot"),
        mock.patch.object(
            transaction,
            "ensure_exact_loaded_program",
            side_effect=transaction.DashboardProgramLoadError(external),
        ),
    ):
        return_code, observation = transaction._load_release_program(
            tmp_path,
            release,
            evidence,
        )

    assert return_code == 69
    assert observation == {
        **external,
        "release_binding": {
            "manifest_sha256": "f" * 64,
            "program_id": promotion.PROGRAM,
            "controller_target": release.controller_target,
        },
    }
    assert json.loads(evidence.read_text(encoding="utf-8")) == observation


def test_program_load_binding_failure_is_named_internal_evidence(
    tmp_path: Path,
) -> None:
    release = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=promotion.PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.urp",
    )
    evidence = tmp_path / "run/program-load-observation.json"

    with mock.patch.object(
        transaction,
        "_program_load_host",
        side_effect=transaction.ContractViolation("missing immutable robot_host"),
    ):
        return_code, observation = transaction._load_release_program(
            tmp_path,
            release,
            evidence,
        )

    assert return_code == 70
    assert observation["reason_code"] == "PROGRAM_LOAD_BINDING_INVALID"
    assert observation["blocker_class"] == "INTERNAL"
    assert json.loads(evidence.read_text(encoding="utf-8")) == observation


def test_transaction_rejects_evidence_output_outside_runs_before_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="escapes runs evidence root"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--evidence-output",
                str(root / "runs/../outside.json"),
            ]
        )

    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_qualification_failure_precedes_controller_lock_and_upload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    evidence_output = root / "runs/campaign/delivery-observation.json"

    with (
        mock.patch.object(
            transaction,
            "require_runtime_profile",
            return_value={"bundle_id": "a" * 64},
        ),
        mock.patch.object(
            transaction,
            "load_gpu_functional_attestation",
            return_value=({}, {"path": "/gpu.json", "sha256": "a" * 64}),
        ),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_qualification",
            side_effect=RuntimeError("qualification evidence differs"),
        ) as qualification_gate,
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="qualification evidence differs"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--release-candidate",
                str(root / "candidate.json"),
                "--qualification-result",
                str(root / "qualification.json"),
                "--evidence-output",
                str(evidence_output),
            ]
        )

    qualification_gate.assert_called_once()
    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_rejects_symlink_artifacts_and_evidence_before_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    artifact_link = root / "artifact-link"
    artifact_link.symlink_to(artifact_dir, target_is_directory=True)
    runs = root / "runs"
    runs.mkdir()
    evidence_target = runs / "actual-observation.json"
    evidence_target.write_text("{}\n", encoding="utf-8")
    evidence_link = runs / "observation-link.json"
    evidence_link.symlink_to(evidence_target)

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="pending artifact directory is unsafe"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_link),
                "--evidence-output",
                str(runs / "unused.json"),
            ]
        )
    lock.assert_not_called()
    upload.assert_not_called()

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="delivery evidence output is unsafe"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--evidence-output",
                str(evidence_link),
            ]
        )
    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_rejects_legacy_deploy_schema_before_lock_or_upload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    generated = builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    deploy = Path(generated["deploy_manifest"])
    payload = json.loads(deploy.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    deploy.write_text(json.dumps(payload), encoding="utf-8")

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="exactly one schema-v2"),
    ):
        transaction.main(
            ["--root", str(root), "--artifact-dir", str(artifact_dir)]
        )

    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_rejects_runtime_identity_tamper_before_upload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    generated = builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    deploy = Path(generated["deploy_manifest"])
    payload = json.loads(deploy.read_text(encoding="utf-8"))
    payload["tp_runtime_identity"]["program_id"] = (
        "step5d_strict_rnn_autotune_v3_r009"
    )
    deploy.write_text(json.dumps(payload), encoding="utf-8")

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="basename and runtime identity program differ"),
    ):
        transaction.main(
            ["--root", str(root), "--artifact-dir", str(artifact_dir)]
        )

    lock.assert_not_called()
    upload.assert_not_called()
