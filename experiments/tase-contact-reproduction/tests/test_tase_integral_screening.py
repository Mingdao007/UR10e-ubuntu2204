import json

from tase_integral_screening import build_schedule, load_screening_config


def test_screening_schedule_has_five_randomized_blocks_and_four_arms():
    rows = build_schedule(load_screening_config())
    assert len(rows) == 20
    assert [sum(row["arm_id"] == arm for row in rows) for arm in "ABCD"] == [5] * 4
    for block in range(5):
        assert sorted(row["arm_id"] for row in rows if row["block"] == block) == list("ABCD")
    assert len({(row["block"], row["position"]) for row in rows}) == 20


def test_screening_schedule_binds_incumbent_and_strategy_identity():
    rows = build_schedule(load_screening_config())
    assert {(row["Md_scalar"], row["Bd_scalar"]) for row in rows} == {
        (9.565272137974492, 693.6559295653944)
    }
    d = next(row for row in rows if row["arm_id"] == "D")
    assert d["frozen"]["force_integral_policy"] == "conditional-double-clamp-v1"
    assert d["frozen"]["force_integral_limit_n_s"] == 1.0
