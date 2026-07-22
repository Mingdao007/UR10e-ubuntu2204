from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import Evaluation, ForceCandidate, TrialDisposition  # noqa: E402
from step5d_autotune_optimizer import Observation, cuda_botorch_joint_candidates  # noqa: E402


def _observation(index: int, candidate: ForceCandidate, objective: float) -> Observation:
    evaluation = Evaluation(
        trial_uid=f"{index:064x}",
        backend_id="r008-installed-runtime-gate",
        eligible=True,
        disposition=TrialDisposition.OBJECTIVE,
        objective_mae_n=objective,
        force_bias_n=0.0,
        force_std_n=0.05,
        coverage_12_plus_minus_1_ratio=1.0,
        complete_bins=550,
        safe_closure=True,
    )
    return Observation(candidate, evaluation, "nf100-slew050-a050", 1)


def test_installed_cuda_runtime_q4_q5_and_seed_replay() -> None:
    import botorch
    import gpytorch
    import torch

    manifest = json.loads(
        (ROOT / "config/step5/step5d_r008_botorch_runtime.json").read_text()
    )
    lock = ROOT / "config/step5/step5d_r008_botorch_runtime.lock"
    assert sys.version.split()[0] == manifest["python_version"]
    assert torch.__version__ == manifest["torch_version"]
    assert torch.version.cuda == manifest["cuda_runtime"]
    assert botorch.__version__ == manifest["botorch_version"]
    assert gpytorch.__version__ == manifest["gpytorch_version"]
    assert torch.cuda.is_available()
    assert hashlib.sha256(lock.read_bytes()).hexdigest() == manifest[
        "environment_lock_sha256"
    ]

    anchor = ForceCandidate()
    observations = (
        _observation(1, anchor, 0.22),
        _observation(2, anchor, 0.20),
        _observation(3, anchor, 0.18),
        _observation(4, ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0), 0.17),
        _observation(5, ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0), 0.16),
        _observation(6, ForceCandidate.from_log2(p=0.0, damping=0.25, i=0.0), 0.15),
    )
    catalog = tuple(
        ForceCandidate.from_log2(p=p, damping=damping, i=i)
        for i in (-0.25, 0.0, 0.25)
        for p in (-1.0, -0.75, -0.5, 0.5, 0.75, 1.0)
        for damping in (-1.0, -0.5, 0.5, 1.0)
    )
    q4, q4_meta = cuda_botorch_joint_candidates(
        observations, catalog, q=4, seed=8008
    )
    q4_replay, _ = cuda_botorch_joint_candidates(
        observations, catalog, q=4, seed=8008
    )
    assert q4 == q4_replay
    remaining = tuple(item for item in catalog if item not in q4)
    q5, q5_meta = cuda_botorch_joint_candidates(
        observations, remaining, q=5, seed=8009
    )
    assert len(set(q4 + q5)) == 9
    assert anchor not in q4 + q5
    assert q4_meta["device"] == q5_meta["device"] == "cuda:0"
