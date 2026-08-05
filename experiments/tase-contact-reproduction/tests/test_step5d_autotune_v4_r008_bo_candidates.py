"""Offline tests: r008 BO candidate budget stays inside the CUDA timeout envelope."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.lattice import neighbors  # noqa: E402
from step5d_autotune_v4_r008.lattice import (  # noqa: E402
    assert_r006_accepts,
    default_anchor,
    default_box,
    point_from_physical,
)
from step5d_autotune_v4_r008.optimizer import (  # noqa: E402
    CANDIDATE_SOBOL,
    propose_candidates,
)
from step5d_autotune_v4_r008.optimizer_keepalive import (  # noqa: E402
    R008_KEEPALIVE_WORKER_MODULE,
    r008_optimizer_keepalive_scope,
)
from step5d_autotune_v4_r008.optimizer_timeout import (  # noqa: E402
    R008_OPTIMIZER_TIMEOUT_S,
    r008_optimizer_timeout_scope,
)
from step5d_optimizer_runtime import OptimizerSubprocessClient  # noqa: E402


def test_candidate_sobol_budget_is_64() -> None:
    # Batched qLogNEI (r008 Stage A) removed the serial ~4.7s/candidate tax that
    # forced the temporary cut to 32; restore power-of-2 Sobol width.
    assert CANDIDATE_SOBOL == 64


def test_propose_candidates_count_and_r006_accept() -> None:
    points = propose_candidates(default_box(), count=CANDIDATE_SOBOL, seed=8)
    assert len(points) == CANDIDATE_SOBOL
    # Convert back through r008 physical projection for lattice go/no-go.
    r008_points = tuple(
        point_from_physical(
            force_p_gain=p.p_gain,
            force_damping=p.d_gain,
            force_i_gain=p.i_gain,
            normal_filter_tau_s=p.tau_s,
            orientation_ko=p.ko,
            motion_kp=p.kp,
        )
        for p in points
    )
    assert_r006_accepts(r008_points)


def test_ask_choice_pool_is_not_4096_scale() -> None:
    sobol = propose_candidates(default_box(), count=CANDIDATE_SOBOL, seed=8)
    local = neighbors(default_anchor().to_parameter_point())
    choices = (*sobol, *local)
    assert len(choices) <= CANDIDATE_SOBOL + 16
    assert len(choices) < 256
    assert len(choices) != 4096 + len(local)


def test_optimizer_timeout_scope_defaults_to_600s() -> None:
    assert R008_OPTIMIZER_TIMEOUT_S == 600.0
    with r008_optimizer_timeout_scope():
        client = OptimizerSubprocessClient(worker_module="step5d_autotune_v4_r006.optimizer_worker")
        assert client.timeout_s == pytest.approx(600.0)
    # Outside the scope, the frozen default returns.
    client = OptimizerSubprocessClient(worker_module="step5d_autotune_v4_r006.optimizer_worker")
    assert client.timeout_s == pytest.approx(180.0)


def test_optimizer_timeout_scope_respects_explicit_timeout() -> None:
    with r008_optimizer_timeout_scope():
        client = OptimizerSubprocessClient(
            worker_module="step5d_autotune_v4_r006.optimizer_worker",
            timeout_s=12.0,
        )
        assert client.timeout_s == pytest.approx(12.0)


def test_optimizer_keepalive_scope_patches_request() -> None:
    assert R008_KEEPALIVE_WORKER_MODULE.endswith("optimizer_worker_loop")
    original = OptimizerSubprocessClient.request
    with r008_optimizer_keepalive_scope():
        assert OptimizerSubprocessClient.request is not original
    assert OptimizerSubprocessClient.request is original
