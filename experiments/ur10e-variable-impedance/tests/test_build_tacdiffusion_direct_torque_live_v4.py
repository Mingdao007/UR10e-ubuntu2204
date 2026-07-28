import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools" / "build_tacdiffusion_direct_torque_live_v4.py"
REFERENCE = (
    ROOT.parent
    / "tase-contact-reproduction"
    / "runs"
    / "tacdiffusion"
    / "passive_remote_baseline_20260726"
    / "unknown_surface_anchor_circle_no_contact_2s_reference_v2.json"
)


def _build(
    tmp_path: Path,
    reference: Path = REFERENCE,
    *,
    friction_profile: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(BUILDER),
        "--reference",
        str(reference),
        "--receiver-source",
        str(tmp_path / "receiver.script"),
        "--manifest",
        str(tmp_path / "manifest.json"),
    ]
    if friction_profile is not None:
        command.extend(["--friction-profile", friction_profile])
    return subprocess.run(
        command,
        cwd=ROOT.parents[1],
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def test_bundle_records_complete_numeric_sanity(tmp_path: Path) -> None:
    if not REFERENCE.is_file():
        pytest.skip("fresh passive reference artifact is unavailable")
    completed = _build(tmp_path)
    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    sanity = manifest["numeric_sanity"]
    assert sanity["ok"]
    assert sanity["claim_class"] == "offline_numeric_sanity_no_live_actions"
    assert sanity["controller_target"] == "secondary_client_urscript_port_30002"
    assert sanity["controller_loop_hz"] == 500
    assert sanity["capture_duration_s"] == 2.0
    assert sanity["row_count"] == 1001
    assert sanity["design_radius_m"] == pytest.approx(0.001)
    assert sanity["maximum_anchor_translation_m"] <= 0.002
    assert sanity["maximum_speed_m_s"] <= 0.002
    assert sanity["maximum_acceleration_m_s2"] <= 0.005
    assert sanity["maximum_orientation_error_rad"] <= 1e-6
    assert sanity["normal_half_width_m"] == pytest.approx(0.002)
    assert sanity["target_load_n"] == 0.0
    assert sanity["preload_n"] == 0.0
    assert sanity["feedforward_wrench"] == [0.0] * 6


def test_bundle_records_ur_default_v2_friction_diagnostic(tmp_path: Path) -> None:
    if not REFERENCE.is_file():
        pytest.skip("fresh passive reference artifact is unavailable")
    completed = _build(
        tmp_path,
        friction_profile="ur_default_v2_diagnostic",
    )
    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["friction_profile"] == "ur_default_v2_diagnostic"
    assert manifest["viscous_scale"] == [0.9, 0.9, 0.8, 0.9, 0.9, 0.9]
    assert manifest["coulomb_scale"] == [0.8, 0.8, 0.7, 0.8, 0.8, 0.8]
    assert manifest["gates"]["friction_compensation"] == (
        "ur_default_v2_diagnostic"
    )


def test_builder_rejects_reference_that_is_not_one_mm(tmp_path: Path) -> None:
    if not REFERENCE.is_file():
        pytest.skip("fresh passive reference artifact is unavailable")
    payload = json.loads(REFERENCE.read_text(encoding="utf-8"))
    payload["trajectory"]["max_curvature_m_inv"] = 500.0
    unsigned = dict(payload)
    unsigned.pop("content_sha256")
    import hashlib

    payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    bad_reference = tmp_path / "bad_reference.json"
    bad_reference.write_text(
        json.dumps(payload, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    output = tmp_path / "output"
    output.mkdir()
    completed = _build(output, bad_reference)
    assert completed.returncode == 2
    assert "design radius is not 1 mm" in completed.stderr
