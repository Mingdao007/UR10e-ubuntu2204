from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _top_level_import_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_live_entrypoint_does_not_eager_import_bridge_runner_or_optimizer() -> None:
    modules = _top_level_import_modules(
        ROOT / "tools/run_step5d_autotune_v3_live.py"
    )
    forbidden = {
        "run_step5d_autotune_campaign",
        "run_step5d_autotune_v3_bridge",
        "step5d_autotune_v3.optimizer_protocol",
        "step5d_autotune_v3.optimizer_deployment",
        "step5d_autotune_v3.campaign_prepare",
    }
    assert modules.isdisjoint(forbidden)


def test_release_identity_has_no_import_time_source_closure() -> None:
    script = """
import json
import sys

import step5d_autotune_v3.release_identity  # noqa: F401

loaded = [
    name
    for name in sys.modules
    if name.endswith(".source_closure") or name == "source_closure"
]
print(json.dumps(sorted(loaded)))
"""
    env = os.environ.copy()
    python_path = [str(ROOT / "tools"), str(ROOT)]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    run = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = json.loads(run.stdout.strip())
    assert not loaded


def test_campaign_prepare_surface_has_no_runtime_or_optimizer_imports() -> None:
    modules = _top_level_import_modules(
        ROOT / "tools/prepare_step5d_autotune_launch.py"
    )
    forbidden = {
        "run_step5d_autotune_campaign",
        "run_step5d_autotune_v3_bridge",
        "step5d_autotune_backend",
        "step5d_autotune_v3.optimizer_protocol",
        "step5d_autotune_v3.optimizer_deployment",
    }
    assert modules.isdisjoint(forbidden)
