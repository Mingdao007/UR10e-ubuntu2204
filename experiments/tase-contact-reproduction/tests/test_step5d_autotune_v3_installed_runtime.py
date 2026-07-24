from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_step5d_autotune_v3 as preflight  # noqa: E402
import build_step5d_autotune_v3_bridge_start_context as context_builder  # noqa: E402
from step5d_autotune_v3 import runtime_calibration as calibration  # noqa: E402
from step5d_autotune_v3.runtime_environment import (  # noqa: E402
    production_runtime_environment,
)
from step5d_autotune_v3.runtime_installation import (  # noqa: E402
    load_runtime_pointer_integrity,
)


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
    pointer = load_runtime_pointer_integrity(environ=os.environ)
    environment = production_runtime_environment(
        os.environ,
        profile="control",
        runtime_pointer=pointer,
    )
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(ROOT / "tools"),
            str(ROOT.parents[1] / "src/ur10e_experiment_runtime"),
            environment.get("PYTHONPATH", ""),
        )
    ).rstrip(os.pathsep)
    completed = subprocess.run(
        [pointer["profiles"]["control"]["python_executable"], "-c", code],
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
    assert status["schema"] == "step5d.bridge/governed-status-v2"
    assert status["launch_attempt"]["present"] is False
    assert status["predicates"]["play_prompt_ready"] is False
    assert "NO_CANONICAL_LAUNCH_ATTEMPT" in status["blocker"]["reason_codes"]
    assert isinstance(status["blocker"]["reason_codes"], list)
    assert status["next_action"] == "start_canonical_bridge"


def test_status_entrypoint_bootstraps_repository_runtime_under_isolated_python() -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            str(ROOT / "tools/step5d_bridge_status.py"),
            "--help",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Reduce the latest canonical Step5d attempt" in completed.stdout


def test_canonical_shell_declares_ros_python_runtime_without_caller_pythonpath() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert '/usr/bin/python3.10 -B -I "${RUNTIME_RESOLVER}" --shell-binding' in source
    assert '"${CONTROL_PYTHON}" -B -I' in source
    assert '"${EXPERIMENT_ROOT}/tools/step5d_bridge_status.py"' in source
    assert 'exec "${status_command[@]}"' in source
    assert '"${RUNTIME_RESOLVER}" --status-json' not in source
    assert 'PYTHON_ABI="3.10"' in source
    assert '"/opt/ros/humble/lib/python${PYTHON_ABI}/site-packages"' in source
    assert '"/opt/ros/humble/local/lib/python${PYTHON_ABI}/dist-packages"' in source
    assert 'export PYTHONPATH="${RUNTIME_PYTHONPATH}"' in source
    assert 'AMENT_PREFIX_PATH="$(IFS=:; echo "${AMENT_PREFIXES[*]}")"' in source
    assert "export AMENT_PREFIX_PATH" in source
    assert 'export CUPY_CACHE_DIR="${CONTROL_CUPY_CACHE_DIR}"' in source
    assert 'export LD_LIBRARY_PATH="${CONTROL_LD_LIBRARY_PATH}"' in source
    assert 'PYTHONPATH:+:${PYTHONPATH}' not in source


def test_installed_manual_guard_contract_matches_production_bridge() -> None:
    import kunwei_rtde_bridge as production
    import run_step5d_manual_bridge as manual

    manual.require_manual_guard_semantics(production)


def test_installed_runtime_rejects_known_incompatible_bridge_context() -> None:
    with pytest.raises(
        context_builder.BridgeContextBuildError,
        match="known_incompatible_do_not_retry",
    ):
        context_builder.build_context(
            ROOT,
            plant_epoch=1,
            runtime_environment={
                "capture_mode": "offline_no_arm_check",
                "scheduler": {
                    "policy_name": "SCHED_OTHER",
                    "priority": 0,
                    "nice": 0,
                },
            },
        )
