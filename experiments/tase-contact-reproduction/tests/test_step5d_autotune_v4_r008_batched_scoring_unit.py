"""Unit-level equivalence: batched t-batch scores match sequential .item() loop."""

from __future__ import annotations

import itertools
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pytest

torch = pytest.importorskip("torch")


def _require_cuda() -> None:
    if not torch.cuda.is_available():
        pytest.skip("CUDA required for batched qLogNEI unit test")


def _point(i: int):
    from step5d_autotune_v4_r006.lattice import IMode, ParameterPoint

    return ParameterPoint(
        p_step=10 + i,
        d_step=5,
        tau_step=0,
        i_mode=IMode.OFF,
        i_step=None,
        ko_step=2,
        kp_step=2,
    )


def _build_acquisition(n_train: int = 12, n_choices: int = 16, seed: int = 7):
    import gpytorch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.acquisition.objective import GenericMCObjective
    from botorch.models import SingleTaskGP
    from gpytorch.likelihoods import FixedNoiseGaussianLikelihood

    from step5d_autotune_v4_r008.optimizer import feature_map_r008 as _features

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    device = torch.device("cuda:0")
    train_points = [_point(i) for i in range(n_train)]
    choices = tuple(_point(i) for i in range(100, 100 + n_choices))
    train_x = torch.tensor([_features(p) for p in train_points], dtype=torch.double, device=device)
    train_y = torch.linspace(0.5, 2.0, n_train, dtype=torch.double, device=device).unsqueeze(-1)
    train_yvar = torch.full_like(train_y, 2.5e-5)

    class ConditionalMatern52Kernel(gpytorch.kernels.Kernel):
        has_lengthscale = True

        def __init__(self) -> None:
            super().__init__(ard_num_dims=7)
            self.shared = gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=7)
            self.same_mode = gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=7)
            self.i_on_only = gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=7)

        def forward(self, x1, x2, diag=False, **params):
            shared = self.shared(x1, x2, diag=diag, **params)
            same = self.same_mode(x1, x2, diag=diag, **params)
            ion = self.i_on_only(x1, x2, diag=diag, **params)
            if diag:
                return shared + same + ion
            x1_mode = x1[..., -1].unsqueeze(-1)
            x2_mode = x2[..., -1].unsqueeze(-2)
            mode_equal = (x1_mode == x2_mode).to(dtype=x1.dtype)
            ion_mask = (x1_mode > 0.5) & (x2_mode > 0.5)
            return shared + mode_equal * same + ion_mask.to(dtype=x1.dtype) * ion

    model = SingleTaskGP(
        train_X=train_x,
        train_Y=train_y,
        train_Yvar=train_yvar,
        likelihood=FixedNoiseGaussianLikelihood(noise=train_yvar.squeeze(-1)),
        covar_module=gpytorch.kernels.ScaleKernel(ConditionalMatern52Kernel()),
    ).to(device=device, dtype=torch.double)
    model.eval()
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=train_x,
        X_pending=None,
        objective=objective,
        prune_baseline=False,
    )
    return acquisition, choices, device


def _score_sequential(acquisition, choices, q: int, device, torch_mod):
    from step5d_autotune_v4_r008.optimizer import feature_map_r008 as _features

    scored = []
    with torch_mod.no_grad():
        for batch in itertools.combinations(choices, q):
            batch_x = torch_mod.tensor(
                [[_features(point) for point in batch]],
                dtype=torch_mod.double,
                device=device,
            )
            value = acquisition(batch_x)
            scored.append(
                (
                    float(value.detach().cpu().item()),
                    tuple(item.key for item in batch),
                    batch,
                )
            )
    selected = max(scored, key=lambda row: (row[0], tuple(str(v) for v in row[1])))[2]
    return selected, [row[0] for row in scored]


@pytest.mark.parametrize("q,n_choices", [(1, 16), (1, 44), (4, 8)])
def test_batched_scores_match_sequential(q: int, n_choices: int) -> None:
    _require_cuda()
    from step5d_autotune_v4_r008.optimizer_worker_batched import _score_combination_batches

    acquisition, choices, device = _build_acquisition(n_choices=n_choices, seed=11)
    torch.manual_seed(11)
    torch.cuda.manual_seed_all(11)
    seq_selected, seq_scores = _score_sequential(acquisition, choices, q, device, torch)

    acquisition2, choices2, device2 = _build_acquisition(n_choices=n_choices, seed=11)
    torch.manual_seed(11)
    torch.cuda.manual_seed_all(11)
    batch_selected, batch_scores, scoring = _score_combination_batches(
        acquisition2, choices2, q, device=device2, torch=torch
    )
    assert scoring.startswith("batched_tbatch")
    assert list(batch_selected[0].key) == list(seq_selected[0].key)
    assert [list(p.key) for p in batch_selected] == [list(p.key) for p in seq_selected]
    assert len(batch_scores) == len(seq_scores)
    max_delta = max(abs(a - b) for a, b in zip(batch_scores, seq_scores))
    assert max_delta <= 1e-10


def test_candidate_sobol_restored_to_64() -> None:
    from step5d_autotune_v4_r008.optimizer import CANDIDATE_SOBOL

    assert CANDIDATE_SOBOL == 64
