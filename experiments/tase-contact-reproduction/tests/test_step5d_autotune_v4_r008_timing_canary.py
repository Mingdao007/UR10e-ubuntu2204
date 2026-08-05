"""r008 timing_canary: skip QUAL and start at ANCHOR (formal default stays 3×QUAL)."""

from __future__ import annotations

from types import SimpleNamespace

from step5d_autotune_v4_r005.runtime import CampaignPhase
from step5d_autotune_v4_r008.live_adapter import (
    R008HostLoop,
    apply_timing_canary_skip_qual,
)
import run_step5d_autotune_v4_r008_b3_two_stage as b3_host
import supervise_step5d_autotune_v4_r008_b3_host as supervise


def test_apply_timing_canary_skip_qual_starts_at_anchor() -> None:
    host = SimpleNamespace(
        qualification_passes=0,
        phase=CampaignPhase.QUALIFICATION,
        events=[],
    )
    apply_timing_canary_skip_qual(host)
    assert host.qualification_passes == 3
    assert host.phase == R008HostLoop._ANCHOR
    assert host.phase != CampaignPhase.QUALIFICATION
    assert host.events == ["R008_TIMING_CANARY:skip_qual"]


def test_timing_canary_does_not_require_three_qual_passes() -> None:
    """Bare HostLoop state after canary arming is ready for ANCHOR refill."""

    loop = object.__new__(R008HostLoop)
    loop.qualification_passes = 0
    loop.phase = CampaignPhase.QUALIFICATION
    loop.events = []
    apply_timing_canary_skip_qual(loop)
    assert loop.qualification_passes >= 3
    assert loop.phase == R008HostLoop._ANCHOR
    assert "R008_TIMING_CANARY:skip_qual" in loop.events


def test_b3_host_cli_accepts_timing_canary_flag() -> None:
    live = b3_host.build_parser()._subparsers._group_actions[0].choices["live"]
    action = next(a for a in live._actions if "--timing-canary" in getattr(a, "option_strings", ()))
    assert action.dest == "timing_canary"
    assert action.default is False
    assert action.const is True


def test_overlap_canary_pass_criteria_helpers() -> None:
    """Live canary success: dispatch_s < 5 and no dispatch≈seal_wall coupling."""

    from statistics import median

    from step5d_autotune_v4_r008.async_seal import dispatch_s_hides_seal_wall

    dispatch_samples = [1.2, 1.5, 1.8, 2.0]
    seal_walls = [32.5, 33.1, 32.8, 33.0]
    assert median(dispatch_samples) < 5.0
    assert not any(
        dispatch_s_hides_seal_wall(d, w) for d, w in zip(dispatch_samples, seal_walls)
    )


def test_supervise_forwards_timing_canary_to_host_argv(tmp_path) -> None:
    ids = {
        "route_id": "r",
        "attempt_id": "a",
        "session_id": "s",
        "session_epoch": 1,
        "script": "aa",
        "txt": "bb",
        "urp": "cc",
        "campaign": "dd",
        "contract": "ee",
    }
    argv = supervise._host_argv(
        control_python=tmp_path / "python",
        run_dir=tmp_path,
        ids=ids,
        controller_host="192.168.1.18",
        kunwei_host="192.168.50.25",
        kunwei_port=5152,
        release_manifest_sha256="0" * 64,
        eoat_sha256="1" * 64,
        timing_canary=True,
    )
    assert "--timing-canary" in argv
    argv_off = supervise._host_argv(
        control_python=tmp_path / "python",
        run_dir=tmp_path,
        ids=ids,
        controller_host="192.168.1.18",
        kunwei_host="192.168.50.25",
        kunwei_port=5152,
        release_manifest_sha256="0" * 64,
        eoat_sha256="1" * 64,
        timing_canary=False,
    )
    assert "--timing-canary" not in argv_off
