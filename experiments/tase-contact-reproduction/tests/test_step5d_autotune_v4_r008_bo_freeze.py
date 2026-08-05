"""Offline regression test for the r008 BO-entry hyperparameter-freeze fix
(2026-08-03 evening).

Root cause fixed here: ``R008HostLoop._refill()``'s SPACEFILL-complete branch
only called ``optimizer.fit_group_once(1)`` before flipping to the BO phase.
It never called ``fit_group_once(2)`` + ``.freeze()`` the way r006's proven
``_FIT_FREEZE`` phase does (``r006/live_adapter.py``'s
``fit_group_once(2)`` then ``freeze()``). Without that freeze,
``R006CudaQLogNEI.hyperparameters_frozen`` stays permanently False, so every
single live BO ``ask()`` sent ``hyperparameters_frozen=False`` and paid a full
``fit_gpytorch_mll`` GP hyperparameter refit -- confirmed live 2026-08-03 as
the reason each BO ask cost 8-19 minutes instead of the ~30-40s a frozen-model
ask costs (grok's own design comment in ``optimizer.py``).

Two now-dead periodic-refit call sites were also removed
(``R008ProductionOptimizer.tell`` and ``R008HostLoop._advance_after_record``'s
BO_TRIAL handling): once hyperparameters are frozen, ``fit_group_once`` always
raises (``r006 CUDA hyperparameters fit only during warm-start groups 1 and
2``), so both were permanent silently-swallowed no-ops after this fix --
worse, before this fix both fired *in addition to* every ask()'s own internal
refit, on every 4th BO trial.

No robot I/O; pure offline unit test using a fake queue/optimizer double.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r005.runtime import CampaignPhase  # noqa: E402
from step5d_autotune_v4_r008.live_adapter import R008HostLoop  # noqa: E402


class _FakeLedger:
    def __init__(self, path: Path) -> None:
        self.path = str(path)


class _FakeQueue:
    def __init__(self) -> None:
        self.inflight = None
        self._calls = 0

    def pending(self) -> tuple:
        self._calls += 1
        # _refill() calls pending() twice per loop iteration (once in the
        # while-condition, once reassigned inside the body). Empty for the
        # first iteration's two calls drives the SPACEFILL-complete
        # transition; two dummy entries afterward make the while-condition
        # false so the loop exits before reaching the freshly-entered BO
        # phase's ask() branch (out of scope here).
        return () if self._calls <= 2 else (object(), object())


class _FakeOptimizer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fit_group_once(self, group: int) -> None:
        self.calls.append(f"fit_group_once({group})")

    def freeze(self) -> None:
        self.calls.append("freeze()")


def _spacefill_complete_loop(tmp_path: Path) -> tuple[R008HostLoop, _FakeOptimizer]:
    loop = object.__new__(R008HostLoop)
    loop.epoch = 1
    loop.phase = R008HostLoop._SPACEFILL
    loop.queue = _FakeQueue()
    loop.optimizer = _FakeOptimizer()
    loop.ledger = _FakeLedger(tmp_path / "r006-observations.jsonl")
    loop.events = []
    loop.qualification_passes = 3
    loop.cursor = object()
    loop._spacefill = ()
    loop._spacefill_count = 0
    loop._spacefill_scheduled = 0
    loop._staircase = ()
    loop._staircase_count = 0
    loop._staircase_scheduled = 0
    loop._qual_scheduled = 3
    loop._anchor_scheduled = 3
    loop._bo_scheduled = 0
    loop._retest_scheduled = 0
    loop._anchor_verdict = "passed"
    return loop, loop.optimizer


def test_spacefill_complete_does_full_warm_start_then_freezes(tmp_path: Path) -> None:
    loop, optimizer = _spacefill_complete_loop(tmp_path)
    loop._refill()
    assert optimizer.calls == ["fit_group_once(1)", "fit_group_once(2)", "freeze()"]
    assert loop.phase == CampaignPhase.BO
    assert "R008_SPACEFILL_COMPLETE_ENTER_BO_FROZEN" in loop.events


def test_no_periodic_refit_call_sites_remain() -> None:
    import inspect

    from step5d_autotune_v4_r008.live_adapter import R008ProductionOptimizer

    tell_source = inspect.getsource(R008ProductionOptimizer.tell)
    assert "fit_group_once" not in tell_source

    refill_source = inspect.getsource(R008HostLoop._advance_after_record)
    # The only surviving fit_group_once/freeze references belong to the
    # BO-entry warm-start comment/call, not a periodic every-4-trials refit.
    assert "% 4" not in refill_source
    assert "R008_GP_REFIT" not in refill_source
