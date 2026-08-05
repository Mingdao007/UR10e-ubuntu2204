"""Smoke tests for Stage B qLogNEI torch.compile helpers (fail-open, env OFF)."""

from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008 import qlognei_compile as mod  # noqa: E402


def _torch_cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def test_compile_env_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mod.COMPILE_ENV, raising=False)
    assert mod.compile_enabled_from_env() is False
    monkeypatch.setenv(mod.COMPILE_ENV, "0")
    assert mod.compile_enabled_from_env() is False
    monkeypatch.setenv(mod.COMPILE_ENV, "1")
    assert mod.compile_enabled_from_env() is True


def test_maybe_compile_disabled_returns_original() -> None:
    acquisition = object()
    assert mod.maybe_compile_acquisition(acquisition, enabled=False) is acquisition


def test_maybe_compile_fail_open_on_compile_error() -> None:
    acquisition = object()

    class _BoomTorch:
        @staticmethod
        def compile(_fn, mode="reduce-overhead"):  # noqa: ARG002
            raise RuntimeError("compile unavailable in smoke test")

    out = mod.maybe_compile_acquisition(
        acquisition,
        enabled=True,
        torch_module=_BoomTorch,
    )
    assert out is acquisition
    assert mod.is_compiled_acquisition(out) is False


def test_maybe_compile_marks_wrapper_when_compile_succeeds() -> None:
    acquisition = MagicMock(name="acquisition")

    class _OkTorch:
        @staticmethod
        def compile(fn, mode="reduce-overhead"):  # noqa: ARG002
            return fn

    out = mod.maybe_compile_acquisition(
        acquisition,
        enabled=True,
        torch_module=_OkTorch,
    )
    assert out is not acquisition
    assert mod.is_compiled_acquisition(out) is True
    X = object()
    out(X)
    acquisition.assert_called_once_with(X)


def test_warmup_fail_open_returns_false() -> None:
    def _bad(_x):
        raise RuntimeError("warmup blew up")

    class _NullCtx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Torch:
        @staticmethod
        def inference_mode():
            return _NullCtx()

    assert mod.warmup_acquisition(_bad, example_x=object(), torch_module=_Torch) is False


def test_prepare_scoring_falls_back_when_warmup_fails() -> None:
    acquisition = MagicMock(name="acquisition")

    class _NullCtx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _OkCompileBadWarm:
        @staticmethod
        def compile(fn, mode="reduce-overhead"):  # noqa: ARG002
            def _compiled(X, *args, **kwargs):
                raise RuntimeError("first forward fails")

            return _compiled

        @staticmethod
        def inference_mode():
            return _NullCtx()

    scorer, compiled = mod.prepare_scoring_acquisition(
        acquisition,
        enabled=True,
        example_x=object(),
        torch_module=_OkCompileBadWarm,
    )
    assert compiled is False
    assert scorer is acquisition


def test_batched_worker_compile_hook_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mod.COMPILE_ENV, raising=False)
    from step5d_autotune_v4_r008 import optimizer_worker_batched as batched

    acquisition = object()
    assert batched._maybe_compile_acquisition(acquisition) is acquisition


@pytest.mark.skipif(
    not _torch_cuda_available(),
    reason="CUDA torch not available; compile smoke stays mock-only",
)
def test_real_torch_compile_optional_smoke() -> None:
    """Optional live compile path when CUDA is present; still fail-open safe."""

    import torch

    def _identity(x):
        return x

    compiled = mod.maybe_compile_acquisition(_identity, enabled=True, torch_module=torch)
    assert compiled is _identity or mod.is_compiled_acquisition(compiled)
    x = torch.zeros(1, 1, 7, dtype=torch.double)
    if torch.cuda.is_available():
        x = x.cuda()
    if mod.is_compiled_acquisition(compiled):
        assert mod.warmup_acquisition(compiled, x, torch_module=torch) in {True, False}
