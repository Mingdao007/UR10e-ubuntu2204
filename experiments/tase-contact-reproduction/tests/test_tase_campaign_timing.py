from tase_campaign_timing import reduce_attempt_timing


def test_reduce_attempt_timing_preserves_full_lifecycle_and_wall_time():
    stages = [
        "HOME_CHECK", "CONTACT_SEARCH", "CONTACT_LATCH", "QUALIFICATION",
        "READINESS_HOLD", "ENTRY", "PATH", "STOP", "UNLOAD_RELIEF",
        "CLEARANCE", "HOME",
    ]
    result = reduce_attempt_timing(
        [{"stage": stage, "timestamp_s": float(index)} for index, stage in enumerate(stages)],
        attempt_started_at=0.0,
        attempt_finished_at=12.5,
    )
    assert result["complete_lifecycle"] is True
    assert result["durations_s"]["PATH_to_STOP"] == 1.0
    assert result["wall_duration_s"] == 12.5


def test_reduce_attempt_timing_keeps_partial_failure_timeline():
    result = reduce_attempt_timing(
        [{"stage": "HOME_CHECK", "timestamp_s": 10.0}, {"stage": "CONTACT_SEARCH", "timestamp_s": 11.0}]
    )
    assert result["complete_lifecycle"] is False
    assert result["durations_s"] == {"HOME_CHECK_to_CONTACT_SEARCH": 1.0}
