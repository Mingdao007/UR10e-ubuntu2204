from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.runtime_calibration import stable_cuda_environment  # noqa: E402


def test_real_production_startup_prewarm_needs_no_ignored_legacy_csv(
    tmp_path: Path,
) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip("hosted CI has no Pinocchio/CUDA runtime; HIL preflight is authoritative")
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
