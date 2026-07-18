from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_execution_readiness as readiness  # noqa: E402
import verify_step5d_autotune_v3_hil_authorization as gate  # noqa: E402


THREAD_ID = "019f71d6-7870-7831-9362-e951de6daa96"
AUTHORIZATION_ID = "019f71d6-aaaa-7bbb-8ccc-e951de6daa96"
NOW = datetime(2026, 7, 18, 14, 30, tzinfo=timezone.utc)


def _authorization(**overrides) -> dict:
    payload = {
        "schema": gate.SCHEMA,
        "authorization_id": AUTHORIZATION_ID,
        "thread_id": THREAD_ID,
        "issued_by": "user_current_turn_explicit",
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=9)).isoformat(),
        "user_instruction_sha256": "1" * 64,
        "candidate_stage_id": gate.V3_STAGE_ID,
        "scope": gate.SCOPE,
        "identity": readiness.verify(ROOT)["identity"],
        "controller_identity_policy": "fresh_read_only_snapshot_before_connection",
        "serial": True,
        "hold_required": True,
        "live_writer_allowed": False,
        "operator_action_consumed": False,
        "allowed_actions": gate.ALLOWED_ACTIONS,
        "forbidden_actions": gate.FORBIDDEN_ACTIONS,
    }
    payload.update(overrides)
    return payload


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_candidate_scoped_current_turn_hold_authorization_passes(tmp_path: Path) -> None:
    report = gate.verify_authorization(
        _write(tmp_path, _authorization()),
        expected_thread_id=THREAD_ID,
        root=ROOT,
        now=NOW,
    )
    assert report["ok"] is True
    assert report["authorized"] is True
    assert report["scope"] == "hil_hold_only"
    assert report["live_writer_allowed"] is False
    assert report["next_legal_action"] == (
        "capture a fresh read-only controller identity snapshot"
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("candidate_stage_id", "step5d_strict_rnn_autotune_v1", "candidate"),
        ("scope", "live_motion", "scope"),
        ("issued_by", "historical_resolver", "issuer"),
        ("live_writer_allowed", True, "live_writer_allowed"),
        ("operator_action_consumed", True, "operator_action_consumed"),
        ("user_instruction_sha256", "0" * 64, "placeholder"),
    ],
)
def test_authorization_identity_and_scope_mutations_fail_closed(
    tmp_path: Path,
    field: str,
    value,
    message: str,
) -> None:
    with pytest.raises(gate.AuthorizationError, match=message):
        gate.verify_authorization(
            _write(tmp_path, _authorization(**{field: value})),
            expected_thread_id=THREAD_ID,
            root=ROOT,
            now=NOW,
        )


def test_control_fingerprint_mismatch_fails_closed(tmp_path: Path) -> None:
    payload = _authorization()
    payload["identity"]["control_fingerprint"] = "2" * 64
    with pytest.raises(gate.AuthorizationError, match="authorization identity"):
        gate.verify_authorization(
            _write(tmp_path, payload),
            expected_thread_id=THREAD_ID,
            root=ROOT,
            now=NOW,
        )


def test_authorization_must_bind_the_current_thread(tmp_path: Path) -> None:
    with pytest.raises(gate.AuthorizationError, match="current-turn thread binding"):
        gate.verify_authorization(
            _write(tmp_path, _authorization()),
            expected_thread_id="019f71d6-bbbb-7ccc-8ddd-e951de6daa96",
            root=ROOT,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "message"),
    [
        (NOW - timedelta(hours=1), NOW - timedelta(minutes=30), "expired"),
        (NOW - timedelta(minutes=1), NOW + timedelta(minutes=31), "TTL"),
        (NOW + timedelta(minutes=1), NOW + timedelta(minutes=10), "future"),
    ],
)
def test_authorization_time_window_fails_closed(
    tmp_path: Path,
    issued_at: datetime,
    expires_at: datetime,
    message: str,
) -> None:
    payload = _authorization(
        issued_at=issued_at.isoformat(),
        expires_at=expires_at.isoformat(),
    )
    with pytest.raises(gate.AuthorizationError, match=message):
        gate.verify_authorization(
            _write(tmp_path, payload),
            expected_thread_id=THREAD_ID,
            root=ROOT,
            now=NOW,
        )


def test_action_allowlist_and_denylist_are_exact(tmp_path: Path) -> None:
    allowed = [*gate.ALLOWED_ACTIONS, "program_load"]
    with pytest.raises(gate.AuthorizationError, match="allowlist"):
        gate.verify_authorization(
            _write(tmp_path, _authorization(allowed_actions=allowed)),
            expected_thread_id=THREAD_ID,
            root=ROOT,
            now=NOW,
        )
    forbidden = gate.FORBIDDEN_ACTIONS[:-1]
    with pytest.raises(gate.AuthorizationError, match="denylist"):
        gate.verify_authorization(
            _write(tmp_path, _authorization(forbidden_actions=forbidden)),
            expected_thread_id=THREAD_ID,
            root=ROOT,
            now=NOW,
        )


def test_extra_authorization_field_and_cli_failure_are_blocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(tmp_path, _authorization(unreviewed_override=True))
    assert gate.main(
        [
            "--authorization",
            str(path),
            "--expected-thread-id",
            THREAD_ID,
            "--root",
            str(ROOT),
            "--json",
        ]
    ) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["authorized"] is False
    assert "fields differ" in report["blocker"]
