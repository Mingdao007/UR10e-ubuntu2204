"""R010 conditional Matérn-5/2 kernel with a correct masked diagonal."""

from __future__ import annotations

from typing import Any, Sequence


KERNEL_FAMILY = "conditional_matern52_shared_same_mode_i_on_only"
KERNEL_POLICY = {
    "family": KERNEL_FAMILY,
    "nu": 2.5,
    "ard_num_dims": 7,
    "components": ["shared", "same_mode", "i_on_only"],
    "mode_feature_index": 6,
    "diag_mask_policy": "paired mode_equal and paired i_on mask",
}


def conditional_matern52_kernel_class(gpytorch: Any):
    """Return the kernel class without importing the CUDA runtime at import time."""

    class ConditionalMatern52Kernel(gpytorch.kernels.Kernel):
        has_lengthscale = True

        def __init__(
            self,
            *,
            shared_lengthscales: Sequence[float] | None = None,
            same_mode_lengthscales: Sequence[float] | None = None,
            i_on_only_lengthscales: Sequence[float] | None = None,
        ) -> None:
            super().__init__(ard_num_dims=7)
            self.shared = gpytorch.kernels.MaternKernel(
                nu=2.5,
                ard_num_dims=7,
                lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
            )
            self.same_mode = gpytorch.kernels.MaternKernel(
                nu=2.5,
                ard_num_dims=7,
                lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
            )
            self.i_on_only = gpytorch.kernels.MaternKernel(
                nu=2.5,
                ard_num_dims=7,
                lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
            )
            for component, values in (
                (self.shared, shared_lengthscales),
                (self.same_mode, same_mode_lengthscales),
                (self.i_on_only, i_on_only_lengthscales),
            ):
                if values is not None:
                    component.initialize(lengthscale=list(values))

        def forward(self, x1, x2, diag=False, **params):
            shared = self.shared(x1, x2, diag=diag, **params)
            same = self.same_mode(x1, x2, diag=diag, **params)
            ion = self.i_on_only(x1, x2, diag=diag, **params)
            if diag:
                x1_mode = x1[..., -1]
                x2_mode = x2[..., -1]
                mode_equal = (x1_mode == x2_mode).to(dtype=x1.dtype)
                ion_mask = ((x1_mode > 0.5) & (x2_mode > 0.5)).to(dtype=x1.dtype)
                return shared + mode_equal * same + ion_mask * ion
            x1_mode = x1[..., -1].unsqueeze(-1)
            x2_mode = x2[..., -1].unsqueeze(-2)
            mode_equal = (x1_mode == x2_mode).to(dtype=x1.dtype)
            ion_mask = ((x1_mode > 0.5) & (x2_mode > 0.5)).to(dtype=x1.dtype)
            return shared + mode_equal * same + ion_mask * ion

    ConditionalMatern52Kernel.__name__ = "ConditionalMatern52Kernel"
    ConditionalMatern52Kernel.__qualname__ = "ConditionalMatern52Kernel"
    return ConditionalMatern52Kernel


def build_conditional_matern52_kernel(gpytorch: Any, **kwargs: Any):
    return conditional_matern52_kernel_class(gpytorch)(**kwargs)


__all__ = [
    "KERNEL_FAMILY",
    "KERNEL_POLICY",
    "build_conditional_matern52_kernel",
    "conditional_matern52_kernel_class",
]
