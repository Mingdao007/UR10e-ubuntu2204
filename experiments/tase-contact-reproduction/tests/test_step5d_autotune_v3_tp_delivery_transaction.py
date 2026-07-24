from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
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
from step5d_autotune_v3 import delivery_observation as delivery_module  # noqa: E402
from step5d_autotune_v3.delivery_observation import (  # noqa: E402
    DeliveryObservationError,
    build_delivery_observation,
    delivery_index_path,
    resolve_delivery_observation,
    validate_delivery_observation,
    write_indexed_delivery_observation,
)
from step5d_autotune_v3.release_identity import load_local_release_candidate  # noqa: E402
from step5d_autotune_v3 import release_transition as transition  # noqa: E402


def test_upload_runner_reports_bounded_child_failure_output() -> None:
    failure = subprocess.CalledProcessError(
        1,
        ["controller-helper.py", "readback"],
        output=("x" * 13_000) + "\nNo route to host\n",
        stderr="readback failed\n",
    )
    with (
        mock.patch.object(
            transaction.upload.subprocess,
            "run",
            side_effect=failure,
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        transaction.upload.run(
            ["controller-helper.py", "readback"],
            dry_run=False,
            capture=True,
        )

    message = str(raised.value)
    assert "bounded child output tail" in message
    assert "No route to host" in message
    assert "readback failed" in message
    assert len(message) < 12_500


def _write_receipt(
    root: Path,
    local: Path,
    *,
    suffix: str,
    transaction_id: str,
    checked_at: str,
    stamp: str,
    controller: str = "root@192.168.1.18",
    delivery_mode: str = "full_upload_readback",
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
        "delivery_mode": delivery_mode,
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


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _transition_fixture(
    tmp_path: Path,
) -> tuple[Path, SimpleNamespace, SimpleNamespace, Path]:
    repository = tmp_path / "repository"
    root = repository / "experiments/tase-contact-reproduction"
    root.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "release transition test")
    (root / ".gitignore").write_text("runs/\n", encoding="utf-8")
    basis_manifest = root / "config/step5d/releases/basis/manifest.json"
    basis_manifest.parent.mkdir(parents=True)
    basis_manifest.write_text('{"basis":true}\n', encoding="utf-8")
    basis_manifest_sha256 = hashlib.sha256(
        basis_manifest.read_bytes()
    ).hexdigest()
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "basis release")
    triplet = {
        ".script": "1" * 64,
        ".txt": "2" * 64,
        ".urp": "3" * 64,
    }
    target = f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.urp"
    basis = SimpleNamespace(
        manifest_path=basis_manifest.relative_to(root).as_posix(),
        manifest_sha256=basis_manifest_sha256,
        program_id=promotion.PROGRAM,
        controller_target=target,
        artifact_sha256=dict(triplet),
    )
    candidate = SimpleNamespace(
        manifest_path="config/step5d/releases/candidate/manifest.json",
        manifest_sha256="f" * 64,
        program_id=promotion.PROGRAM,
        controller_target=target,
        artifact_sha256=dict(triplet),
    )
    receipt = tmp_path / "prior-full-readback.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "controller read-back verified",
                "target_dir": promotion.TARGET_DIR,
                "validation": {
                    "program": promotion.PROGRAM,
                    "script_node_path": target.replace(".urp", ".script"),
                    "script_sha256": triplet[".script"],
                    "txt_sha256": triplet[".txt"],
                    "urp_sha256": triplet[".urp"],
                },
                "sha256": {
                    role: dict(triplet)
                    for role in ("local", "controller", "readback")
                },
                "delivery_mode": "full_upload_readback",
                "fresh_controller_sha_verified": True,
                "fresh_controller_checked_at": "2026-07-24T13:30:31+08:00",
                "readback_source": "fresh_controller_get",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return root, candidate, basis, receipt


def test_tracked_basis_imports_exact_prior_full_readback(
    tmp_path: Path,
) -> None:
    root, candidate, basis, receipt = _transition_fixture(tmp_path)

    path, payload = transition.create_delivery_basis(
        root,
        candidate_release=candidate,
        basis_release=basis,
        prior_full_receipt=receipt,
    )

    assert path == transition.delivery_basis_path(root, candidate)
    assert payload["candidate_release_manifest_sha256"] == "f" * 64
    prior = payload["prior_full_readback_receipt"]
    imported = root / prior["path"]
    assert imported.name == f"{prior['sha256']}.json"
    assert transition.delivery_basis_reference(
        root,
        release=candidate,
    ) == {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_existing_receipt_requires_exact_tracked_basis(
    tmp_path: Path,
) -> None:
    root, candidate, basis, prior = _transition_fixture(tmp_path)
    transition.create_delivery_basis(
        root,
        candidate_release=candidate,
        basis_release=basis,
        prior_full_receipt=prior,
    )
    reference = transition.delivery_basis_reference(root, release=candidate)

    assert transition.require_receipt_delivery_basis(
        root,
        {"delivery_basis": reference},
        release=candidate,
    ) == reference
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="basis reference differs",
    ):
        transition.require_receipt_delivery_basis(
            root,
            {"delivery_basis": {**reference, "sha256": "0" * 64}},
            release=candidate,
        )


def test_runtime_lineage_binds_clean_exact_head_and_tree(
    tmp_path: Path,
) -> None:
    root, candidate, basis, prior = _transition_fixture(tmp_path)
    transition.create_delivery_basis(
        root,
        candidate_release=candidate,
        basis_release=basis,
        prior_full_receipt=prior,
    )
    repository = root.parents[1]
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "publish candidate basis")

    path, payload = transition.write_publication_lineage(
        root,
        release=candidate,
    )

    assert path.is_file()
    assert payload["release_commit"] == _git(repository, "rev-parse", "HEAD")
    assert payload["release_tree"] == _git(repository, "rev-parse", "HEAD^{tree}")
    assert transition.resolve_publication_lineage(
        root,
        release=candidate,
    )[1] == payload

    (root / ".gitignore").write_text("runs/\nchanged\n", encoding="utf-8")
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="no valid publication lineage",
    ):
        transition.resolve_publication_lineage(root, release=candidate)


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


def test_existing_program_fresh_readback_is_promotion_and_observation_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["delivery_mode"] = "existing_program_fresh_readback"
    basis_reference = {
        "path": "config/step5d/delivery-bases/fixture.json",
        "sha256": "9" * 64,
    }
    payload["delivery_basis"] = basis_reference
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload["fresh_controller_checked_at"] = now.isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    validated, hashes = promotion.validate_delivery(
        root,
        receipt,
        root / promotion.PACKAGE_DIR,
        expected_transaction_id="a" * 32,
        expected_manifest_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        expected_delivery_basis=basis_reference,
    )
    assert validated["delivery_mode"] == "existing_program_fresh_readback"
    release = _release_for_receipt(receipt)
    monkeypatch.setattr(
        delivery_module,
        "require_receipt_delivery_basis",
        lambda *_args, **_kwargs: basis_reference,
    )
    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        transaction_id="a" * 32,
        release=release,
        now=now,
    )
    assert observation["triplet_sha256"] == hashes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fresh_controller_sha_verified", False),
        ("readback_source", "prior_full_readback"),
        ("target_dir", "/programs/andyl/other"),
    ],
)
def test_existing_program_readback_receipt_fails_closed_on_binding_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["delivery_mode"] = "existing_program_fresh_readback"
    payload[field] = value
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(
        promotion.R009PromotionError,
        match="identity, target, or freshness",
    ):
        promotion.validate_delivery(
            root,
            receipt,
            root / promotion.PACKAGE_DIR,
        )


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

    validated = validate_delivery_observation(
        root,
        observation,
        release=release,
    )
    assert validated == observation
    assert validated["fresh_controller_checked_at"] == (
        now - timedelta(seconds=5)
    ).isoformat()
    changed = json.loads(json.dumps(observation))
    changed["fresh_controller_checked_at"] = now.isoformat()
    with pytest.raises(DeliveryObservationError, match="receipt content differs"):
        validate_delivery_observation(root, changed, release=release)


def test_delivery_observation_age_does_not_expire_exact_content_binding(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    checked_at = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = checked_at.isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)
    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        transaction_id="a" * 32,
        release=release,
        now=checked_at,
    )

    validated = validate_delivery_observation(
        root,
        observation,
        release=release,
    )
    assert validated == observation
    assert validated["fresh_controller_checked_at"] == checked_at.isoformat()


def test_current_release_resolves_latest_content_addressed_delivery_receipt(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    release = _release_for_receipt(receipt)
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    first = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=receipt_sha256,
        transaction_id="a" * 32,
        release=release,
        now=datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc),
    )
    second = {
        **first,
        "recorded_at_unix_ns": first["recorded_at_unix_ns"] + 1,
    }
    for observation in (first, second):
        path = write_indexed_delivery_observation(
            root,
            observation,
            release=release,
        )
        assert path.stem == hashlib.sha256(path.read_bytes()).hexdigest()
    corrupt = delivery_index_path(root, first).parent / f"{'f' * 64}.json"
    corrupt.write_text('{"broken":true}\n', encoding="utf-8")

    resolved_path, resolved = resolve_delivery_observation(
        root,
        release=release,
    )

    assert resolved == second
    assert resolved_path == delivery_index_path(root, second)


def test_explicit_delivery_observation_remains_read_only_compatibility_path(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    release = _release_for_receipt(receipt)
    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        transaction_id="a" * 32,
        release=release,
        now=datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc),
    )
    compatibility = root / "runs/campaign/delivery-observation.json"
    compatibility.parent.mkdir(parents=True)
    compatibility.write_text(json.dumps(observation) + "\n", encoding="utf-8")

    resolved_path, resolved = resolve_delivery_observation(
        root,
        release=release,
        compatibility_path=compatibility,
    )

    assert resolved_path == compatibility
    assert resolved == observation


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
    contract_path = root / "release-contract.json"

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
        assert events[-2:] == [
            "evidence:delivery-observation.json",
            "evidence:indexed-delivery.json",
        ]
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
            "_validate_candidate_and_certificate",
            side_effect=lambda *_args: events.append("certify") or release,
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
        mock.patch.object(transaction, "create_delivery_basis"),
        mock.patch.object(transaction.promote, "promote", side_effect=fake_promote),
        mock.patch.object(
            transaction,
            "load_current_release",
            side_effect=lambda _root: events.append("load") or release,
        ),
        mock.patch.object(
            transaction,
            "build_delivery_observation",
            side_effect=lambda *args, **kwargs: events.append("observe")
            or {
                "schema": "fixture",
                "release_manifest_sha256": release.manifest_sha256,
            },
        ),
        mock.patch.object(
            transaction,
            "write_indexed_delivery_observation",
            side_effect=lambda *_args, **_kwargs: events.append(
                "evidence:indexed-delivery.json"
            )
            or exact.append(evidence_output.with_name("indexed-delivery.json"))
            or evidence_output.with_name("indexed-delivery.json"),
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
                "--release-certificate",
                str(contract_path),
                "--evidence-output",
                str(evidence_output),
            ]
        ) == 0

    assert events == [
        "certify",
        "lock",
        "upload",
        "observe",
        "evidence:delivery-observation.json",
        "evidence:indexed-delivery.json",
        "promote",
        "load",
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
    assert exact[0] == exact[3]
    assert exact[4] == (root / promotion.PACKAGE_DIR).resolve()
    assert exact[1] == evidence_output.resolve()
    assert exact[2] == evidence_output.with_name("indexed-delivery.json").resolve()
    token = upload_arguments[upload_arguments.index("--upload-transaction-id") + 1]
    assert promotion_arguments == {
        "expected_transaction_id": token,
        "expected_manifest_sha256": hashlib.sha256(exact[0].read_bytes()).hexdigest(),
        "expected_candidate_manifest_sha256": release.manifest_sha256,
        "expected_delivery_basis": None,
    }


def test_readback_only_transaction_adopts_exact_candidate_without_upload_or_load(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    artifact_sha = {
        extension: hashlib.sha256(
            (artifact_dir / f"{promotion.PROGRAM}{extension}").read_bytes()
        ).hexdigest()
        for extension in promotion.EXTENSIONS
    }
    release = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=promotion.PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.urp",
        artifact_sha256=artifact_sha,
        tp_runtime_identity={"protocol_version": 1},
    )
    evidence_output = root / "runs/campaign/delivery-observation.json"
    upload_arguments: list[str] = []

    def fake_upload(arguments: list[str]) -> int:
        upload_arguments.extend(arguments)
        transaction_id = arguments[arguments.index("--upload-transaction-id") + 1]
        result_path = Path(arguments[arguments.index("--manifest-path-output") + 1])
        receipt = root / "runs/readback/manifest.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps({"upload_transaction_id": transaction_id}) + "\n",
            encoding="utf-8",
        )
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "ur10e_upload_result_v1",
                    "upload_transaction_id": transaction_id,
                    "manifest_path": str(receipt),
                    "manifest_sha256": hashlib.sha256(
                        receipt.read_bytes()
                    ).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return 0

    with (
        mock.patch.object(transaction, "require_runtime_profile", return_value={}),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_certificate",
            return_value=release,
        ),
        mock.patch.object(
            transaction,
            "load_current_release",
            return_value=release,
        ) as load_current,
        mock.patch.object(transaction, "owner_dependency", return_value={
            "path": "/verified/helper.py",
            "sha256": "a" * 64,
        }),
        mock.patch.object(
            transaction,
            "acquire_controller_mutation_locks",
            return_value=[object()],
        ),
        mock.patch.object(transaction.upload, "_main", side_effect=fake_upload),
        mock.patch.object(
            transaction,
            "create_delivery_basis",
        ) as create_basis,
        mock.patch.object(
            transaction,
            "delivery_basis_reference",
            return_value={
                "path": "config/step5d/delivery-bases/fixture.json",
                "sha256": "9" * 64,
            },
        ),
        mock.patch.object(
            transaction.promote,
            "promote",
            return_value={"manifest_sha256": release.manifest_sha256},
        ),
        mock.patch.object(
            transaction,
            "build_delivery_observation",
            return_value={
                "schema": "fixture",
                "release_manifest_sha256": release.manifest_sha256,
            },
        ),
        mock.patch.object(
            transaction,
            "write_indexed_delivery_observation",
            return_value=evidence_output.with_name("indexed-delivery.json"),
        ),
        mock.patch.object(transaction, "atomic_json"),
        mock.patch.object(transaction, "release_controller_mutation_locks"),
    ):
        assert transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--release-candidate",
                str(root / "candidate.json"),
                "--release-certificate",
                str(root / "release-contract.json"),
                "--evidence-output",
                str(evidence_output),
                "--readback-only-existing",
                "--prior-full-readback-receipt",
                str(root / "prior-full-readback.json"),
            ]
        ) == 0

    assert "--readback-only-existing" in upload_arguments
    assert "--force-upload-readback" not in upload_arguments
    assert upload_arguments[
        upload_arguments.index("--delivery-basis-path") + 1
    ] == "config/step5d/delivery-bases/fixture.json"
    assert upload_arguments[
        upload_arguments.index("--delivery-basis-sha256") + 1
    ] == "9" * 64
    assert load_current.call_count == 1
    assert create_basis.call_args.kwargs == {
        "candidate_release": release,
        "basis_release": release,
        "prior_full_receipt": root / "prior-full-readback.json",
    }
    assert not hasattr(transaction, "load_current_release_for_compatible_readback")


def test_readback_only_get_failure_prevents_evidence_and_promotion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-23T0000HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R012",
    )
    candidate = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=promotion.PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.urp",
        artifact_sha256={
            extension: "a" * 64 for extension in promotion.EXTENSIONS
        },
        tp_runtime_identity={"protocol_version": 1},
    )

    with (
        mock.patch.object(transaction, "require_runtime_profile", return_value={}),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_certificate",
            return_value=candidate,
        ),
        mock.patch.object(
            transaction,
            "load_current_release",
            return_value=candidate,
        ) as load_current,
        mock.patch.object(transaction, "create_delivery_basis"),
        mock.patch.object(
            transaction,
            "delivery_basis_reference",
            return_value={
                "path": "config/step5d/delivery-bases/fixture.json",
                "sha256": "9" * 64,
            },
        ),
        mock.patch.object(
            transaction,
            "owner_dependency",
            return_value={
                "path": "/verified/helper.py",
                "sha256": "a" * 64,
            },
        ),
        mock.patch.object(
            transaction,
            "acquire_controller_mutation_locks",
            return_value=[object()],
        ),
        mock.patch.object(
            transaction.upload,
            "_main",
            side_effect=RuntimeError(
                "existing local/controller/readback triplet SHA closure differs"
            ),
        ) as upload,
        mock.patch.object(transaction, "atomic_json") as write_evidence,
        mock.patch.object(transaction.promote, "promote") as promote_release,
        mock.patch.object(transaction, "release_controller_mutation_locks") as unlock,
        pytest.raises(RuntimeError, match="triplet SHA closure differs"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--release-candidate",
                str(root / "candidate.json"),
                "--release-certificate",
                str(root / "release-contract.json"),
                "--evidence-output",
                str(root / "runs/campaign/delivery-observation.json"),
                "--readback-only-existing",
                "--prior-full-readback-receipt",
                str(root / "prior-full-readback.json"),
            ]
        )

    assert "--readback-only-existing" in upload.call_args.args[0]
    write_evidence.assert_not_called()
    promote_release.assert_not_called()
    load_current.assert_not_called()
    unlock.assert_called_once()


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


def test_transaction_contract_failure_precedes_controller_lock_and_upload(
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
            "_validate_candidate_and_certificate",
            side_effect=RuntimeError("release certificate differs"),
        ) as certificate_gate,
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="release certificate differs"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--release-candidate",
                str(root / "candidate.json"),
                "--release-certificate",
                str(root / "release-contract.json"),
                "--evidence-output",
                str(evidence_output),
            ]
        )

    certificate_gate.assert_called_once()
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
