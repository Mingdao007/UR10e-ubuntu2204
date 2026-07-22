from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import promote_step5d_manual_release as promote  # noqa: E402
from step5d_manual_atomic_release import (  # noqa: E402
    ManualAtomicReleasePublisher,
    canonical_bytes,
)
from step5d_manual_queue import enqueue  # noqa: E402
from step5d_manual_runtime import wait_for_request  # noqa: E402


def _fake_fresh_get(tmp_path: Path) -> Path:
    package = ROOT / promote.PACKAGE_DIR
    sha = {}
    for extension in promote.EXTENSIONS:
        source = package / f"{promote.PROGRAM}{extension}"
        target = tmp_path / source.name
        target.write_bytes(source.read_bytes())
        sha[extension] = hashlib.sha256(source.read_bytes()).hexdigest()
    stamp = (package / f"{promote.PROGRAM}.script").read_text().splitlines()[0].removeprefix("# VERSION: ")
    manifest = {
        "status": "controller read-back verified",
        "controller": "root@192.168.1.18",
        "target_dir": promote.TARGET_DIR,
        "validation": {
            "stamp": stamp,
            "program": promote.PROGRAM,
            "target_dir": promote.TARGET_DIR,
            "installation_relative_path": "../../../default",
            "script_node_path": f"{promote.TARGET_DIR}/{promote.PROGRAM}.script",
            "urp_sha256": sha[".urp"],
            "script_sha256": sha[".script"],
            "txt_sha256": sha[".txt"],
        },
        "sha256": {"local": sha, "controller": sha, "readback": sha},
        "delivery_mode": "full_upload_readback",
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": "2026-07-21T15:00:00+08:00",
        "readback_source": "fresh_controller_get",
        "upload_transaction_id": "1" * 32,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def test_manual_release_binds_i1e4_and_does_not_target_v3_pointer() -> None:
    pointer = json.loads((ROOT / promote.MANUAL_POINTER).read_text())
    manifest = json.loads((ROOT / pointer["manifest_path"]).read_text())
    assert manifest["identity"]["protocol_id"] == "v3_full_home_manual_hold_v1"
    assert manifest["default_request"]["force_i_gain"] == 0.0001
    assert manifest["runtime_policy"]["logical_batch_size"] == 1
    assert manifest["runtime_policy"]["home_wait_timeout_s"] is None
    assert pointer["manifest_path"].startswith("config/step5d/manual/releases/")
    assert pointer != json.loads((ROOT / "config/step5d/current.json").read_text())


def test_runner_selects_enqueued_request_without_parameter_tests(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    queue = campaign / "control/manual_queue.json"
    request = enqueue(
        queue,
        campaign_id="manual-campaign-1",
        release_manifest_sha256="a" * 64,
        launch_profile_path=ROOT / "config/step5d/manual/launch_profile.json",
        force_p=0.001,
        force_i=0.0001,
        force_damping=7.0,
        occurrence_nonce="1" * 32,
    )
    selected, payload = wait_for_request(
        queue_path=queue,
        campaign_id="manual-campaign-1",
        release_manifest_sha256="a" * 64,
        completed_sequences=set(),
    )
    assert selected == request
    assert payload["revision"] == 1


def test_manual_atomic_publisher_switches_only_manual_pointer(tmp_path: Path) -> None:
    canonical = tmp_path / "config/step5d/current.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"frozen-r009\n")
    manifest = {"schema": "manual-test", "identity": {"program": "manual"}}
    bundle = {"artifacts/manual.txt": b"manual\n"}

    def verify(stage: Path, path: Path, digest: str) -> None:
        assert path.read_bytes() == canonical_bytes(manifest)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert (stage / "artifacts/manual.txt").read_bytes() == b"manual\n"

    result = ManualAtomicReleasePublisher(tmp_path).publish(
        manifest=manifest,
        bundle_files=bundle,
        compatibility_targets={"config/step5d/manual/view.txt": "artifacts/manual.txt"},
        stage_verifier=verify,
    )
    pointer = json.loads((tmp_path / "config/step5d/manual/current.json").read_text())
    assert pointer["manifest_sha256"] == result["manifest_sha256"]
    assert (tmp_path / "config/step5d/manual/view.txt").read_bytes() == b"manual\n"
    assert canonical.read_bytes() == b"frozen-r009\n"
