from analyze_qp_motion_gate import common_clock_join, pearson


def test_pearson_rejects_constant_stream_and_recognizes_positive_relation():
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0
    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


def test_common_clock_join_filters_held_and_unmatched_observations():
    commands = {
        10: {"reference_phase": "path"},
        11: {"reference_phase": "path"},
    }
    frames = [
        {"timestamp": 1.0, "consumed_packet_sequence": 10, "integer_echoes": {"26": 25}},
        {"timestamp": 1.002, "consumed_packet_sequence": 10, "integer_echoes": {"26": 25}},
        {"timestamp": 1.004, "consumed_packet_sequence": 11, "integer_echoes": {"26": 25}},
        {"timestamp": 1.006, "consumed_packet_sequence": 12, "integer_echoes": {"26": 25}},
        {"timestamp": 1.008, "consumed_packet_sequence": 13, "integer_echoes": {"26": 40}},
    ]

    joined, counts = common_clock_join(frames, commands)

    assert [row[0] for row in joined] == [10, 11]
    assert counts == {
        "state25_rtde_rows": 4,
        "formal_path_command_joins": 2,
        "state25_rows_without_formal_path_command": 1,
        "held_or_duplicate_rows_filtered": 1,
    }
