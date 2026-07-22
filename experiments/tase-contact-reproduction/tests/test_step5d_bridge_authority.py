from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_bridge_authority as authority  # noqa: E402
from step5d_autotune_v3.governance import load_current_launch_attempt  # noqa: E402


def test_owner_epoch_fences_overlap_and_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pid = 42
    owner_starttime = 700
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_pid)
    monkeypatch.setattr(
        authority,
        "read_proc_starttime_ticks",
        lambda pid: owner_starttime if pid == owner_pid else None,
    )
    first = authority.begin(
        tmp_path,
        attempt_id="attempt-1",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
    )
    assert first["state"] == "ACTIVE"
    assert first["sequence"] == 1
    assert authority.begin(
        tmp_path,
        attempt_id="attempt-1",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
    ) == first
    with pytest.raises(authority.BridgeAuthorityError, match="another canonical"):
        authority.begin(
            tmp_path,
            attempt_id="attempt-2",
            owner_pid=owner_pid,
            owner_starttime_ticks=owner_starttime,
        )
    revoked = authority.revoke(
        tmp_path,
        attempt_id="attempt-1",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
        reason="cancelled",
    )
    assert revoked["state"] == "REVOKED"
    assert revoked["sequence"] == 2
    second = authority.begin(
        tmp_path,
        attempt_id="attempt-2",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
    )
    assert second["state"] == "ACTIVE"
    assert second["sequence"] == 3


def test_revoked_epoch_invalidates_route_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pid = 42
    owner_starttime = 700
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_pid)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: owner_starttime)
    active = authority.begin(
        tmp_path,
        attempt_id="attempt-1",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
    )
    authority.revoke(
        tmp_path,
        attempt_id="attempt-1",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
        reason="failed",
    )
    current = authority.load_current(tmp_path)
    assert current is not None
    assert current["sequence"] != active["sequence"]
    assert current["state"] == "REVOKED"


def test_runtime_gate_is_journaled_before_governed_runtime_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pid = 42
    owner_starttime = 700
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_pid)
    monkeypatch.setattr(
        authority,
        "read_proc_starttime_ticks",
        lambda pid: owner_starttime if pid == owner_pid else None,
    )
    authority.begin(
        tmp_path,
        attempt_id="attempt-runtime",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
    )

    started = authority.record_runtime_attempt(
        tmp_path,
        attempt_id="attempt-runtime",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
        state="STARTED",
    )
    assert started["attestation"]["phase"] == "runtime_gate"
    assert started["attestation"]["schema"] == "step5d.autotune-v3/launch-attempt-v1"

    failed = authority.record_runtime_attempt(
        tmp_path,
        attempt_id="attempt-runtime",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
        state="FAILED",
        exit_code=78,
        reason_code="RUNTIME_NOT_PROVISIONED",
        detail="runtime pointer unavailable",
    )
    observed, _pointer = load_current_launch_attempt(tmp_path)
    assert observed == failed["attestation"]
    assert observed["reason_code"] == "RUNTIME_NOT_PROVISIONED"


def test_runtime_recorder_rejects_non_owner_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pid = 42
    owner_starttime = 700
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_pid)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: owner_starttime)
    authority.begin(
        tmp_path,
        attempt_id="attempt-runtime",
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime,
    )
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_pid + 1)

    with pytest.raises(authority.BridgeAuthorityError, match="bound shell owner"):
        authority.record_runtime_attempt(
            tmp_path,
            attempt_id="attempt-runtime",
            owner_pid=owner_pid,
            owner_starttime_ticks=owner_starttime,
            state="STARTED",
        )
