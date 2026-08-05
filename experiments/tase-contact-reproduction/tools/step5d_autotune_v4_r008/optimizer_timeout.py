"""r008 overlay: raise the frozen 180s CUDA optimizer child timeout.

r008 asks over a wide Sobol cover of the Stage-B box.  Even after shrinking
``CANDIDATE_SOBOL`` to 512, cold torch import + first JIT + GP fit can still
nudge a loaded host past the frozen r006 default of 180s.  This scope keeps
the shared ``OptimizerSubprocessClient`` body byte-identical and only lifts
the wall-clock budget for the live r008 host process.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from step5d_optimizer_runtime import (
    OptimizerProfileDeclaration,
    OptimizerSubprocessClient,
    ResolvedOptimizerRuntime,
)

# One-shot r006 worker still pays cold torch/CUDA + GP fit; at ~70 ledger rows
# asks land near ~300s.  Keep the ceiling above that flake band.
R008_OPTIMIZER_TIMEOUT_S = 600.0


@contextmanager
def r008_optimizer_timeout_scope(
    *, timeout_s: float = R008_OPTIMIZER_TIMEOUT_S
) -> Iterator[None]:
    """Process-local patch: new optimizer clients default to the r008 budget."""

    budget = float(timeout_s)
    original_init = OptimizerSubprocessClient.__init__

    def _patched(
        self: OptimizerSubprocessClient,
        *,
        declaration: OptimizerProfileDeclaration | None = None,
        worker_module: str,
        timeout_s: float = budget,
        runtime_resolver: Callable[[], ResolvedOptimizerRuntime] | None = None,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        original_init(
            self,
            declaration=declaration,
            worker_module=worker_module,
            timeout_s=timeout_s,
            runtime_resolver=runtime_resolver,
            source_environment=source_environment,
        )

    try:
        OptimizerSubprocessClient.__init__ = _patched  # type: ignore[method-assign]
        yield
    finally:
        OptimizerSubprocessClient.__init__ = original_init  # type: ignore[method-assign]


__all__ = [
    "R008_OPTIMIZER_TIMEOUT_S",
    "r008_optimizer_timeout_scope",
]
