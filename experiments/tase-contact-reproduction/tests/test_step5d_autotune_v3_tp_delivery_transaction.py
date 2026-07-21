from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as builder  # noqa: E402
import promote_step5d_r009_atomic_release as promotion  # noqa: E402
import run_step5d_autotune_v3_tp_transaction as transaction  # noqa: E402


def _fixture_manifest(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "experiment"
    local = root / promotion.PACKAGE_DIR
    readback = root / "runs" / f"controller_readback_{promotion.PROGRAM}_fixture"
    local.mkdir(parents=True)
    readback.mkdir(parents=True)
    builder.write_triplet(
        local,
        "2026-07-21T1200HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R010",
    )
    hashes: dict[str, str] = {}
    for extension in promotion.EXTENSIONS:
        source = local / f"{promotion.PROGRAM}{extension}"
        data = source.read_bytes()
        (readback / source.name).write_bytes(data)
        hashes[extension] = hashlib.sha256(data).hexdigest()
    manifest = {
        "status": "controller read-back verified",
        "controller": "root@192.168.1.18",
        "target_dir": promotion.TARGET_DIR,
        "validation": {
            "stamp": "fixture",
            "program": promotion.PROGRAM,
            "target_dir": promotion.TARGET_DIR,
            "script_node_path": f"{promotion.TARGET_DIR}/{promotion.PROGRAM}.script",
            "script_sha256": hashes[".script"],
            "txt_sha256": hashes[".txt"],
            "urp_sha256": hashes[".urp"],
        },
        "sha256": {role: dict(hashes) for role in ("local", "controller", "readback")},
        "delivery_mode": "full_upload_readback",
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": "2026-07-21T11:00:33+08:00",
        "readback_source": "fresh_controller_get",
        "upload_transaction_id": "a" * 32,
    }
    path = readback / "manifest.json"
    path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return root, path


def test_delivery_manifest_drives_only_exact_fresh_triplet(tmp_path: Path) -> None:
    root, manifest = _fixture_manifest(tmp_path)
    artifact_dir = root / promotion.PACKAGE_DIR
    payload, hashes = promotion.validate_delivery(root, manifest, artifact_dir)
    assert payload["upload_transaction_id"] == "a" * 32
    assert hashes == payload["sha256"]["readback"]

    payload["sha256"]["controller"][".script"] = "0" * 64
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(promotion.R009PromotionError, match="SHA closure"):
        promotion.validate_delivery(root, manifest, artifact_dir)


def test_transaction_passes_exact_uploader_manifest_to_promotion(tmp_path: Path) -> None:
    root = tmp_path / "experiment"
    root.mkdir()
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        "2026-07-21T1200HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R010",
    )
    events: list[str] = []
    exact: list[Path] = []
    upload_arguments: list[str] = []

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

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks", side_effect=lambda: events.append("lock") or [object()]),
        mock.patch.object(transaction.upload, "_main", side_effect=fake_upload),
        mock.patch.object(
            transaction.promote,
            "promote",
            side_effect=lambda _root, manifest, artifact_dir: (
                events.append("promote"),
                exact.append(manifest),
                exact.append(artifact_dir),
            ),
        ),
        mock.patch.object(transaction, "release_controller_mutation_locks", side_effect=lambda _handles: events.append("release")),
    ):
        assert transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(root / promotion.PACKAGE_DIR),
            ]
        ) == 0

    assert events == ["lock", "upload", "promote", "release"]
    assert upload_arguments[0] == builder.PROGRAM_NAME
    assert any(
        value.startswith("manifest-driven step5d_strict_rnn_autotune_v3_r010")
        for value in upload_arguments
    )
    assert exact[0] == exact[1]
    assert exact[2] == (root / promotion.PACKAGE_DIR).resolve()


def test_transaction_rejects_legacy_deploy_schema_before_lock_or_upload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    generated = builder.write_triplet(
        artifact_dir,
        "2026-07-21T1200HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R010",
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
        "2026-07-21T1200HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R010",
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
