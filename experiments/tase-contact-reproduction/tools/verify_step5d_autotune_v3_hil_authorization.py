#!/usr/bin/env python3
"""Verify a short-lived V3 full-production-bridge HOLD authorization."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import verify_step5d_autotune_v3_execution_readiness as execution_readiness


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "step5d.autotune-v3/hil-authorization-v2"
SCOPE = "hil_full_bridge_hold"
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
MAX_TTL = timedelta(minutes=30)
ALLOWED_ACTIONS = [
    "controller_identity_read",
    "dashboard_state_read",
    "rtde_output_read",
    "program_load_v3",
    "program_play_v3",
    "kunwei_stream_start_read",
    "production_bridge_start_hold",
    "rtde_hold_heartbeat_write",
    "hold_observation",
    "program_stop",
    "bridge_cleanup",
]
FORBIDDEN_ACTIONS = [
    "arm",
    "trial_dispatch",
    "campaign_runner_start",
    "zero_tare",
    "contact",
    "motion",
    "urscript_send",
    "tp_parameter_edit",
    "payload_tcp_write",
    "safety_write",
    "arbitrary_controller_write",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")


class AuthorizationError(RuntimeError):
    """The supplied authorization cannot admit the HIL HOLD-only lane."""


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorizationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AuthorizationError("authorization must be a regular JSON file")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                AuthorizationError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorizationError(f"authorization is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuthorizationError("authorization must be a JSON object")
    return payload


def _timestamp(value: Any, *, role: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AuthorizationError(f"{role} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AuthorizationError(f"{role} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorizationError(f"{role} lacks an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _require(actual: Any, expected: Any, role: str) -> None:
    if actual != expected:
        raise AuthorizationError(
            f"{role} differs: expected={expected!r}, observed={actual!r}"
        )


def verify_authorization(
    authorization_path: Path,
    *,
    expected_thread_id: str,
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    if _THREAD_ID.fullmatch(expected_thread_id) is None:
        raise AuthorizationError("expected current thread id is invalid")
    payload = _load_json(authorization_path.expanduser().absolute())
    required = {
        "schema",
        "authorization_id",
        "thread_id",
        "issued_by",
        "issued_at",
        "expires_at",
        "user_instruction_sha256",
        "candidate_stage_id",
        "scope",
        "identity",
        "controller_identity_policy",
        "serial",
        "hold_required",
        "live_writer_allowed",
        "operator_action_consumed",
        "allowed_actions",
        "forbidden_actions",
    }
    if set(payload) != required:
        raise AuthorizationError("authorization fields differ")
    _require(payload.get("schema"), SCHEMA, "authorization schema")
    authorization_id = payload.get("authorization_id")
    if not isinstance(authorization_id, str) or _THREAD_ID.fullmatch(authorization_id) is None:
        raise AuthorizationError("authorization id is invalid")
    _require(payload.get("thread_id"), expected_thread_id, "current-turn thread binding")
    _require(payload.get("issued_by"), "user_current_turn_explicit", "authorization issuer")
    instruction_sha = payload.get("user_instruction_sha256")
    if not isinstance(instruction_sha, str) or _SHA256.fullmatch(instruction_sha) is None:
        raise AuthorizationError("user instruction SHA-256 is invalid")
    if set(instruction_sha) == {"0"}:
        raise AuthorizationError("user instruction SHA-256 is a placeholder")
    _require(payload.get("candidate_stage_id"), V3_STAGE_ID, "authorization candidate")
    _require(payload.get("scope"), SCOPE, "authorization scope")

    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise AuthorizationError("verification clock lacks an explicit timezone")
    current = current.astimezone(timezone.utc)
    issued_at = _timestamp(payload.get("issued_at"), role="authorization issued_at")
    expires_at = _timestamp(payload.get("expires_at"), role="authorization expires_at")
    ttl = expires_at - issued_at
    if ttl <= timedelta(0) or ttl > MAX_TTL:
        raise AuthorizationError("authorization TTL must be in (0, 30 minutes]")
    if issued_at > current + timedelta(seconds=5):
        raise AuthorizationError("authorization issued_at is in the future")
    if current > expires_at:
        raise AuthorizationError("authorization is expired")

    readiness = execution_readiness.verify(root)
    _require(
        readiness.get("state"),
        "ready_for_hil_full_bridge_hold_authorization",
        "release readiness",
    )
    _require(readiness.get("ready_to_execute"), False, "pre-HIL execution boundary")
    _require(payload.get("identity"), readiness.get("identity"), "authorization identity")
    _require(
        payload.get("controller_identity_policy"),
        "fresh_read_only_snapshot_before_connection",
        "controller identity policy",
    )
    for field, expected in (
        ("serial", True),
        ("hold_required", True),
        ("live_writer_allowed", True),
        ("operator_action_consumed", False),
    ):
        _require(payload.get(field), expected, f"authorization {field}")
    _require(payload.get("allowed_actions"), ALLOWED_ACTIONS, "HIL action allowlist")
    _require(payload.get("forbidden_actions"), FORBIDDEN_ACTIONS, "HIL action denylist")

    return {
        "schema": "step5d.autotune-v3/hil-authorization-report-v2",
        "ok": True,
        "authorized": True,
        "authorization_id": authorization_id,
        "thread_id": expected_thread_id,
        "candidate_stage_id": V3_STAGE_ID,
        "scope": SCOPE,
        "identity": readiness["identity"],
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "serial": True,
        "hold_required": True,
        "live_writer_allowed": True,
        "allowed_actions": ALLOWED_ACTIONS,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "next_legal_action": (
            "capture a fresh controller snapshot, then run the serialized "
            "full-production-bridge HOLD gate"
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--expected-thread-id", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = verify_authorization(
            args.authorization,
            expected_thread_id=args.expected_thread_id,
            root=args.root,
        )
    except (AuthorizationError, execution_readiness.ReadinessError) as exc:
        report = {
            "schema": "step5d.autotune-v3/hil-authorization-report-v2",
            "ok": False,
            "authorized": False,
            "blocker": str(exc),
        }
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"step5d_v3_hil_authorization=blocked:{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("step5d_v3_hil_authorization=authorized_hold_only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
