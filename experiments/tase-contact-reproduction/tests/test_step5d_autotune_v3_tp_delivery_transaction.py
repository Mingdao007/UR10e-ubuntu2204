from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import promote_step5d_autotune_v3_delivery as promotion  # noqa: E402
import run_step5d_autotune_v3_tp_transaction as transaction  # noqa: E402


def _fixture_manifest(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "experiment"
    local = root / promotion.PACKAGE_DIR
    readback = root / "runs" / f"controller_readback_{promotion.PROGRAM}_fixture"
    local.mkdir(parents=True)
    readback.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for extension in promotion.EXTENSIONS:
        source = ROOT / promotion.PACKAGE_DIR / f"{promotion.PROGRAM}{extension}"
        data = source.read_bytes()
        (local / source.name).write_bytes(data)
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
    payload, hashes = promotion.validate_delivery(root, manifest)
    assert payload["upload_transaction_id"] == "a" * 32
    assert hashes == payload["sha256"]["readback"]

    payload["sha256"]["controller"][".script"] = "0" * 64
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(promotion.PromotionError, match="SHA closure"):
        promotion.validate_delivery(root, manifest)


def test_transaction_passes_exact_uploader_manifest_to_promotion(tmp_path: Path) -> None:
    root = tmp_path / "experiment"
    root.mkdir()
    events: list[str] = []
    exact: list[Path] = []

    def fake_upload(arguments: list[str]) -> int:
        events.append("upload")
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
        mock.patch.object(transaction.promote, "promote", side_effect=lambda _root, manifest: events.append("promote") or exact.append(manifest)),
        mock.patch.object(transaction, "release_controller_mutation_locks", side_effect=lambda _handles: events.append("release")),
    ):
        assert transaction.main(["--root", str(root)]) == 0

    assert events == ["lock", "upload", "promote", "release"]
    assert exact[0] == exact[1]
