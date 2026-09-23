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


def test_native_methods_keep_source_binding_and_qp_has_a_distinct_live_binding():
    records = load_method_records()
    for name in ("SFC", "DSFC", "MSFC"):
        record = resolve_method(name)
        assert record.available is True
        assert record.family == "native_yield"
        assert record.source_binding == "yield_native_route_v1"
    with pytest.raises(MethodUnavailableError, match="not implemented"):
        resolve_method("TASE_RNN")
    qp = resolve_method("TASE_QP")
    assert qp.available is True
    assert qp.family == "tase_qp"
    assert qp.provider == "TaseContactProvider"
    assert qp.source_binding == "tase-qp-osqp-codegen-c-v1"
    assert records["TASE_RNN"].family == "tase_rnn"
    assert records["TASE_QP"].family == "tase_qp"
    assert records["TASE_QP"].available is True
    assert records["TASE_RNN"].source_binding is None


def test_live_qp_is_reported_separately_from_registered_unavailable_methods(tmp_path):
    from contact_yield_live import _controller_identity
    from contact_yield_live_writer import _select_tase_solver_profile
    from contact_yield_method_registry import native_status_payload
    from types import SimpleNamespace

    status = native_status_payload()
    assert status["tase_qp"]["name"] == "TASE_QP"
    assert status["tase_qp"]["available"] is True
    assert status["tase_qp"]["source_binding"] == "tase-qp-osqp-codegen-c-v1"
    assert [item["name"] for item in status["registered_unavailable"]] == ["TASE_RNN"]
    library = tmp_path / "libcontact_qp.so"
    library.write_bytes(b"profile identity fixture")
    profile = _select_tase_solver_profile(family="tase_qp", library=library)
    identity = _controller_identity(resolve_method("TASE_QP"), SimpleNamespace(solver_profile=profile))
    assert identity["method"] == "TASE_QP"
    assert identity["solver_backend"] == "osqp-codegen-c"
    assert identity["solver_profile"]["deadline_s"] == pytest.approx(.001)


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
