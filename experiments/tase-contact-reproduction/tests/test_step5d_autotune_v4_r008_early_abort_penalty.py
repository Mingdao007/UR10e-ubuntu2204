"""Early-abort penalty sidecar: no floor + GP training gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.early_abort_penalty import (  # noqa: E402
    PENALTY_JSONL_NAME,
    PENALTY_SCHEMA,
    append_early_abort_penalty,
    build_early_abort_penalty_row,
    has_early_abort_penalty_for_dispatch,
    load_early_abort_penalties,
    merge_penalties_into_grouped,
    record_early_abort_for_run,
)
from step5d_autotune_v4_r008.hard_stop_penalty import (  # noqa: E402
    merge_penalties_into_grouped as hard_stop_merge,
)


def test_schema_and_default_training_gate() -> None:
    row = build_early_abort_penalty_row(
        dispatch_sequence=7,
        attempt_sequence=7,
        kind="BO_TRIAL",
        point_key=[10, 8, -4, "ON", 0, 0, 0],
        partial_mae_n_at_trigger=4.5,
        best_so_far_mae_n=2.0,
        kappa_at_trigger=2.0,
        trigger_progress_fraction=0.4,
    )
    assert row["schema"] == PENALTY_SCHEMA
    assert row["enters_gp_training"] is False
    assert row["enters_raw_ledger"] is False
    assert "penalty_floor_mae_n" not in row
    assert row["partial_mae_n_at_trigger"] == pytest.approx(4.5)
    assert row["best_so_far_mae_n"] == pytest.approx(2.0)
    assert row["kappa_at_trigger"] == pytest.approx(2.0)
    assert row["trigger_progress_fraction"] == pytest.approx(0.4)


def test_penalty_uses_max_of_partial_and_kappa_times_best() -> None:
    # kappa * best dominates
    row_a = build_early_abort_penalty_row(
        dispatch_sequence=1,
        attempt_sequence=1,
        kind="BO_TRIAL",
        point_key=[1, 0, 0, "OFF", 0, 0, 0],
        partial_mae_n_at_trigger=3.0,
        best_so_far_mae_n=2.0,
        kappa_at_trigger=2.5,
        trigger_progress_fraction=0.2,
    )
    assert row_a["penalty_mae_n"] == pytest.approx(5.0)

    # partial dominates
    row_b = build_early_abort_penalty_row(
        dispatch_sequence=2,
        attempt_sequence=2,
        kind="BO_TRIAL",
        point_key=[2, 0, 0, "OFF", 0, 0, 0],
        partial_mae_n_at_trigger=9.0,
        best_so_far_mae_n=2.0,
        kappa_at_trigger=2.5,
        trigger_progress_fraction=0.8,
    )
    assert row_b["penalty_mae_n"] == pytest.approx(9.0)


def test_no_floor_constant_flattening() -> None:
    """Distinct partial/kappa pairs must keep distinct penalties (no shared floor)."""

    rows = [
        build_early_abort_penalty_row(
            dispatch_sequence=i,
            attempt_sequence=i,
            kind="BO_TRIAL",
            point_key=[i, 0, 0, "ON", 0, 0, 0],
            partial_mae_n_at_trigger=partial,
            best_so_far_mae_n=1.0,
            kappa_at_trigger=kappa,
            trigger_progress_fraction=0.5,
        )
        for i, (partial, kappa) in enumerate(
            [(1.1, 1.5), (2.2, 1.8), (3.3, 2.1), (4.4, 2.4)],
            start=1,
        )
    ]
    penalties = [row["penalty_mae_n"] for row in rows]
    assert len(set(penalties)) == len(penalties)
    assert all(p != 10.0 for p in penalties)
    assert not hasattr(
        __import__(
            "step5d_autotune_v4_r008.early_abort_penalty", fromlist=["x"]
        ),
        "PENALTY_FLOOR_MAE_N",
    )


def test_append_idempotent_on_dispatch(tmp_path: Path) -> None:
    row = build_early_abort_penalty_row(
        dispatch_sequence=42,
        attempt_sequence=42,
        kind="BO_TRIAL",
        point_key=[10, 8, -4, "ON", 0, 0, 0],
        partial_mae_n_at_trigger=4.0,
        best_so_far_mae_n=2.0,
        kappa_at_trigger=1.5,
        trigger_progress_fraction=0.55,
        enters_gp_training=False,
    )
    first = append_early_abort_penalty(tmp_path, row)
    second = append_early_abort_penalty(tmp_path, row)
    assert first is not None
    assert second is None
    assert has_early_abort_penalty_for_dispatch(tmp_path, 42)
    loaded = load_early_abort_penalties(tmp_path)
    assert len(loaded) == 1
    assert (tmp_path / PENALTY_JSONL_NAME).is_file()


def test_record_early_abort_for_run_idempotent(tmp_path: Path) -> None:
    row = record_early_abort_for_run(
        tmp_path,
        dispatch_sequence=11,
        attempt_sequence=11,
        kind="BO_TRIAL",
        point_key=[3, 1, -1, "ON", 0, 0, 0],
        candidate={"force_p_gain": 0.003},
        partial_mae_n_at_trigger=6.0,
        best_so_far_mae_n=2.5,
        kappa_at_trigger=2.0,
        trigger_progress_fraction=0.3,
        enters_gp_training=False,
    )
    assert row is not None
    assert row["penalty_mae_n"] == pytest.approx(6.0)
    again = record_early_abort_for_run(
        tmp_path,
        dispatch_sequence=11,
        attempt_sequence=11,
        kind="BO_TRIAL",
        point_key=[3, 1, -1, "ON", 0, 0, 0],
        partial_mae_n_at_trigger=6.0,
        best_so_far_mae_n=2.5,
        kappa_at_trigger=2.0,
        trigger_progress_fraction=0.3,
    )
    assert again is None
    assert len(load_early_abort_penalties(tmp_path)) == 1


def test_enters_gp_training_false_not_merged() -> None:
    grouped: dict[tuple, list[float]] = {(1, 2, 3, "OFF", 0, 0, 0): [0.5]}
    order = [(1, 2, 3, "OFF", 0, 0, 0)]
    rows = [
        {
            "schema": PENALTY_SCHEMA,
            "point_key": [10, 8, -4, "ON", 0, 0, 0],
            "penalty_mae_n": 7.5,
            "enters_gp_training": False,
        }
    ]
    filtered = [r for r in rows if r.get("enters_gp_training") is True]
    added = merge_penalties_into_grouped(grouped, order, filtered)
    assert added == 0
    assert (10, 8, -4, "ON", 0, 0, 0) not in grouped
    assert grouped[(1, 2, 3, "OFF", 0, 0, 0)] == [0.5]


def test_enters_gp_training_true_is_merged() -> None:
    grouped: dict[tuple, list[float]] = {(1, 2, 3, "OFF", 0, 0, 0): [0.5]}
    order = [(1, 2, 3, "OFF", 0, 0, 0)]
    rows = [
        {
            "schema": PENALTY_SCHEMA,
            "point_key": [10, 8, -4, "ON", 0, 0, 0],
            "penalty_mae_n": 7.5,
            "enters_gp_training": True,
        },
        {
            "schema": PENALTY_SCHEMA,
            "point_key": [1, 2, 3, "OFF", 0, 0, 0],
            "penalty_mae_n": 3.0,
            "enters_gp_training": False,
        },
    ]
    filtered = [r for r in rows if r.get("enters_gp_training") is True]
    added = merge_penalties_into_grouped(grouped, order, filtered)
    assert added == 1
    assert grouped[(10, 8, -4, "ON", 0, 0, 0)] == [7.5]
    assert 3.0 not in grouped[(1, 2, 3, "OFF", 0, 0, 0)]


def test_reuses_hard_stop_merge_implementation() -> None:
    assert merge_penalties_into_grouped is hard_stop_merge
