"""Hard-stop penalty sidecar: MAE floor + BO training merge."""

from __future__ import annotations

from pathlib import Path
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.hard_stop_penalty import (  # noqa: E402
    PENALTY_FLOOR_MAE_N,
    PENALTY_JSONL_NAME,
    PENALTY_SCHEMA,
    RECOVERABLE_HARD_STOP_REASON_CODES,
    append_penalty,
    build_penalty_row,
    exception_is_recoverable_hard_stop,
    exception_looks_like_hard_stop,
    extract_reason_code,
    has_penalty_for_dispatch,
    historical_max_mae_n,
    load_penalties,
    merge_penalties_into_grouped,
    penalty_mae_from_history,
    record_hard_stop_penalty_for_run,
)


def _write_obs(run_dir: Path, rows: list[dict]) -> None:
    path = run_dir / "r006-observations.jsonl"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_penalty_mae_empty_history_uses_floor() -> None:
    assert penalty_mae_from_history(None) == pytest.approx(PENALTY_FLOOR_MAE_N)


def test_penalty_mae_below_floor_raises_to_floor() -> None:
    assert penalty_mae_from_history(8.2) == pytest.approx(PENALTY_FLOOR_MAE_N)


def test_penalty_mae_above_floor_keeps_history() -> None:
    assert penalty_mae_from_history(12.0) == pytest.approx(12.0)


def test_historical_max_from_eligible_observations(tmp_path: Path) -> None:
    _write_obs(
        tmp_path,
        [
            {"attempt_sequence": 1, "eligible": True, "mae_n": 1.5},
            {"attempt_sequence": 2, "eligible": False, "mae_n": 99.0},
            {"attempt_sequence": 3, "eligible": True, "mae_n": 8.2},
        ],
    )
    assert historical_max_mae_n(tmp_path) == pytest.approx(8.2)


def test_append_idempotent_on_dispatch(tmp_path: Path) -> None:
    row = build_penalty_row(
        dispatch_sequence=42,
        attempt_sequence=42,
        kind="BO_TRIAL",
        point_key=[10, 8, -4, "ON", 0, 0, 0],
        historical_max_mae_n=8.2,
        reason_code=61,
        reason="hard_abs_normal_60n",
    )
    assert row["schema"] == PENALTY_SCHEMA
    assert row["penalty_mae_n"] == pytest.approx(PENALTY_FLOOR_MAE_N)
    assert row["enters_raw_ledger"] is False
    first = append_penalty(tmp_path, row)
    second = append_penalty(tmp_path, row)
    assert first is not None
    assert second is None
    assert has_penalty_for_dispatch(tmp_path, 42)
    loaded = load_penalties(tmp_path)
    assert len(loaded) == 1
    assert (tmp_path / PENALTY_JSONL_NAME).is_file()


def test_exception_looks_like_hard_stop() -> None:
    assert exception_looks_like_hard_stop(
        "r006 stop-dominant packet: reason_code=61 reason=hard_abs_normal_60n"
    )
    assert not exception_looks_like_hard_stop("sidecar bytes differ")


def test_merge_penalties_into_grouped() -> None:
    grouped: dict[tuple, list[float]] = {(1, 2, 3, "OFF", None, 0, 0): [0.5]}
    order = [(1, 2, 3, "OFF", None, 0, 0)]
    penalties = [
        {
            "schema": PENALTY_SCHEMA,
            "point_key": [10, 8, -4, "ON", 0, 0, 0],
            "penalty_mae_n": 10.0,
        },
        {
            "schema": PENALTY_SCHEMA,
            "point_key": [1, 2, 3, "OFF", None, 0, 0],
            "penalty_mae_n": 12.0,
        },
    ]
    added = merge_penalties_into_grouped(grouped, order, penalties)
    assert added == 2
    assert (10, 8, -4, "ON", 0, 0, 0) in grouped
    assert grouped[(10, 8, -4, "ON", 0, 0, 0)] == [10.0]
    assert 12.0 in grouped[(1, 2, 3, "OFF", None, 0, 0)]


def test_record_hard_stop_penalty_for_run(tmp_path: Path) -> None:
    _write_obs(
        tmp_path,
        [{"attempt_sequence": 41, "eligible": True, "mae_n": 12.5}],
    )
    (tmp_path / "r008-state20-stop-dominant.json").write_text(
        json.dumps({"reason_code": 61, "reason": "hard_abs_normal_60n"}),
        encoding="utf-8",
    )
    row = record_hard_stop_penalty_for_run(
        tmp_path,
        dispatch_sequence=42,
        attempt_sequence=42,
        kind="BO_TRIAL",
        point_key=[10, 8, -4, "ON", 0, 0, 0],
        candidate={"force_p_gain": 0.00336, "force_damping": 158.4},
    )
    assert row is not None
    assert row["penalty_mae_n"] == pytest.approx(12.5)
    assert row["reason_code"] == 61
    again = record_hard_stop_penalty_for_run(
        tmp_path,
        dispatch_sequence=42,
        attempt_sequence=42,
        kind="BO_TRIAL",
        point_key=[10, 8, -4, "ON", 0, 0, 0],
    )
    assert again is None


# --- reason_code extraction / recoverable-hard-stop classification -------
# 2026-08-06: far005 disp42 and limit50-pathring attempt10 both revoked
# campaign authority (CampaignPhase.INCOMPLETE_STOPPED) for a
# candidate-specific 60N force trip that the penalty sidecar above already
# exists to punish. These cover the classifier that lets run_one() seal the
# penalty and continue instead of killing the whole campaign.


def test_recoverable_hard_stop_reason_codes_are_force_and_torque_only() -> None:
    assert RECOVERABLE_HARD_STOP_REASON_CODES == frozenset({61, 62, 63})


def test_extract_reason_code_reads_far005_disp42_message() -> None:
    msg = (
        "r006 stop-dominant packet: reason_code=61 reason=hard_abs_normal_60n; "
        'stop_diag={"tp_state": 25, "force_norm_n": 61.12}'
    )
    assert extract_reason_code(msg) == 61
    assert exception_is_recoverable_hard_stop(msg) is True


def test_extract_reason_code_reads_pathring_attempt10_message() -> None:
    msg = (
        "r006 stop-dominant packet: reason_code=61 reason=hard_abs_normal_60n; "
        'stop_diag={"tp_state": 25, "force_norm_n": 61.24}'
    )
    assert extract_reason_code(msg) == 61
    assert exception_is_recoverable_hard_stop(msg) is True


@pytest.mark.parametrize("code", [62, 63])
def test_force_norm_and_torque_trips_are_also_recoverable(code: int) -> None:
    msg = f"r006 stop-dominant packet: reason_code={code} reason=whatever;"
    assert exception_is_recoverable_hard_stop(msg) is True


@pytest.mark.parametrize("code", [3, 4, 41, 42, 50])
def test_session_and_protocol_stops_stay_fatal(code: int) -> None:
    """sensor_stale / external_stop / structural / hold / qualification-missing.

    These are not "this candidate was bad" outcomes; the loop must not
    auto-continue past them.
    """

    msg = f"r006 stop-dominant packet: reason_code={code} reason=whatever;"
    assert exception_is_recoverable_hard_stop(msg) is False


def test_extract_reason_code_none_when_absent() -> None:
    assert extract_reason_code("some unrelated RuntimeError") is None
    assert exception_is_recoverable_hard_stop("some unrelated RuntimeError") is False


def test_five_newton_acquisition_timeout_is_recoverable_candidate_fault() -> None:
    msg = (
        "unexpected_code_fault:five_newton_acquisition_timeout; "
        'baseline_diag={"filtered_max_n": 10.6}'
    )
    assert extract_reason_code(msg) is None
    assert exception_is_recoverable_hard_stop(msg) is True


def test_extract_reason_code_accepts_exception_instances_not_only_str() -> None:
    exc = RuntimeError(
        "r006 stop-dominant packet: reason_code=61 reason=hard_abs_normal_60n;"
    )
    assert extract_reason_code(exc) == 61
    assert exception_is_recoverable_hard_stop(exc) is True
