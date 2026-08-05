"""Offline tests for robot body truth (no robot / no RTDE)."""

from __future__ import annotations

import json
from pathlib import Path

from step5d_autotune_v4_r008.robot_body_truth import (
    BodyVerdict,
    RobotBodyTruth,
    SCHEMA,
    classify_run_dir,
    diff_motion,
    format_human,
    sample_from_run_dir,
    snapshot_from_state20_row,
    verdict,
)


def _snap(
    *,
    t: float,
    z: float,
    tp: int = 20,
    force: float = 0.0,
    normal: float = 0.0,
    vz: float | None = None,
    stationary: bool | None = None,
    program_running: bool | None = None,
    command_mode: int | None = None,
    x: float = 0.5,
    y: float = 0.1,
) -> RobotBodyTruth:
    speed = None
    if vz is not None:
        speed = (0.0, 0.0, vz, 0.0, 0.0, 0.0)
        if stationary is None:
            stationary = abs(vz) < 0.0005
    return RobotBodyTruth(
        schema=SCHEMA,
        wall_time_s=t,
        source="synthetic",
        tp_state=tp,
        command_mode=command_mode,
        packet_sequence=1,
        tcp_pose_m_rad=(x, y, z, 0.0, 0.0, 0.0),
        tcp_speed_m_s_rad_s=speed,
        linear_speed_m_s=abs(vz) if vz is not None else None,
        vz_m_s=vz,
        stationary=stationary,
        normal_load_n=normal,
        force_norm_n=force,
        sensor_fresh=True,
        program_state="PLAYING" if program_running else None,
        program_running=program_running,
        safety_mode="Safetymode: NORMAL",
        robot_mode="Robotmode: RUNNING",
        host_claim_phase="CONTACT_SEARCH",
        path_rows=1,
    )


def test_descending_z_is_moving_down() -> None:
    a = _snap(t=1.0, z=0.033, vz=-0.005, force=0.05)
    b = _snap(t=1.2, z=0.028, vz=-0.005, force=0.08)
    motion = diff_motion(a, b)
    assert motion.dz_m < 0
    assert verdict(b, prior=a, motion=motion) == BodyVerdict.MOVING_DOWN


def test_flat_z_high_force_is_contacted_stalled() -> None:
    a = _snap(t=1.0, z=0.0203, vz=0.0, force=2.7, normal=2.7, stationary=True)
    b = _snap(t=1.4, z=0.0203, vz=0.0, force=8.6, normal=2.7, stationary=True)
    motion = diff_motion(a, b)
    assert abs(motion.dz_m) < 1e-9
    v = verdict(b, prior=a, motion=motion)
    assert v == BodyVerdict.CONTACTED_STALLED
    # PLAYING must not flip the verdict to moving.
    b_play = _snap(
        t=1.4,
        z=0.0203,
        vz=0.0,
        force=8.6,
        normal=2.7,
        stationary=True,
        program_running=True,
    )
    assert verdict(b_play, prior=a, motion=motion) == BodyVerdict.CONTACTED_STALLED
    text = format_human(b_play, BodyVerdict.CONTACTED_STALLED)
    assert "claim≠body" in text


def test_playing_stationary_no_force_is_holding_ready() -> None:
    snap = _snap(
        t=1.0,
        z=0.12,
        tp=78,
        vz=0.0,
        force=0.02,
        stationary=True,
        program_running=True,
    )
    assert verdict(snap) == BodyVerdict.HOLDING_READY


def test_never_moving_from_program_running_alone() -> None:
    snap = RobotBodyTruth(
        schema=SCHEMA,
        wall_time_s=1.0,
        source="synthetic",
        tp_state=None,
        command_mode=None,
        packet_sequence=None,
        tcp_pose_m_rad=None,
        tcp_speed_m_s_rad_s=None,
        linear_speed_m_s=None,
        vz_m_s=None,
        stationary=None,
        normal_load_n=None,
        force_norm_n=None,
        sensor_fresh=None,
        program_state="PLAYING",
        program_running=True,
        safety_mode="Safetymode: NORMAL",
        robot_mode="Robotmode: RUNNING",
        host_claim_phase="CONTACT_SEARCH",
        path_rows=1,
    )
    assert verdict(snap) == BodyVerdict.UNKNOWN


def test_023243_state20_tail_classifies_stalled(tmp_path: Path) -> None:
    src = Path(
        "/home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/"
        "tase-contact-reproduction/runs/step5d_autotune_v4_r008/"
        "live_20260805_023243_b3_path60_overlap_canary/r008-state20-search-trace.jsonl"
    )
    if not src.is_file():
        # Fixture may be absent in CI clones; synthesize the stall pair.
        rows = [
            {
                "schema": "step5d.autotune-v4/r008-state20-search-trace-v1",
                "monotonic_s": 100.0,
                "tp_state": 20,
                "command_mode": 0,
                "normal_load_n": 0.29,
                "force_norm_n": 0.31,
                "tcp_pose_m_rad": [0.48, 0.12, 0.02035, 3.12, 0.0, 0.06],
            },
            {
                "schema": "step5d.autotune-v4/r008-state20-search-trace-v1",
                "monotonic_s": 100.4,
                "tp_state": 20,
                "command_mode": 4,
                "normal_load_n": 2.68,
                "force_norm_n": 8.63,
                "tcp_pose_m_rad": [0.48, 0.12, 0.02035, 3.12, 0.0, 0.06],
            },
        ]
        (tmp_path / "r008-state20-search-trace.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        run_dir = tmp_path
    else:
        # Copy only the last few lines into an isolated run dir.
        lines = src.read_text(encoding="utf-8").splitlines()[-6:]
        run_dir = tmp_path
        (run_dir / "r008-state20-search-trace.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    snap, v, motion = classify_run_dir(run_dir)
    assert snap.source == "run_dir_state20"
    assert snap.tp_state == 20
    assert motion is not None
    assert abs(motion.dz_m) < 0.001
    assert v == BodyVerdict.CONTACTED_STALLED


def test_sample_from_run_dir_empty_is_unknown(tmp_path: Path) -> None:
    snap = sample_from_run_dir(tmp_path)
    assert snap.source == "run_dir_empty"
    assert verdict(snap) == BodyVerdict.UNKNOWN


def test_snapshot_from_state20_row_roundtrip() -> None:
    row = {
        "monotonic_s": 12.5,
        "tp_state": 25,
        "force_norm_n": 1.2,
        "normal_load_n": 0.8,
        "tcp_pose_m_rad": [0.1, 0.2, 0.3, 0.0, 0.0, 0.0],
        "command_mode": 2,
    }
    snap = snapshot_from_state20_row(row)
    assert snap.tp_state == 25
    assert snap.tcp_pose_m_rad is not None
    assert snap.tcp_pose_m_rad[2] == 0.3
