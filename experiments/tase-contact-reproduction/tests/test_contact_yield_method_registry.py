from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_yield_live_path import parse_live_duration
from contact_yield_method_registry import (
    MethodUnavailableError,
    load_method_records,
    resolve_method,
)
from contact_yield_protocol import PERIOD_S


def test_native_methods_keep_source_binding_and_tase_is_unavailable():
    records = load_method_records()
    for name in ("SFC", "DSFC", "MSFC"):
        record = resolve_method(name)
        assert record.available is True
        assert record.family == "native_yield"
        assert record.source_binding == "yield_native_route_v1"
    with pytest.raises(MethodUnavailableError, match="not implemented"):
        resolve_method("TASE_RNN")
    with pytest.raises(MethodUnavailableError, match="not implemented"):
        resolve_method("TASE_QP")
    assert records["TASE_RNN"].family == "tase_rnn"
    assert records["TASE_QP"].family == "tase_qp"
    assert records["TASE_RNN"].source_binding is None


def test_duration_labels_are_not_interchangeable():
    short = parse_live_duration("2")
    ten = parse_live_duration("10s")
    full = parse_live_duration("full")
    period = parse_live_duration(PERIOD_S)
    assert short.kind == "diagnostic" and short.path_duration_s == 2.0
    assert ten.kind == "diagnostic" and ten.path_duration_s == 10.0
    assert full.kind == "full_period" and full.path_duration_s == PERIOD_S
    assert period.kind == "full_period"
    assert short.formally_qualified is False and short.full_cycle_acceptance is False
    assert full.formally_qualified is False
    with pytest.raises(Exception, match="cannot be labeled full"):
        parse_live_duration(3.0)
