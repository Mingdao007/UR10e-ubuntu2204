"""Stage B optional ``torch.compile`` for r008 batched qLogNEI scoring.

Wire this behind ``R008_QLOGNEI_COMPILE=1`` only.  Default remains OFF so Stage
A fat-batch equivalence can land cleanly.

**Do not enable by default in production until numerical equivalence vs the
frozen r006 oracle (and the uncompiled Stage A batched path) is proven**: same
``selected_point_key(s)``, score Δ within the Stage A gate (≤1e-10).  Compile
must fail-open to the Stage A acquisition callable on any error; equivalence
regressions must fail-closed (leave compile disabled).
"""

from __future__ import annotations

import os
from typing import Any

COMPILE_ENV = "R008_QLOGNEI_COMPILE"
DEFAULT_COMPILE_MODE = "reduce-overhead"
_COMPILED_ATTR = "__r008_compiled__"


def compile_enabled_from_env(environ: Any | None = None) -> bool:
    """True only when ``R008_QLOGNEI_COMPILE`` is an explicit truthy flag."""

    env = os.environ if environ is None else environ
    return str(env.get(COMPILE_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def is_compiled_acquisition(acquisition: Any) -> bool:
    return bool(getattr(acquisition, _COMPILED_ATTR, False))


def maybe_compile_acquisition(
    acquisition: Any,
    *,
    enabled: bool,
    mode: str = DEFAULT_COMPILE_MODE,
    torch_module: Any | None = None,
) -> Any:
    """Optionally ``torch.compile`` an acquisition callable.

    Fail-open: on import/compile failure, return ``acquisition`` unchanged so
    the Stage A batched path remains usable.
    """

    if not enabled:
        return acquisition
    try:
        torch = torch_module
        if torch is None:
            import torch as torch  # type: ignore[no-redef]
        compiled = torch.compile(acquisition, mode=mode)

        def _compiled_call(X: Any, *args: Any, **kwargs: Any) -> Any:
            return compiled(X, *args, **kwargs)

        setattr(_compiled_call, _COMPILED_ATTR, True)
        return _compiled_call
    except Exception:
        return acquisition


def warmup_acquisition(
    acquisition: Any,
    example_x: Any,
    *,
    torch_module: Any | None = None,
) -> bool:
    """Run one forward to pay compile/capture warmup.

    Returns True on success.  Fail-open: any exception returns False without
    raising so callers can keep the Stage A path.
    """

    try:
        torch = torch_module
        if torch is None:
            import torch as torch  # type: ignore[no-redef]
        with torch.inference_mode():
            _ = acquisition(example_x)
        return True
    except Exception:
        return False


def prepare_scoring_acquisition(
    acquisition: Any,
    *,
    enabled: bool | None = None,
    example_x: Any | None = None,
    mode: str = DEFAULT_COMPILE_MODE,
    torch_module: Any | None = None,
) -> tuple[Any, bool]:
    """Compile (optional) + warmup; fail-open to the original acquisition.

    Returns ``(acquisition_or_compiled, compiled_and_warm)``.
    When warmup fails after a successful compile attempt, returns the original
    acquisition so scoring stays on the Stage A path.
    """

    use = compile_enabled_from_env() if enabled is None else bool(enabled)
    if not use:
        return acquisition, False
    compiled = maybe_compile_acquisition(
        acquisition,
        enabled=True,
        mode=mode,
        torch_module=torch_module,
    )
    if not is_compiled_acquisition(compiled):
        return acquisition, False
    if example_x is None:
        return compiled, True
    if warmup_acquisition(compiled, example_x, torch_module=torch_module):
        return compiled, True
    return acquisition, False


__all__ = [
    "COMPILE_ENV",
    "DEFAULT_COMPILE_MODE",
    "compile_enabled_from_env",
    "is_compiled_acquisition",
    "maybe_compile_acquisition",
    "prepare_scoring_acquisition",
    "warmup_acquisition",
]
