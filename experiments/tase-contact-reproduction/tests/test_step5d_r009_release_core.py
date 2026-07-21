from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as builder  # noqa: E402
import run_step5d_autotune_v3_live as live_launcher  # noqa: E402
from step5d_autotune_batch_plan import initialize_rolling_plan  # noqa: E402
from step5d_autotune_v3.admission import verify_first_row_admission  # noqa: E402
from step5d_autotune_v3.atomic_release import (  # noqa: E402
    AtomicReleaseError,
    AtomicReleasePublisher,
    canonical_bytes,
)
from step5d_autotune_v3.release_identity import (  # noqa: E402
    CURRENT_POINTER_SCHEMA,
    RELEASE_MANIFEST_SCHEMA,
    REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS,
    ReleaseIdentityError,
    load_current_release,
)
from step5d_autotune_v3.release_verifier import (  # noqa: E402
    REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS,
    ReleaseVerificationError,
    verify_release_manifest,
)


PROGRAM = "step5d_strict_rnn_autotune_v3_r009"


def _sha(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _release_fixture(
    root: Path,
    *,
    bad_registers: bool = False,
    bad_write_order: bool = False,
) -> Path:
    stamp = "2026-07-21T1200HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R009"
    script = builder.build_package_script(stamp)
    numeric = {
        "input_integer_registers": list(range(24, 32)),
        "output_integer_registers": list(range(24, 35)),
    }
    if bad_registers:
        script = script.replace("read_input_integer_register(31)", "read_input_integer_register(30)")
        script = script.replace("write_output_integer_register(34,", "write_output_integer_register(33,")
        numeric = {
            "input_integer_registers": list(range(24, 31)),
            "output_integer_registers": list(range(24, 34)),
        }
    if bad_write_order:
        script = script.replace(
            "  write_output_integer_register(26, state)\n"
            "  write_output_integer_register(30, consumed_command_seq)",
            "  write_output_integer_register(30, consumed_command_seq)\n"
            "  write_output_integer_register(26, state)",
        )
    txt = builder.build_txt(stamp).encode()
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

    generated_path = root / "generated/numeric-sanity.json"
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    generated_path.write_text(json.dumps(numeric), encoding="utf-8")
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
    mirror_path = root / "config/mirror.json"
    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    mirror_path.write_text('{"program":"r009"}\n', encoding="utf-8")
    triplet = {extension: reference["sha256"] for extension, reference in artifacts.items()}
    readback = {
        "status": "controller read-back verified",
        "verified": True,
        "program": PROGRAM,
        "triplet_sha256": triplet,
    }
    readback_path = root / "config/readback.json"
    readback_path.write_bytes(canonical_bytes(readback))

    manifest = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "identity": {
            "program_id": PROGRAM,
            "release_stage_id": "step5d_strict_rnn_autotune_v3",
            "control_profile_id": "step5d_strict_rnn_autotune_v1",
            "protocol_id": "v3_full_home_rolling_arm_v1",
            "normal_max_rate_rad_s": 0.1,
            "execution_profile_id": "nf100-slew050-a050",
            "execution_profile_integer_id": 633,
        },
        "artifacts": artifacts,
        "controller_readback": {
            "path": "config/readback.json",
            "sha256": _sha(readback_path.read_bytes()),
            "triplet_sha256": triplet,
            "fresh_get": True,
        },
        "runtime_policy": {"state_78_watchdog_s": 30.0},
        "optimizer_policy": {"group_by": "ControlCandidateUid"},
        "source_fingerprints": source_fingerprints,
        "generated_files": {"generated/numeric-sanity.json": _sha(generated_path.read_bytes())},
        "compatibility_mirrors": {"config/mirror.json": _sha(mirror_path.read_bytes())},
        "verification": {
            "canonical_verifier": "independent_script_urp_v1",
            "repository_source_root_depth": 0,
            "repository_source_fingerprints": repository_source_fingerprints,
        },
    }
    encoded = canonical_bytes(manifest)
    digest = _sha(encoded)
    manifest_path = root / f"config/step5d/releases/{digest}/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
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


def test_exact_first_row_admission_closes_plan_overlay_wrapper_and_tp_commit(
    tmp_path: Path,
) -> None:
    _release_fixture(tmp_path)
    release = load_current_release(tmp_path)
    campaign_root = tmp_path / "campaign"
    initialize_rolling_plan(
        campaign_root / "control/candidate_plan.json",
        campaign_id="admission-campaign",
    )
    live_launcher._ensure_initial_batch(
        campaign_root=campaign_root,
        campaign_id="admission-campaign",
        launch_profile_path=(
            ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
        ),
    )
    report = verify_first_row_admission(
        tmp_path,
        campaign_root=campaign_root,
        launch_profile_path=(
            ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
        ),
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


def test_current_pointer_has_no_fallback_and_mirror_drift_fails_closed(
    tmp_path: Path,
) -> None:
    manifest = _release_fixture(tmp_path)
    release = load_current_release(tmp_path)
    assert release.manifest_path == manifest.relative_to(tmp_path).as_posix()
    (tmp_path / "config/mirror.json").write_text('{"program":"drift"}\n', encoding="utf-8")
    with pytest.raises(ReleaseIdentityError, match="fingerprint drifted"):
        load_current_release(tmp_path)
    pointer = tmp_path / "config/step5d/current.json"
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload["latest"] = True
    pointer.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseIdentityError, match="manifest path and SHA only"):
        load_current_release(tmp_path)


def test_active_release_source_fingerprint_drift_fails_closed(
    tmp_path: Path,
) -> None:
    _release_fixture(tmp_path)
    relative = "tools/step5d_autotune_coordinator.py"
    source = tmp_path / relative
    source.write_text("# drifted coordinator source\n", encoding="utf-8")

    with pytest.raises(ReleaseIdentityError, match="source file fingerprint drifted"):
        load_current_release(tmp_path)


def test_atomic_publisher_requires_and_runs_verifier_before_bundle_rename(
    tmp_path: Path,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    manifest = {"schema": "fixture"}
    with pytest.raises(AtomicReleaseError, match="verifier is required"):
        publisher.publish(
            manifest=manifest,
            bundle_files={"mirror/value.txt": b"new\n"},
            compatibility_targets={"active/value.txt": "mirror/value.txt"},
        )
    order: list[str] = []
    result = publisher.publish(
        manifest=manifest,
        bundle_files={"mirror/value.txt": b"new\n"},
        compatibility_targets={"active/value.txt": "mirror/value.txt"},
        stage_verifier=lambda stage, path, digest: order.append("verified"),
        crash_hook=order.append,
    )
    assert order.index("verified") < order.index("staging_verified") < order.index("bundle_renamed")
    assert (tmp_path / "active/value.txt").read_bytes() == b"new\n"
    pointer = json.loads((tmp_path / "config/step5d/current.json").read_text())
    assert pointer["manifest_sha256"] == result["manifest_sha256"]


@pytest.mark.parametrize(
    "cut",
    [
        "staging_fsynced",
        "staging_verified",
        "bundle_renamed",
        "compatibility_mirror_1",
        "before_pointer_write",
    ],
)
def test_atomic_crash_cuts_leave_old_pointer_until_pointer_write(
    tmp_path: Path,
    cut: str,
) -> None:
    publisher = AtomicReleasePublisher(tmp_path)
    publisher.pointer.parent.mkdir(parents=True, exist_ok=True)
    publisher.pointer.write_bytes(b"old-pointer\n")

    def crash(name: str) -> None:
        if name == cut:
            raise RuntimeError(cut)

    with pytest.raises(RuntimeError, match=cut):
        publisher.publish(
            manifest={"schema": "fixture", "cut": cut},
            bundle_files={"mirror/value.txt": b"new\n"},
            compatibility_targets={"active/value.txt": "mirror/value.txt"},
            stage_verifier=lambda stage, path, digest: None,
            crash_hook=crash,
        )
    assert publisher.pointer.read_bytes() == b"old-pointer\n"
