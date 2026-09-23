from analyze_qp_force_burst import group_exceedances


def sample(time_s: float, sequence: int, force_n: float) -> dict:
    return {
        "host_use_monotonic_s": time_s,
        "packet_sequence": sequence,
        "corrected_wrench_n_nm": [0.0, 0.0, -force_n, 0.0, 0.0, 0.0],
    }


def test_force_burst_groups_only_threshold_exceedances_within_gap():
    samples = [
        sample(1.000, 1, 9.9),
        sample(1.002, 2, 10.1),
        sample(1.008, 3, 12.0),
        sample(1.021, 4, 13.0),
        sample(1.050, 5, 20.0),
    ]

    clusters = group_exceedances(samples, threshold_n=10.0, maximum_gap_s=0.012)

    assert [[row["packet_sequence"] for row in cluster] for cluster in clusters] == [[2, 3], [4], [5]]


def test_force_burst_rejects_invalid_analysis_thresholds():
    import pytest

    with pytest.raises(ValueError, match="threshold_n"):
        group_exceedances([sample(1.0, 1, 12.0)], threshold_n=0)
    with pytest.raises(ValueError, match="maximum_gap_s"):
        group_exceedances([sample(1.0, 1, 12.0)], maximum_gap_s=0)
