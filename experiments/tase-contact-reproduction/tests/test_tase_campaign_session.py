import itertools
import pytest

from tase_campaign_session import CampaignSession, CampaignSessionError, SessionState


def test_prepare_once_reuses_identity_and_attempt_timing(tmp_path):
    wall = iter([100.0, 101.0, 102.0, 103.0, 104.0])
    mono = iter([10.0, 11.5, 12.0, 13.0, 14.0])
    kwargs = dict(
        protocol_id="figure8_window60_r013_compat_v1",
        duration_token="r013_60",
        method="TASE_RNN_MATURE",
        monotonic_clock=lambda: next(mono),
        wall_clock=lambda: next(wall),
    )
    session = CampaignSession.prepare_once(tmp_path / "campaign", **kwargs)
    assert session.state is SessionState.PREPARED
    session.start_attempt("screen-A-00", 0)
    receipt = session.finish_attempt(status="complete", returncode=0)
    assert receipt["elapsed_s"] == 1.5
    reused = CampaignSession.prepare_once(tmp_path / "campaign", **kwargs)
    assert reused.preparation_reused is True
    assert reused.session_id == session.session_id
    assert reused.summary()["attempt_count"] == 1


def test_stop_and_resume_recover_interrupted_attempt(tmp_path):
    clock = itertools.count(1.0)
    session = CampaignSession.prepare_once(
        tmp_path / "campaign",
        protocol_id="p", duration_token="r013_60", method="TASE_RNN_MATURE",
        monotonic_clock=lambda: next(clock), wall_clock=lambda: next(clock),
    )
    session.start_attempt("a", 0)
    session.stop("test_stop")
    assert session.state is SessionState.STOPPING
    session.resume()
    assert session.state is SessionState.PREPARED
    assert session.summary()["resume_count"] == 1
    assert session.summary()["last_attempt"]["status"] == "interrupted"


def test_prepare_once_rejects_protocol_identity_change(tmp_path):
    CampaignSession.prepare_once(
        tmp_path / "campaign",
        protocol_id="p", duration_token="r013_60", method="TASE_RNN_MATURE",
    )
    with pytest.raises(CampaignSessionError, match="protocol_id differs"):
        CampaignSession.prepare_once(
            tmp_path / "campaign",
            protocol_id="full", duration_token="full", method="TASE_RNN_MATURE",
        )
