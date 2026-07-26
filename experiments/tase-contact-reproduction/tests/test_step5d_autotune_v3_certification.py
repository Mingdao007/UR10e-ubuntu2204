from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_autotune_v3.certification import (  # noqa: E402
    CertificationProtocolError,
    CertificationSession,
    EXECUTION_PROFILE_ID,
)
from ur10e_experiment_runtime.authorization import (  # noqa: E402
    CERTIFICATION_PROCEDURES,
    STEP5D_V3_STAGE_IDENTITY,
    CertificationMotionAuthorization,
)


def _authorization() -> CertificationMotionAuthorization:
    now = datetime.now(timezone.utc)
    return CertificationMotionAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        release_basis_fingerprint="1" * 64,
        deployment_fingerprint="2" * 64,
        plant_epoch=7,
        deployment_readback_sha256="3" * 64,
        allowed_procedures=CERTIFICATION_PROCEDURES,
        max_linear_speed_m_s=0.09,
        max_linear_acceleration_m_s2=0.135,
        max_angular_speed_rad_s=0.05,
        max_angular_acceleration_rad_s2=0.1,
        numeric_margin_m=0.001,
        authorization_source="explicit_attended_fixture",
        authorized_at=(now - timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )


def _row(
    state: int,
    *,
    session: CertificationSession,
    speed: float = 0.0,
    ready_identity: bool = False,
    safety_mode: int = 1,
) -> dict[str, object]:
    handshake = session.handshake
    row: dict[str, object] = {
        "timestamp": 123.456,
        "safety_mode": safety_mode,
        "actual_TCP_speed": [speed, 0.0, 0.0, 0.0, 0.0, 0.0],
        "output_int_register_26": state,
        "output_int_register_28": 0,
        "output_int_register_30": handshake["command_seq"],
        "output_int_register_31": handshake["batch_row_index"],
        "output_int_register_32": 3,
    }
    for index, name in ((24, "campaign_epoch"), (25, "trial_id"), (27, "candidate_token"), (29, "execution_profile_id")):
        row[f"output_int_register_{index}"] = 0 if ready_identity else handshake[name]
    return row


def _start(session: CertificationSession) -> None:
    assert session.poll(_row(10, session=session, ready_identity=True)) is True
    assert session.handshake["execution_profile_id"] == EXECUTION_PROFILE_ID


def test_fixed_sequence_cannot_become_a_campaign() -> None:
    authorization = _authorization()
    session = CertificationSession(authorization)
    assert [step.procedure for step in session.steps] == [
        "direct_exact_stop",
        "direct_exact_stop",
        "direct_exact_stop",
        "stale_watchdog_exact_stop",
        "stale_watchdog_exact_stop",
        "stale_watchdog_exact_stop",
        "return_route",
    ]
    assert [step.command for step in session.steps] == [4, 4, 4, 5, 5, 5, 6]
    assert all(step.ticket.document()["campaign_allowed"] is False for step in session.steps)
    repeated = CertificationSession(authorization)
    assert [step.trial_id for step in session.steps] == [
        step.trial_id for step in repeated.steps
    ]
    assert len({step.ticket.ticket_sha256 for step in session.steps}) == 7


def test_ticket_is_revalidated_before_each_motion() -> None:
    session = CertificationSession(_authorization())
    session.steps = (
        replace(
            session.steps[0],
            ticket=replace(session.steps[0].ticket, max_linear_speed_m_s=0.005),
        ),
        *session.steps[1:],
    )
    with pytest.raises(CertificationProtocolError, match="envelope is insufficient"):
        _start(session)


def test_direct_stop_requests_existing_float_stop_transport() -> None:
    session = CertificationSession(_authorization())
    _start(session)
    session.poll(_row(80, session=session, speed=0.006))
    assert session.stop_request is True
    assert session.hold_heartbeat is False
    assert session.trigger_controller_timestamp_s == 123.456
    session.poll(_row(81, session=session, speed=0.006))
    assert session.stop_request is False


def test_stale_watchdog_holds_then_restores_heartbeat() -> None:
    session = CertificationSession(_authorization())
    session.step_index = 3
    _start(session)
    session.poll(_row(80, session=session, speed=0.006))
    assert session.hold_heartbeat is True
    assert session.stop_request is False
    assert session.trigger_controller_timestamp_s == 123.456
    session.poll(_row(81, session=session, speed=0.004))
    assert session.hold_heartbeat is False


def test_safe_closure_advances_only_after_exact_ready_home() -> None:
    session = CertificationSession(_authorization())
    _start(session)
    session.poll(_row(80, session=session, speed=0.006))
    session.poll(_row(81, session=session, speed=0.004))
    session.poll(_row(82, session=session))
    session.poll(_row(85, session=session))
    assert session.awaiting_ready is True
    next_ready = _row(10, session=session, ready_identity=True)
    assert session.poll(next_ready) is True
    assert session.step_index == 1


def test_return_preparation_is_ticket_bound_before_motion() -> None:
    session = CertificationSession(_authorization())
    session.step_index = 6
    _start(session)
    session.poll(_row(83, session=session, speed=0.006))
    session.poll(_row(84, session=session, speed=0.006))
    session.poll(_row(85, session=session))
    assert session.awaiting_ready is True


def test_prestart_stale_fault_waits_for_fresh_ready_home() -> None:
    session = CertificationSession(_authorization())
    stale_fault = _row(90, session=session)
    stale_fault["output_int_register_24"] = 1
    stale_fault["output_int_register_25"] = 1_700_182_342
    stale_fault["output_int_register_27"] = 1_021_235_430
    stale_fault["output_int_register_28"] = 18
    stale_fault["output_int_register_30"] = 1

    assert session.poll(stale_fault) is False
    assert session.started is False
    assert session.handshake["command_seq"] == 0
    assert session.poll(_row(10, session=session, ready_identity=True)) is True
    assert session.started is True
    assert session.handshake["command_seq"] == 1


def test_started_fault_requires_current_identity_and_reports_reason() -> None:
    session = CertificationSession(_authorization())
    _start(session)
    current_fault = _row(90, session=session)
    current_fault["output_int_register_28"] = 18
    with pytest.raises(CertificationProtocolError, match="fault reason=18"):
        session.poll(current_fault)

    session = CertificationSession(_authorization())
    _start(session)
    mismatched_fault = _row(90, session=session)
    mismatched_fault["output_int_register_27"] = 0
    with pytest.raises(CertificationProtocolError, match="fault identity differs"):
        session.poll(mismatched_fault)


def test_identity_or_safety_drift_fails_closed() -> None:
    session = CertificationSession(_authorization())
    _start(session)
    bad_identity = _row(80, session=session, speed=0.006)
    bad_identity["output_int_register_27"] = 0
    with pytest.raises(CertificationProtocolError, match="identity differs"):
        session.poll(bad_identity)

    session = CertificationSession(_authorization())
    _start(session)
    with pytest.raises(CertificationProtocolError, match="NORMAL"):
        session.poll(_row(80, session=session, safety_mode=3))
