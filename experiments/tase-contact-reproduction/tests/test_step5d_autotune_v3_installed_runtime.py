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


def test_canonical_shell_rehearses_v3_no_arm_ready_without_network(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "bridge-start-context.json"
    context = context_builder.build_context(
        ROOT,
        plant_epoch=1,
        runtime_environment={
            "capture_mode": "canonical_no_network_rehearsal",
            "scheduler": {"policy_name": "SCHED_OTHER", "priority": 0, "nice": 0},
        },
    )
    context_builder.write_once(context_path, context.document())

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

real_python = os.environ["STEP5D_REHEARSAL_REAL_PYTHON"]
if len(sys.argv) > 1 and sys.argv[1] == "-c":
    os.execv(real_python, [real_python, *sys.argv[1:]])

target = Path(sys.argv[1]).name if len(sys.argv) > 1 else ""
arguments = sys.argv[2:]

def value(name):
    index = arguments.index(name)
    return arguments[index + 1]

if target == "preflight_step5d_autotune_v3.py":
    import preflight_step5d_autotune_v3 as production
    from step5d_autotune_v3.readiness import require_bridge_start

    _, bridge = require_bridge_start(production.ROOT, Path(value("--bridge-start-context")))
    payload = {{
        "schema": production.SCHEMA,
        "ok": True,
        "fresh": True,
        "candidate_stage_id": production.RELEASE_STAGE_ID,
        "control_profile_id": production.CONTROL_PROFILE_ID,
        "tp_program_id": production.TP_PROGRAM_ID,
        "identity": bridge.identity,
        "controller_identity_sha256": "0" * 64,
        "predicates": {{name: {{"ok": True}} for name in production.PREDICATE_NAMES}},
    }}
    output = Path(value("--output"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(0)

if target == "run_step5d_autotune_v3_live.py":
    import run_step5d_autotune_v3_live as production
    import run_step5d_autotune_v3_bridge as bridge_wrapper
    from step5d_autotune_v3.readiness import require_bridge_start

    _, bridge = require_bridge_start(
        production.ROOT, Path(value("--bridge-start-context"))
    )
    production._validate_preflight(Path(value("--preflight")), bridge.identity)
    prewarm = bridge_wrapper.check_v3_runtime_prewarm([
        "--bridge-mode", "line",
        "--bridge-profile", "step5d_strict_rnn_autotune_v1",
        "--step5d-rnn-backend", "numpy",
        "--step5d-rnn-inner-iterations", "4",
    ])
    if prewarm.get("ok") is not True:
        raise SystemExit("production bridge prewarm failed")
    if Path(value("--campaign-arming-context")).exists():
        raise SystemExit("rehearsal must not consume an arming context")
    print("V3_BRIDGE_READY_NO_ARM", flush=True)
    raise SystemExit(0)

os.execv(real_python, [real_python, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    environment = {
        "HOME": os.environ["HOME"],
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.pathsep.join((str(shim_dir), "/usr/bin", "/bin")),
        "STEP5D_REHEARSAL_REAL_PYTHON": sys.executable,
    }
    output_root = tmp_path / "rehearsal-output"
    completed = subprocess.run(
        [
            str(ROOT / "scripts/step5d-autotune-v3.sh"),
            "bridge",
            "--output-root",
            str(output_root),
            "--bridge-start-context",
            str(context_path),
            "--campaign-arming-context",
            str(tmp_path / "not-created-campaign-arming-context.json"),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.rstrip().endswith("V3_BRIDGE_READY_NO_ARM")
    assert not (tmp_path / "not-created-campaign-arming-context.json").exists()
