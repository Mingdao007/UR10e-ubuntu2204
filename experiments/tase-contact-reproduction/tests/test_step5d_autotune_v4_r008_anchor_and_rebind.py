"""Offline tests for the r008 Stage D2 gaps fixed 2026-08-03:

- level-0 r006-anchor-repeat validation (lattice.r006_anchor_point)
- domain rebind around a staircase edge (live_adapter._narrow_box_around_pd)
- anchor-verdict gate logic (R008HostLoop._compute_anchor_verdict)

No robot I/O; pure offline unit tests.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import D_ANCHOR, KO_ANCHOR, KP_ANCHOR, P_ANCHOR, TAU_ANCHOR  # noqa: E402
from step5d_autotune_v4_r008.lattice import (  # noqa: E402
    assert_r006_accepts,
    default_anchor,
    default_box,
    r006_anchor_point,
)
from step5d_autotune_v4_r008.live_adapter import (  # noqa: E402
    R008HostLoop,
    _clamp_log2_pd_center,
    _narrow_box_around_pd,
)


def test_r006_anchor_point_matches_frozen_r006_constants() -> None:
    point = r006_anchor_point()
    assert point.pd_ratio == pytest.approx(P_ANCHOR / D_ANCHOR)
    assert point.damping == pytest.approx(D_ANCHOR)
    assert point.force_p_gain == pytest.approx(P_ANCHOR)
    assert point.tau_s == pytest.approx(TAU_ANCHOR)
    assert point.orientation_ko == pytest.approx(KO_ANCHOR)
    assert point.motion_kp == pytest.approx(KP_ANCHOR)
    assert point.kf == 0.0


def test_r006_anchor_point_is_on_the_r006_lattice() -> None:
    # Must decode through the frozen r006 candidate grid with no TP rotation,
    # same go/no-go the r008 anchor and staircase/spacefill points already pass.
    assert_r006_accepts((r006_anchor_point(),))


def test_narrow_box_around_pd_clips_to_original_bounds() -> None:
    box = default_box()
    center = (box.log2_pd_min + box.log2_pd_max) / 2.0
    narrowed = _narrow_box_around_pd(box, center, half_width=0.5)
    assert narrowed.log2_pd_min == pytest.approx(center - 0.5)
    assert narrowed.log2_pd_max == pytest.approx(center + 0.5)
    # Other axes are untouched.
    assert narrowed.log2_d_min == box.log2_d_min
    assert narrowed.log2_d_max == box.log2_d_max


def test_narrow_box_around_pd_clamps_out_of_range_center_to_real_subbox() -> None:
    """170124-style: center above box max must narrow, not silently full-box."""
    box = default_box()
    hw = 1.5
    # Historical bad center from live_20260806_170124 (above Stage-B max).
    raw_center = -9.287712379549449
    clamped, was = _clamp_log2_pd_center(box, raw_center, half_width=hw)
    assert was is True
    assert clamped == pytest.approx(box.log2_pd_max - hw)
    narrowed = _narrow_box_around_pd(box, raw_center, half_width=hw)
    assert narrowed.log2_pd_max == pytest.approx(box.log2_pd_max)
    assert narrowed.log2_pd_min == pytest.approx(box.log2_pd_max - 2.0 * hw)
    # Must be a strict sub-box (not the silent full-box fallback).
    assert narrowed.log2_pd_min > box.log2_pd_min


def test_narrow_box_around_pd_falls_back_when_box_too_narrow_for_half_width() -> None:
    box = default_box()
    # half_width larger than half the Stage-B span → cannot place ±hw window.
    span = box.log2_pd_max - box.log2_pd_min
    narrowed = _narrow_box_around_pd(box, (box.log2_pd_min + box.log2_pd_max) / 2.0, half_width=span)
    assert narrowed.log2_pd_min == box.log2_pd_min
    assert narrowed.log2_pd_max == box.log2_pd_max


def _bare_host_loop(**anchor_cfg: float) -> R008HostLoop:
    """Construct an R008HostLoop instance without running its heavy __init__.

    Only the attributes exercised by the anchor-verdict gate are set; this is
    a targeted unit test of that gate's pure decision logic, not an
    integration test of the full live host loop (which needs a live queue,
    ledger, and contract).
    """

    loop = object.__new__(R008HostLoop)
    loop._anchor_target = int(anchor_cfg.get("repeats", 3))
    loop._anchor_expected_mae_n = float(anchor_cfg.get("expected_mae_n", 5.58))
    loop._anchor_tolerance_n = float(anchor_cfg.get("tolerance_n", 0.5))
    loop._anchor_max_attempts = int(anchor_cfg.get("max_attempts", loop._anchor_target * 3))
    loop._anchor_count = 0
    loop._anchor_values = []
    return loop


def test_anchor_verdict_none_until_enough_values() -> None:
    loop = _bare_host_loop()
    loop._anchor_count = 1
    loop._anchor_values = [5.6]
    assert loop._compute_anchor_verdict() is None


def test_anchor_verdict_passed_when_median_near_r006_baseline() -> None:
    loop = _bare_host_loop()
    loop._anchor_count = 3
    loop._anchor_values = [5.58, 5.60, 5.57]
    assert loop._compute_anchor_verdict() == "passed"


def test_anchor_verdict_failed_when_median_far_from_r006_baseline() -> None:
    # The actual 2026-08-03 live measurement at this exact point (r008 anchor's
    # P/D, D=28) was ~4.8 N three independent times -- outside the 5.58+-0.5N
    # band -- which is exactly the case this gate exists to catch.
    loop = _bare_host_loop()
    loop._anchor_count = 3
    loop._anchor_values = [4.81, 4.79, 4.81]
    assert loop._compute_anchor_verdict() == "failed"


def test_anchor_verdict_failed_after_exhausting_attempts_without_enough_values() -> None:
    # Every attempt came back non-trainable (no mae_n); must not loop forever.
    loop = _bare_host_loop(repeats=3, max_attempts=6)
    loop._anchor_count = 6
    loop._anchor_values = []
    assert loop._compute_anchor_verdict() == "failed"


def test_domain_rebind_path_none_without_a_ledger() -> None:
    assert R008HostLoop._domain_rebind_path(None) is None
    assert R008HostLoop._domain_rebind_path(object()) is None


def test_domain_rebind_path_sits_next_to_the_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "run_dir" / "r006-observations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("")
    fake_ledger = type("FakeLedger", (), {"path": ledger_path})()
    rebind_path = R008HostLoop._domain_rebind_path(fake_ledger)
    assert rebind_path == ledger_path.resolve().parent / "r008_domain_rebind.json"


def test_load_domain_rebind_missing_file_returns_none(tmp_path: Path) -> None:
    assert R008HostLoop._load_domain_rebind(tmp_path / "does_not_exist.json") is None
    assert R008HostLoop._load_domain_rebind(None) is None


def test_load_domain_rebind_rejects_malformed_content(tmp_path: Path) -> None:
    path = tmp_path / "r008_domain_rebind.json"
    path.write_text("not json")
    assert R008HostLoop._load_domain_rebind(path) is None
    path.write_text('{"no_center_here": 1}')
    assert R008HostLoop._load_domain_rebind(path) is None


def test_persist_then_load_domain_rebind_round_trips(tmp_path: Path) -> None:
    loop = object.__new__(R008HostLoop)
    loop._domain_rebind_path_value = tmp_path / "r008_domain_rebind.json"
    loop._persist_domain_rebind(-13.25, source="preemptive_veto")

    loaded = R008HostLoop._load_domain_rebind(loop._domain_rebind_path_value)
    assert loaded is not None
    assert loaded["log2_pd_center"] == pytest.approx(-13.25)
    assert loaded["source"] == "preemptive_veto"
    assert loaded["half_width"] == pytest.approx(1.5)


def test_persist_domain_rebind_is_a_noop_without_a_path() -> None:
    loop = object.__new__(R008HostLoop)
    loop._domain_rebind_path_value = None
    loop._persist_domain_rebind(-13.25, source="empirical_edge")  # must not raise


def test_a_persisted_rebind_narrows_the_box_the_same_way_init_would(tmp_path: Path) -> None:
    """Exercises the exact load-then-narrow sequence __init__ runs on resume.

    Not a full R008HostLoop construction (that needs a live contract/queue/
    optimizer stack unrelated to this unit); this proves the two pieces
    __init__ wires together -- _load_domain_rebind and _narrow_box_around_pd
    -- compose correctly, which is the part that was missing before this fix.
    """

    import json as _json

    from step5d_autotune_v4_r008.lattice import default_box

    ledger_path = tmp_path / "r006-observations.jsonl"
    ledger_path.write_text("")
    fake_ledger = type("FakeLedger", (), {"path": ledger_path})()

    rebind_path = R008HostLoop._domain_rebind_path(fake_ledger)
    assert rebind_path is not None
    rebind_path.write_text(_json.dumps({"log2_pd_center": -14.0, "half_width": 1.0}))

    wide_box = default_box()
    rebind = R008HostLoop._load_domain_rebind(rebind_path)
    assert rebind is not None
    narrowed = _narrow_box_around_pd(
        wide_box, float(rebind["log2_pd_center"]), half_width=float(rebind.get("half_width", 1.5))
    )

    assert narrowed.log2_pd_min == pytest.approx(max(wide_box.log2_pd_min, -15.0))
    assert narrowed.log2_pd_max == pytest.approx(min(wide_box.log2_pd_max, -13.0))
    # Other axes untouched by the rebind.
    assert narrowed.log2_d_min == wide_box.log2_d_min
    assert narrowed.log2_d_max == wide_box.log2_d_max


def _bare_resume_loop(tmp_path: Path, *, rebind_present: bool, rows_for_counts: dict[str, int]):
    """A minimally-mocked R008HostLoop, just enough to exercise
    _resume_from_ledger's STAIRCASE/SPACEFILL transition, without the full
    live contract/queue/optimizer stack a real construction needs."""

    import json as _json
    from unittest.mock import MagicMock

    from step5d_autotune_v4_r008 import live_adapter as mod

    loop = object.__new__(mod.R008HostLoop)
    loop._staircase = tuple(range(6))  # only len() is used by the code under test
    loop._spacefill = tuple(range(24))
    loop.qualification_passes = 3
    loop._anchor_count = 3
    loop._anchor_target = 3
    loop._anchor_expected_mae_n = 5.58
    loop._anchor_tolerance_n = 0.5
    loop._anchor_values = [5.58, 5.58, 5.58]  # median hits the anchor gate -> "passed"
    loop._staircase_count = 0
    loop._spacefill_count = 0
    loop.epoch = 0
    loop.retest_candidate = None
    loop.events = []

    ledger_path = tmp_path / "r006-observations.jsonl"
    ledger_path.write_text("")
    loop.ledger = MagicMock()
    loop.ledger.fresh_process_verify = MagicMock(return_value=None)
    loop.ledger.records = ()
    loop.ledger.path = ledger_path
    loop.queue = MagicMock()
    loop.queue.last_attempt_sequence = max(sum(rows_for_counts.values()), 1)
    loop.optimizer = MagicMock()

    loop._domain_rebind_path_value = mod.R008HostLoop._domain_rebind_path(loop.ledger)
    if rebind_present:
        loop._domain_rebind_path_value.write_text(
            _json.dumps({"log2_pd_center": -14.0, "half_width": 1.0})
        )

    def fake_rows_for(kind: str):
        return tuple(range(rows_for_counts.get(kind, 0)))

    loop._rows_for = fake_rows_for
    loop._anchor_values_from_rows = lambda rows: list(loop._anchor_values)
    return loop


def test_resume_without_rebind_reenters_staircase_forever(tmp_path: Path) -> None:
    # Pre-fix behavior, kept as a regression pin: with only 2 of 6 staircase
    # rows recorded and no persisted rebind, resume must re-enter STAIRCASE.
    loop = _bare_resume_loop(
        tmp_path, rebind_present=False, rows_for_counts={"STAIRCASE": 2, "SPACEFILL": 42},
    )
    loop._resume_from_ledger()
    assert loop.phase == R008HostLoop._STAIRCASE
    # And _spacefill_count is never computed -- the bug this fix closes.
    assert loop._spacefill_count == 0


def test_resume_with_persisted_rebind_recognizes_spacefill_already_done(tmp_path: Path) -> None:
    loop = _bare_resume_loop(
        tmp_path, rebind_present=True, rows_for_counts={"STAIRCASE": 2, "SPACEFILL": 42},
    )
    loop._resume_from_ledger()
    # 42 recorded SPACEFILL rows >= the 24-point budget -> must not re-enter
    # STAIRCASE or SPACEFILL; must fall through toward BO/RETEST.
    assert loop.phase not in (R008HostLoop._STAIRCASE, R008HostLoop._SPACEFILL)
    assert loop._spacefill_count == 42
    # Resuming straight into BO must re-run the warm-start+freeze sequence:
    # a fresh process has a fresh (unfrozen) optimizer client, and this is
    # the only place that would ever re-freeze it on a mid-BO resume.
    assert [call.args for call in loop.optimizer.fit_group_once.call_args_list] == [(1,), (2,)]
    loop.optimizer.freeze.assert_called_once()


def test_resume_with_persisted_rebind_and_partial_spacefill_resumes_it(tmp_path: Path) -> None:
    loop = _bare_resume_loop(
        tmp_path, rebind_present=True, rows_for_counts={"STAIRCASE": 2, "SPACEFILL": 10},
    )
    loop._resume_from_ledger()
    assert loop.phase == R008HostLoop._SPACEFILL
    assert loop._spacefill_count == 10
