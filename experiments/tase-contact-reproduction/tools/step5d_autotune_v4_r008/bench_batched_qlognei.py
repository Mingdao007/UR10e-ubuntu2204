"""Microbench: sequential qLogNEI loop vs r008 batched t-batch scorer.

Usage (managed optimizer interpreter; prefer ``-m`` so local ``queue.py``
does not shadow the stdlib):

  PYTHONPATH=tools <optimizer>/bin/python -m step5d_autotune_v4_r008.bench_batched_qlognei
  PYTHONPATH=tools <optimizer>/bin/python -m step5d_autotune_v4_r008.bench_batched_qlognei --sample-gpu
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import threading
import time
from typing import Any

import gpytorch
import torch
from botorch.acquisition.logei import qLogNoisyExpectedImprovement
from botorch.acquisition.objective import GenericMCObjective
from botorch.models import SingleTaskGP
from gpytorch.likelihoods import FixedNoiseGaussianLikelihood

from step5d_autotune_v4_r006.lattice import IMode, ParameterPoint
from step5d_autotune_v4_r006.optimizer_worker import _features
from step5d_autotune_v4_r008.optimizer_worker_batched import _score_combination_batches


def _point(index: int) -> ParameterPoint:
    return ParameterPoint(
        p_step=10 + index,
        d_step=5,
        tau_step=0,
        i_mode=IMode.OFF,
        i_step=None,
        ko_step=2,
        kp_step=2,
    )


def _build(n_train: int, n_choices: int, seed: int):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    device = torch.device("cuda:0")
    train_points = [_point(i) for i in range(n_train)]
    choices = tuple(_point(i) for i in range(200, 200 + n_choices))
    train_x = torch.tensor([_features(p) for p in train_points], dtype=torch.double, device=device)
    train_y = torch.linspace(0.5, 2.0, n_train, dtype=torch.double, device=device).unsqueeze(-1)
    train_yvar = torch.full_like(train_y, 1.0e-4)

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
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=train_x,
        X_pending=None,
        objective=GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1)),
        prune_baseline=False,
    )
    return acquisition, choices, device


def _score_sequential(acquisition, choices, device):
    scored = []
    with torch.no_grad():
        for batch in itertools.combinations(choices, 1):
            batch_x = torch.tensor(
                [[_features(point) for point in batch]],
                dtype=torch.double,
                device=device,
            )
            scored.append(
                (
                    float(acquisition(batch_x).detach().cpu().item()),
                    tuple(point.key for point in batch),
                    batch,
                )
            )
    selected = max(scored, key=lambda row: (row[0], tuple(str(v) for v in row[1])))[2]
    return selected, [row[0] for row in scored]


class _GpuSampler:
    def __init__(self, interval_s: float = 0.1) -> None:
        self.interval_s = interval_s
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _read_util() -> float | None:
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        lines = (completed.stdout or "").strip().splitlines()
        if not lines:
            return None
        try:
            return float(lines[0].strip())
        except ValueError:
            return None

    def start(self) -> None:
        self.samples.clear()
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                value = self._read_util()
                if value is not None:
                    self.samples.append(value)
                self._stop.wait(self.interval_s)

        self._thread = threading.Thread(target=_loop, name="nvidia-smi-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if not self.samples:
            return {"sampled": False, "max_util_pct": None, "mean_util_pct": None, "n_samples": 0}
        return {
            "sampled": True,
            "max_util_pct": max(self.samples),
            "mean_util_pct": sum(self.samples) / len(self.samples),
            "n_samples": len(self.samples),
        }


def _timed(fn, *, sample_gpu: bool) -> tuple[Any, float, dict[str, Any]]:
    sampler = _GpuSampler() if sample_gpu else None
    if sampler is not None:
        sampler.start()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    t0 = time.perf_counter()
    result = fn()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    wall_s = time.perf_counter() - t0
    gpu = sampler.stop() if sampler is not None else {"sampled": False}
    return result, wall_s, gpu


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-train", type=int, default=75)
    parser.add_argument("--n-choices", type=int, default=76)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--sample-gpu", action="store_true", help="sample nvidia-smi during scoring")
    args = parser.parse_args(argv)

    if not torch.cuda.is_available():
        print(json.dumps({"ok": False, "reason": "CUDA required"}, sort_keys=True))
        return 2

    acquisition, choices, device = _build(args.n_train, args.n_choices, args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    (seq_selected, seq_scores), seq_s, seq_gpu = _timed(
        lambda: _score_sequential(acquisition, choices, device),
        sample_gpu=args.sample_gpu,
    )

    acquisition_b, choices_b, device_b = _build(args.n_train, args.n_choices, args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    (batch_selected, batch_scores, scoring), batch_s, batch_gpu = _timed(
        lambda: _score_combination_batches(
            acquisition_b, choices_b, 1, device=device_b, torch=torch
        ),
        sample_gpu=args.sample_gpu,
    )

    payload = {
        "ok": True,
        "n_train": args.n_train,
        "n_choices": args.n_choices,
        "seq_s": round(seq_s, 6),
        "batched_s": round(batch_s, 6),
        "speedup": round(seq_s / batch_s, 3) if batch_s else None,
        "same_selected": list(seq_selected[0].key) == list(batch_selected[0].key),
        "max_abs_delta": max(abs(a - b) for a, b in zip(seq_scores, batch_scores, strict=True)),
        "scoring": scoring,
        "gpu_oracle": seq_gpu,
        "gpu_batched": batch_gpu,
        "compile_default": "off (R008_QLOGNEI_COMPILE=1 optional)",
    }
    print(json.dumps(payload, sort_keys=True))
    if not payload["same_selected"] or payload["max_abs_delta"] > 1e-10:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
