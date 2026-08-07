"""SHADOW-ONLY early-abort wiring: no motion abort, record once."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.early_abort_penalty import (  # noqa: E402
    load_early_abort_penalties,
)
from step5d_autotune_v4_r008.early_abort_shadow import (  # noqa: E402
    GUARD_FRAC,
    resolve_early_abort_mode,
    should_trigger_early_abort,
)
from step5d_autotune_v4_r008.live_adapter import R008LiveWriterAdapter  # noqa: E402
from step5d_force_objective import FORMAL_END_S, FORMAL_START_S  # noqa: E402


def _make_adapter() -> R008LiveWriterAdapter:
    writer = MagicMock()
    contract = MagicMock()
    return R008LiveWriterAdapter(writer, contract=contract)


def _arm(
    adapter: R008LiveWriterAdapter,
    tmp_path: Path,
    *,
    best: float = 2.0,
    mode_env: dict[str, str] | None = None,
    events: list[str] | None = None,
) -> list[str]:
    log: list[str] = [] if events is None else events
    adapter.arm_early_abort_shadow(
        best_so_far_mae_n=best,
        dispatch_sequence=42,
        attempt_sequence=7,
        kind="BO_TRIAL",
        point_key=[1, 0, 0, "OFF", 0, 0, 0],
        candidate={"force_p_gain": 1.0},
        run_dir=tmp_path,
        execution_id="exec-test",
        events=log,
        environ=mode_env if mode_env is not None else {},
    )
    return log


def test_resolve_mode_default_shadow() -> None:
    assert resolve_early_abort_mode({}) == "shadow"
    assert resolve_early_abort_mode({"R008_EARLY_ABORT_MODE": "shadow"}) == "shadow"
    assert resolve_early_abort_mode({"R008_EARLY_ABORT_MODE": "off"}) == "off"
    assert resolve_early_abort_mode({"R008_EARLY_ABORT_MODE": "active"}) == "active"


def test_should_trigger_respects_guard() -> None:
    assert (
        should_trigger_early_abort(
            progress=0.05,
            partial_mae_n=100.0,
            best_so_far_mae_n=1.0,
            kappa=3.0,
            guard_frac=GUARD_FRAC,
        )
        is False
    )
    assert (
        should_trigger_early_abort(
            progress=0.20,
            partial_mae_n=100.0,
            best_so_far_mae_n=1.0,
            kappa=3.0,
            guard_frac=GUARD_FRAC,
        )
        is True
    )


def test_shadow_trigger_records_once(tmp_path: Path) -> None:
    adapter = _make_adapter()
    events = _arm(adapter, tmp_path)
    # Fake local partial MAE high enough to trip after guard.
    adapter._ea_partial_abs_error_sum = 20.0
    adapter._ea_partial_count = 1

    # Just past guard: progress ≈ 0.10 → path_time = 5 + 0.10*55 = 10.5
    path_at_guard = FORMAL_START_S + GUARD_FRAC * (FORMAL_END_S - FORMAL_START_S)
    path_trigger = path_at_guard + 1.0  # past guard

    adapter._evaluate_early_abort_shadow(path_trigger)
    adapter._evaluate_early_abort_shadow(path_trigger + 5.0)
    adapter._evaluate_early_abort_shadow(path_trigger + 10.0)

    rows = load_early_abort_penalties(tmp_path)
    assert len(rows) == 1
    assert rows[0]["dispatch_sequence"] == 42
    assert rows[0]["enters_gp_training"] is False
    shadow_logs = [e for e in events if e.startswith("R008_EARLY_ABORT_SHADOW:")]
    assert len(shadow_logs) == 1
    assert adapter._ea_fired is True


def test_below_guard_no_record(tmp_path: Path) -> None:
    adapter = _make_adapter()
    events = _arm(adapter, tmp_path)
    adapter._ea_partial_abs_error_sum = 20.0
    adapter._ea_partial_count = 1

    # progress 0.05 < guard 0.10
    path_below = FORMAL_START_S + 0.05 * (FORMAL_END_S - FORMAL_START_S)
    adapter._evaluate_early_abort_shadow(path_below)
    adapter._evaluate_early_abort_shadow(path_below)

    assert load_early_abort_penalties(tmp_path) == []
    assert not any(e.startswith("R008_EARLY_ABORT_SHADOW:") for e in events)
    assert adapter._ea_fired is False


def test_active_mode_still_shadow_no_raise(tmp_path: Path) -> None:
    """R008_EARLY_ABORT_MODE=active must NOT abort motion / raise."""

    adapter = _make_adapter()
    events = _arm(
        adapter,
        tmp_path,
        mode_env={"R008_EARLY_ABORT_MODE": "active"},
    )
    assert any(
        e.startswith("R008_EARLY_ABORT_ACTIVE_BLOCKED:await_andy_channel")
        for e in events
    )
    assert adapter._ea_armed is True
    adapter._ea_partial_abs_error_sum = 20.0
    adapter._ea_partial_count = 1
    path_trigger = FORMAL_START_S + 0.5 * (FORMAL_END_S - FORMAL_START_S)

    # Must not raise; still records shadow row only.
    adapter._evaluate_early_abort_shadow(path_trigger)
    rows = load_early_abort_penalties(tmp_path)
    assert len(rows) == 1
    assert rows[0]["enters_gp_training"] is False
    # Confirm no motion-abort side effects were attempted on the writer.
    assert not adapter.writer._send_safe_stop.called
    assert not adapter.writer.stop.called


def test_mode_off_is_noop(tmp_path: Path) -> None:
    adapter = _make_adapter()
    events = _arm(
        adapter,
        tmp_path,
        mode_env={"R008_EARLY_ABORT_MODE": "off"},
    )
    assert adapter._ea_armed is False
    adapter._ea_partial_abs_error_sum = 20.0
    adapter._ea_partial_count = 1
    path_trigger = FORMAL_START_S + 0.5 * (FORMAL_END_S - FORMAL_START_S)
    adapter._evaluate_early_abort_shadow(path_trigger)
    assert load_early_abort_penalties(tmp_path) == []
    assert events == []


def test_observe_override_calls_super_then_shadow(tmp_path: Path, monkeypatch: Any) -> None:
    """Mock adapter path: observe calls super then shadow eval; never raises."""

    adapter = _make_adapter()
    _arm(adapter, tmp_path)

    called: dict[str, int] = {"super": 0}

    def fake_super_observe(self: Any, sample: Any) -> None:  # noqa: ARG001
        called["super"] += 1

    monkeypatch.setattr(
        R008LiveWriterAdapter.__mro__[1],
        "observe_r004_path_sample",
        fake_super_observe,
    )

    path_trigger = FORMAL_START_S + 0.4 * (FORMAL_END_S - FORMAL_START_S)
    # Far from target=5N so local partial MAE trips after guard.
    sample = SimpleNamespace(path_time_s=path_trigger, filtered_normal_n=25.0)
    adapter.observe_r004_path_sample(sample)  # type: ignore[arg-type]
    adapter.observe_r004_path_sample(sample)  # type: ignore[arg-type]

    assert called["super"] == 2
    assert len(load_early_abort_penalties(tmp_path)) == 1
