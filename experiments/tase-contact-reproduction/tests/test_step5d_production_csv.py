from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_production_csv import (  # noqa: E402
    BridgeCsvFollower,
    BridgeCsvTimeout,
    ProductionCsvWriter,
)


def test_repeated_terminal_state_fsyncs_once_per_transition(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "terminal-episode.csv"
    fields = ("t_monotonic_s", "ur_output_int_register_26")
    with csv_path.open("x", newline="", encoding="utf-8") as handle:
        writer = ProductionCsvWriter(handle, fields, flush_interval_rows=50)
        for index in range(101):
            assert writer.writerow(
                {
                    "t_monotonic_s": index * 0.002,
                    "ur_output_int_register_26": 78,
                }
            )
        assert writer.stats.terminal_flushes == 1
        assert writer.stats.durable_fsyncs == 2
        assert writer.stats.buffered_flushes == 2

        assert not writer.writerow(
            {"t_monotonic_s": 0.203, "ur_output_int_register_26": ""}
        )
        assert writer.writerow(
            {"t_monotonic_s": 0.2035, "ur_output_int_register_26": 78}
        )
        assert writer.stats.terminal_flushes == 1
        assert writer.stats.durable_fsyncs == 2

        assert writer.writerow(
            {"t_monotonic_s": 0.204, "ur_output_int_register_26": 75}
        )
        assert writer.stats.terminal_flushes == 2
        assert writer.stats.durable_fsyncs == 3

        assert not writer.writerow(
            {"t_monotonic_s": 0.206, "ur_output_int_register_26": 10}
        )
        assert writer.writerow(
            {"t_monotonic_s": 0.208, "ur_output_int_register_26": 78}
        )
        assert writer.stats.terminal_flushes == 3
        assert writer.stats.durable_fsyncs == 4


def test_partial_line_rolls_back_then_complete_row_is_consumed_once(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "growing.csv"
    with csv_path.open("x", newline="", encoding="utf-8") as handle:
        writer = ProductionCsvWriter(
            handle,
            ("t_monotonic_s", "ur_output_int_register_26", "value"),
        )
        follower = BridgeCsvFollower(csv_path)
        publisher = threading.Thread(
            target=writer.publish_row_with_partial_visibility,
            kwargs={
                "row": {
                    "t_monotonic_s": 1.0,
                    "ur_output_int_register_26": 76,
                    "value": "complete-only",
                },
                "split_at": 5,
                "partial_visible_s": 0.10,
            },
        )
        publisher.start()
        rows = follower.rows(timeout_s=1.0)
        assert next(rows)["value"] == "complete-only"
        publisher.join(timeout=1.0)
        assert not publisher.is_alive()
        assert follower.stats.rows_seen == 1
        assert follower.stats.partial_line_polls >= 1
        with pytest.raises(BridgeCsvTimeout) as timeout:
            next(follower.rows(timeout_s=0.02))
        assert timeout.value.code == "no_fresh_rows"
        assert timeout.value.stats.rows_seen == 1
        follower.close()


def test_r005_incident_is_fresh_rows_with_prefixed_schema_not_no_fresh(
    tmp_path: Path,
) -> None:
    incident = json.loads(
        (
            ROOT
            / "tests/fixtures/step5d_r005_post_ack_csv_schema_incident.json"
        ).read_text(encoding="utf-8")
    )
    assert incident["source"]["sha256"] == (
        "5d33a098ad4d6236b6ae0d615a55c194a0bc8f1db58a0e739dc4aca98e341dd9"
    )
    assert incident["source"]["summary_sha256"] == (
        "babef54f37950dcb396f1d83a234379ce9e5a39c9a2bae22c6613bd04d009f36"
    )
    assert incident["source"]["state_76_row_count"] == 5013
    assert incident["observed_lifecycle"] == {
        "ack_sequence": 2,
        "ack_consumed": True,
        "ready_near_state": 76,
        "arm2_sent": False,
        "bundle_committed": True,
    }

    rows = incident["minimal_rows"]
    fields = tuple(
        dict.fromkeys(
            ("t_monotonic_s",)
            + tuple(name for row in rows for name in row)
        )
    )
    csv_path = tmp_path / "r005-fixture.csv"
    with csv_path.open("x", newline="", encoding="utf-8") as handle:
        writer = ProductionCsvWriter(handle, fields)
        follower = BridgeCsvFollower(csv_path)
        for index, source in enumerate(rows):
            writer.writerow({"t_monotonic_s": index * 0.01, **source})
        writer.flush(durable=False)

        expected_unprefixed = tuple(
            incident["deterministic_root_cause"]["collector_expected"]
        )
        with pytest.raises(BridgeCsvTimeout) as timeout:
            follower.wait_for(
                lambda row: all(row[name] not in (None, "") for name in expected_unprefixed),
                required_columns=expected_unprefixed,
                timeout_s=0.05,
            )
        failure = timeout.value
        assert failure.code == "fresh_rows_never_qualified"
        assert failure.stats.rows_seen == len(rows)
        assert failure.stats.predicate_rejects == len(rows)
        assert set(failure.missing_columns) == set(expected_unprefixed)
        assert "output_double_register_35" in failure.missing_columns
        assert "ur_output_double_register_35" in follower.fieldnames
        follower.close()


def test_timeout_codes_distinguish_no_rows_and_unsealed_terminal(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "empty-growing.csv"
    with csv_path.open("x", newline="", encoding="utf-8") as handle:
        ProductionCsvWriter(handle, ("t_monotonic_s", "ur_output_int_register_26"))
        follower = BridgeCsvFollower(csv_path)
        with pytest.raises(BridgeCsvTimeout) as no_rows:
            next(follower.rows(timeout_s=0.02))
        assert no_rows.value.code == "no_fresh_rows"

        follower.mark_terminal_seen(tmp_path / "missing-capture.csv")
        with pytest.raises(BridgeCsvTimeout) as unsealed:
            next(follower.rows(timeout_s=0.02))
        assert unsealed.value.code == "terminal_seen_capture_not_sealed"
        follower.close()
