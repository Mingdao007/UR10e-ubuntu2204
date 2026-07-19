from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_step5d_autotune_v3 as preflight  # noqa: E402
from step5d_autotune_v3 import runtime_calibration as calibration  # noqa: E402
from step5d_autotune_v3.runtime_calibration import stable_cuda_environment  # noqa: E402


def test_compact_runtime_calibration_matches_installed_robot_description() -> None:
    artifact = calibration.validate_installed_calibration()
    assert artifact.calibration_hash == "calib_7367377276742883610"
    assert artifact.finite_samples == 13917
    assert artifact.tcp_offset_tool0_m == (
        1.8186503701174852e-06,
        2.2293003722353485e-07,
        0.12209917288991741,
    )
    assert artifact.source_csv_sha256 == (
        "495d6d3ee61d7f59bfb268e79ce083eef23f6de23f40ef9f5030e1b8a9d3f4ae"
    )


def test_runtime_dependency_observation_covers_exact_production_prewarm() -> None:
    observation = preflight.dependency_observation()
    assert observation["ok"] is True
    assert observation["finite_samples"] == 13917
    assert observation["source_csv_sha256"] == (
        "495d6d3ee61d7f59bfb268e79ce083eef23f6de23f40ef9f5030e1b8a9d3f4ae"
    )


def test_real_production_startup_prewarm_needs_no_ignored_legacy_csv(
    tmp_path: Path,
) -> None:
    code = """
import json
import run_step5d_autotune_v3_bridge as wrapper

result = wrapper.check_v3_runtime_prewarm([
    '--bridge-mode', 'line',
    '--bridge-profile', 'step5d_strict_rnn_autotune_v1',
    '--step5d-rnn-backend', 'numpy',
    '--step5d-rnn-inner-iterations', '4',
])
print(json.dumps(result, sort_keys=True))
"""
    environment = stable_cuda_environment(dict(os.environ))
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "tools"), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["rnn_backend"] == "numpy"
