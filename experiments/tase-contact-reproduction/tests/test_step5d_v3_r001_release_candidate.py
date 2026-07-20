from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_bridge as bridge  # noqa: E402
import run_step5d_autotune_v3_live as live_runner  # noqa: E402
from step5d_autotune_v3.arming import BridgeStartContext  # noqa: E402
from step5d_autotune_v3.identity_layers import (  # noqa: E402
    release_basis_fingerprint,
    runtime_environment_fingerprint,
)


def _bridge_context(path: Path) -> BridgeStartContext:
    environment = {
        "schema": "step5d.autotune-v3/runtime-environment-identity-v1",
        "environment": {"capture_mode": "r001_fake_transport_rehearsal"},
    }
    identity = {
        "tick_semantics_fingerprint": "0" * 64,
        "timing_harness_fingerprint": "1" * 64,
        "runtime_environment_fingerprint": runtime_environment_fingerprint(
            environment["environment"]
        ),
        "deployment_fingerprint": "3" * 64,
        "orchestration_fingerprint": "4" * 64,
    }
    identity["release_basis_fingerprint"] = release_basis_fingerprint(
        **identity,
        plant_epoch=1,
    )
    context = BridgeStartContext(
        **identity,
        local_triplet_sha256={
            ".script": "5" * 64,
            ".txt": "6" * 64,
            ".urp": "7" * 64,
        },
        plant_epoch=1,
        deployment_readback_sha256="8" * 64,
        runtime_environment_manifest=environment,
    )
    path.write_text(json.dumps(context.document()), encoding="utf-8")
    return context


def test_r001_ticket_binds_bridge_and_exact_campaign_without_auth_files(
    tmp_path: Path,
) -> None:
    context_path = (tmp_path / "bridge-start-context.json").resolve()
    context = _bridge_context(context_path)
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket_path = (tmp_path / "runtime-ticket.json").resolve()
    ticket = {
        "schema": bridge.TICKET_SCHEMA,
        "parent_pid": os.getppid(),
        "argv_sha256": hashlib.sha256(
            json.dumps(argv, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "launch_id": "9" * 32,
        "scope": "bridge_no_arm",
        "identity": context.identity,
        "launch_profile_fingerprint": "a" * 64,
        "trial_overlay_fingerprint": "b" * 64,
        "release_stage_id": "step5d_strict_rnn_autotune_v3",
        "control_profile_id": "step5d_strict_rnn_autotune_v1",
        "tp_program_id": "step5d_strict_rnn_autotune_v3_r001",
        "bridge_start_context": {
            "path": str(context_path),
            "sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
        },
        "campaign_binding": {
            "campaign_id": "r001-round-a",
            "campaign_epoch": 1,
            "candidate_plan_revision": 1,
            "candidate_plan_sha256": "c" * 64,
            "trial_overlay_plan_sha256": "d" * 64,
        },
    }
    ticket_path.write_text(json.dumps(ticket), encoding="utf-8")

    observed = bridge._strict_ticket(ticket_path, argv)
    assert observed["scope"] == "bridge_no_arm"
    assert observed["tp_program_id"].endswith("_r001")
    assert "campaign_arming_context_path" not in observed
    assert "certification_authorization_path" not in observed


def test_live_runner_uses_the_current_bridge_readiness_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context_path = tmp_path / "bridge-start-context.json"
    observed: list[tuple[Path, Path]] = []

    def require(root: Path, context: Path):
        observed.append((root, context))
        return ({"identity": {}}, SimpleNamespace(identity={}))

    monkeypatch.setattr(live_runner, "require_bridge_start", require)
    (tmp_path / "runtime").mkdir()

    with pytest.raises(FileExistsError):
        live_runner.run(
            SimpleNamespace(
                output_root=tmp_path,
                bridge_start_context=context_path,
            )
        )

    assert observed == [(live_runner.ROOT, context_path)]


def test_canonical_shell_fake_transport_reaches_r001_no_arm(tmp_path: Path) -> None:
    context_path = (tmp_path / "bridge-start-context.json").resolve()
    context = _bridge_context(context_path)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(
        f"""#!{sys.executable}
import hashlib
import json
import os
from pathlib import Path
import sys

real_python = os.environ["STEP5D_REHEARSAL_REAL_PYTHON"]
if len(sys.argv) > 1 and sys.argv[1] == "-c":
    os.execv(real_python, [real_python, *sys.argv[1:]])

target = Path(sys.argv[1]).name
arguments = sys.argv[2:]
def value(name):
    return arguments[arguments.index(name) + 1]

if target == "preflight_step5d_autotune_v3.py":
    import preflight_step5d_autotune_v3 as production
    bridge_path = Path(value("--bridge-start-context"))
    payload = {{
        "schema": production.SCHEMA,
        "ok": True,
        "fresh": True,
        "candidate_stage_id": production.RELEASE_STAGE_ID,
        "control_profile_id": production.CONTROL_PROFILE_ID,
        "tp_program_id": production.TP_PROGRAM_ID,
        "identity": {json.dumps(context.identity)},
        "bridge_start_context_sha256": hashlib.sha256(bridge_path.read_bytes()).hexdigest(),
        "controller_identity_sha256": "e" * 64,
        "predicates": {{name: {{"ok": True}} for name in production.PREDICATE_NAMES}},
    }}
    output = Path(value("--output"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps(payload))
    raise SystemExit(0)

if target == "run_step5d_autotune_v3_live.py":
    import run_step5d_autotune_v3_live as production
    bridge_path = Path(value("--bridge-start-context"))
    production._validate_preflight(
        Path(value("--preflight")),
        {json.dumps(context.identity)},
        bridge_path,
    )
    print("V3_BRIDGE_READY_NO_ARM")
    raise SystemExit(0)

raise SystemExit(99)
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
    completed = subprocess.run(
        [
            str(ROOT / "scripts/step5d-autotune-v3.sh"),
            "bridge",
            "--output-root",
            str(tmp_path / "output"),
            "--bridge-start-context",
            str(context_path),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.rstrip().endswith("V3_BRIDGE_READY_NO_ARM")


def test_active_sources_retire_wrong_path_without_weakening_v1_guards() -> None:
    shell = (ROOT / "scripts/step5d-autotune-v3.sh").read_text()
    live = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text()
    wrapper = (ROOT / "tools/run_step5d_autotune_v3_bridge.py").read_text()
    base = (ROOT / "tools/kunwei_rtde_bridge.py").read_text()

    assert "--campaign-arming-context" not in shell
    assert "certification_session_provider" not in wrapper
    assert "step5d_moving_sphere_enabled = False" in wrapper
    assert 'getattr(args, "step5d_moving_sphere_enabled", False)' in base
    assert '"--campaign-binding"' in live
    assert "READY_FOR_ONE_PLAY_TO_MOVE" in live
    assert 'exchange(robot_host, ["programState"]' in live
    assert '["stop", "programState"]' not in live
    assert "require_bridge_start(" in live
    assert "release_identity = bridge_start.identity" in live
    assert '"identity": release_identity' in live
    assert 'readiness["identity"]' not in live
    assert "execution_readiness.verify" not in live
    assert "require_live=" not in live
