from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_step5d_autotune_v3 as preflight  # noqa: E402
import build_step5d_autotune_v3_bridge_start_context as context_builder  # noqa: E402
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
import builtins
import json
import sys
import run_step5d_autotune_v3_bridge as wrapper
from step5d_autotune_v3.runtime_calibration import validate_installed_calibration

original_import = builtins.__import__

def reject_pandas(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split('.', 1)[0] == 'pandas':
        raise AssertionError(f'pandas imported during control startup: {name}')
    return original_import(name, globals, locals, fromlist, level)

def reject_legacy_rows(*args, **kwargs):
    raise AssertionError('legacy calibration CSV reached during V3 control startup')

builtins.__import__ = reject_pandas
original_install = wrapper.install_v3_seams

def guarded_install(*args, **kwargs):
    bridge = original_install(*args, **kwargs)
    bridge.step5d_kin.finite_run_rows = reject_legacy_rows
    return bridge

wrapper.install_v3_seams = guarded_install

result = wrapper.check_v3_runtime_prewarm([
    '--bridge-mode', 'line',
    '--bridge-profile', 'step5d_strict_rnn_autotune_v1',
    '--step5d-rnn-backend', 'numpy',
    '--step5d-rnn-inner-iterations', '4',
])
expected = validate_installed_calibration()
if result['tcp_offset_tool0_m'] != list(expected.tcp_offset_tool0_m):
    raise AssertionError('prewarmed TCP offset differs from compact calibration')
if 'pandas' in sys.modules:
    raise AssertionError('pandas remained loaded after control startup')
print(json.dumps(result, sort_keys=True))
"""
    environment = stable_cuda_environment(dict(os.environ))
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(ROOT / "tools"),
            str(ROOT.parents[1] / "src/ur10e_experiment_runtime"),
            environment.get("PYTHONPATH", ""),
        )
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
    assert result["tcp_offset_tool0_m"] == list(
        calibration.validate_installed_calibration().tcp_offset_tool0_m
    )


def test_canonical_shell_resolves_runtime_without_caller_pythonpath() -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(ROOT / "scripts/step5d-autotune-v3.sh"), "status", "--json"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    status = json.loads(completed.stdout)
    assert status["release_readiness"]["selected_release"] == (
        "step5d_strict_rnn_autotune_v3"
    )


def test_canonical_shell_declares_ros_python_runtime_without_caller_pythonpath() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert 'PYTHON_ABI="$(python3 -c' in source
    assert '"/opt/ros/humble/lib/python${PYTHON_ABI}/site-packages"' in source
    assert '"/opt/ros/humble/local/lib/python${PYTHON_ABI}/dist-packages"' in source
    assert 'export PYTHONPATH="${RUNTIME_PYTHONPATH}"' in source
    assert 'export AMENT_PREFIX_PATH=' in source
    assert 'PYTHONPATH:+:${PYTHONPATH}' not in source


def test_installed_runtime_builds_r006_no_arm_bridge_context() -> None:
    context = context_builder.build_context(
        ROOT,
        plant_epoch=1,
        runtime_environment={
            "capture_mode": "offline_r006_no_arm_check",
            "scheduler": {
                "policy_name": "SCHED_OTHER",
                "priority": 0,
                "nice": 0,
            },
        },
    )
    assert context.document()["tp_program_id"] == (
        "step5d_strict_rnn_autotune_v3_r006"
    )
