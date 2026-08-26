from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_v5_entry_comparison import (  # noqa: E402
    EXPECTED_SOURCE_SHA,
    PAIR_COUNT,
    build_comparison_schedule,
    evaluate_comparison,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import BoundaryMode  # noqa: E402


def _record(plan: object, *, mae: float, index: int, terminal_home: bool, events: tuple[dict, ...]) -> SimpleNamespace:
    rows = tuple(
        {
            "monotonic_s": float(item),
            "rtde_timestamp_s": float(item),
            "normal_load_n": 5.0,
            "force_norm_n": 5.1,
            "torque_norm_nm": 0.1,
        }
        for item in range(3)
    )
    artifact = SimpleNamespace(
        rows=rows,
        event_bundle={"events": events},
        receipt={"max_gap_s": 0.002, "max_rtde_gap_s": 0.002},
    )
    trial_slice = SimpleNamespace(
        sample_start_index=index,
        sample_end_index=index + 1,
    )
    return SimpleNamespace(
        eligible=True,
        metric_snapshot=SimpleNamespace(formal_mae_n=float(mae)),
        record_sha256=f"{int(mae * 1000):064x}"[-64:],
        source_artifact=artifact,
        trial_slice=trial_slice,
        boundary=SimpleNamespace(
            mode=BoundaryMode.HOME if terminal_home else BoundaryMode.CONTACT_ROLLOVER
        ),
    )


def _fake_results(*, difference: float) -> dict[str, SimpleNamespace]:
    pairs, chains = build_comparison_schedule(
        session_epoch=91,
        comparison_fingerprint="a" * 64,
    )
    results: dict[str, SimpleNamespace] = {}
    for chain_id, plans in chains.items():
        events = [
            {"kind": "CHAIN_START", "sample_index": 0},
        ]
        if len(plans) == 2:
            events.extend(
                [
                    {"kind": "CONTACT_ROLLOVER", "sample_index": 1},
                    {"kind": "HOME", "sample_index": 2},
                ]
            )
        else:
            events.append({"kind": "HOME", "sample_index": 1})
        records = []
        for index, plan in enumerate(plans):
            # The selected Home-entry row is the first record of a one-plan
            # chain; the selected rollover-entry row is the second record of
            # a two-plan chain.  Warm rows are deliberately retained too.
            mae = 1.0
            if len(plans) == 2 and index == 1:
                mae += difference
            records.append(
                _record(
                    plan,
                    mae=mae,
                    index=index,
                    terminal_home=index == len(plans) - 1,
                    events=tuple(events),
                )
            )
        results[chain_id] = SimpleNamespace(chain_id=chain_id, records=tuple(records))
    assert len(pairs) == PAIR_COUNT
    return results


def test_schedule_has_balanced_twenty_pairs_and_warm_rows() -> None:
    pairs, chains = build_comparison_schedule(
        session_epoch=77,
        comparison_fingerprint="a" * 64,
    )
    assert len(pairs) == 10
    assert sum(pair.order == "HOME_THEN_ROLLOVER" for pair in pairs) == 10
    assert sum(pair.order == "ROLLOVER_THEN_HOME" for pair in pairs) == 0
    assert len(chains) == 10
    assert sum(len(value) for value in chains.values()) == 20
    assert all(not pair.warm_chain_ids for pair in pairs)
    assert all(
        plan.candidate.correction_weights == (0.0,) * 6
        for plans in chains.values()
        for plan in plans
    )


def test_comparison_evaluator_recommends_rollover_only_inside_margin() -> None:
    pairs, _chains = build_comparison_schedule(
        session_epoch=91,
        comparison_fingerprint="a" * 64,
    )
    report = evaluate_comparison(
        pairs=pairs,
        results=_fake_results(difference=0.01),
        comparison_fingerprint="a" * 64,
        release_identity_sha256="b" * 64,
        runner_sha256="c" * 64,
    )
    assert report["status"] == "EQUIVALENT_RECOMMEND_ROLLOVER"
    assert report["recommendation"] == "ROLLOVER"
    assert report["pair_count"] == 10
    assert report["counted_trial_count"] == 20
    assert report["warm_trial_count"] == 0
    assert report["tell_exact_calls"] == 0
    assert report["primary_started"] is False
    assert report["correction_started"] is False


def test_comparison_evaluator_blocks_when_ci_is_outside_margin() -> None:
    pairs, _chains = build_comparison_schedule(
        session_epoch=91,
        comparison_fingerprint="a" * 64,
    )
    report = evaluate_comparison(
        pairs=pairs,
        results=_fake_results(difference=0.20),
        comparison_fingerprint="a" * 64,
        release_identity_sha256="b" * 64,
        runner_sha256="c" * 64,
    )
    assert report["status"] == "NOT_EQUIVALENT_MEASUREMENT_RESOLUTION_UNRESOLVED"
    assert report["recommendation"] is None
    assert report["paired_difference"]["equivalence_passed"] is False


def test_comparison_runner_freezes_current_source_identity() -> None:
    assert EXPECTED_SOURCE_SHA == "3a4dc0ae98674d211405c68682a30182b243325b59414162260611c9a54b4e05"
