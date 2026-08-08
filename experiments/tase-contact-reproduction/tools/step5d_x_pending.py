"""Lineage-neutral BoTorch pending-point binding.

This module is intentionally framework-light: the optimizer child supplies
the already imported torch module and acquisition function.  It keeps the
V3 qLogNEI implementation's default empty-pending behavior unchanged while
making the API-correct ``n x d`` shape explicit for V4 and future V5.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
import math
from typing import Any, Sequence


class PendingBindingError(ValueError):
    """X_pending cannot be represented as a unique n x d tensor."""


def bind_x_pending(
    acquisition: Any,
    rows: Sequence[Sequence[float]],
    *,
    torch_module: Any,
    device: Any,
) -> tuple[int, int] | None:
    if not rows:
        return None
    try:
        tensor = torch_module.tensor(rows, device=device)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise PendingBindingError("X_pending rows cannot form a tensor") from exc
    if getattr(tensor, "ndim", None) != 2 or tensor.shape[1] <= 0:
        raise PendingBindingError("X_pending must have API shape n x d")
    acquisition.set_X_pending(tensor)
    return (int(tensor.shape[0]), int(tensor.shape[1]))


@contextmanager
def install_qlognei_x_pending(rows: Sequence[Sequence[float]]):
    """Inject V4/V5 pending rows while invoking the immutable V3 qLogNEI body.

    The child owns BoTorch imports.  This seam patches only the qLogNEI
    constructor for the duration of one child request, then restores it.  V3's
    source bytes and public function signature remain untouched while the
    acquisition receives the API-required ``n x d`` pending tensor.
    """

    normalized = tuple(tuple(float(value) for value in row) for row in rows)
    if normalized:
        width = len(normalized[0])
        if width <= 0 or any(len(row) != width for row in normalized):
            raise PendingBindingError("X_pending rows must have one common positive width")
        if any(not math.isfinite(value) for row in normalized for value in row):
            raise PendingBindingError("X_pending rows must be finite")
    receipt: dict[str, tuple[int, int] | None] = {"shape": None}
    if not normalized:
        yield receipt
        return

    try:
        logei = importlib.import_module("botorch.acquisition.logei")
        original = logei.qLogNoisyExpectedImprovement
    except (ImportError, AttributeError) as exc:
        raise PendingBindingError("BoTorch qLogNEI constructor is unavailable") from exc

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        acquisition = original(*args, **kwargs)
        try:
            train_x = acquisition.model.train_inputs[0]
            tensor = train_x.new_tensor(normalized)
            if tensor.ndim != 2 or tensor.shape[0] != len(normalized) or tensor.shape[1] != len(normalized[0]):
                raise PendingBindingError("X_pending constructor tensor is not n x d")
            acquisition.set_X_pending(tensor)
            receipt["shape"] = (int(tensor.shape[0]), int(tensor.shape[1]))
        except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as exc:
            if isinstance(exc, PendingBindingError):
                raise
            raise PendingBindingError("qLogNEI X_pending binding failed") from exc
        return acquisition

    logei.qLogNoisyExpectedImprovement = wrapped
    try:
        yield receipt
    finally:
        logei.qLogNoisyExpectedImprovement = original


__all__ = ["PendingBindingError", "bind_x_pending", "install_qlognei_x_pending"]
