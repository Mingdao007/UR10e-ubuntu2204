#!/usr/bin/env python3
"""Monotonic fencing authority for the one canonical Step5d bridge owner."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from step5d_autotune_v3.governance import read_proc_starttime_ticks
from step5d_autotune_v3.state import atomic_json


SCHEMA = "step5d.bridge/owner-authority-v1"
STATE_FILE = "owner-authority.json"
LOCK_FILE = ".owner-authority.lock"


class BridgeAuthorityError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise BridgeAuthorityError("bridge owner authority path is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeAuthorityError(f"bridge owner authority is invalid: {exc}") from exc
    required = {
        "schema",
        "sequence",
        "state",
        "attempt_id",
        "owner",
        "activated_at_unix_ns",
        "revoked_at_unix_ns",
        "reason",
    }
    owner = payload.get("owner") if isinstance(payload, Mapping) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("schema") != SCHEMA
        or isinstance(payload.get("sequence"), bool)
        or not isinstance(payload.get("sequence"), int)
        or payload["sequence"] < 1
        or payload.get("state") not in {"ACTIVE", "REVOKED"}
        or not isinstance(payload.get("attempt_id"), str)
        or not payload["attempt_id"]
        or not isinstance(owner, Mapping)
        or set(owner) != {"pid", "starttime_ticks"}
        or not isinstance(owner["pid"], int)
        or not isinstance(owner["starttime_ticks"], int)
    ):
        raise BridgeAuthorityError("bridge owner authority fields differ")
    if payload["state"] == "ACTIVE":
        if payload["revoked_at_unix_ns"] is not None or payload["reason"] is not None:
            raise BridgeAuthorityError("active bridge authority contains revocation state")
    elif (
        not isinstance(payload["revoked_at_unix_ns"], int)
        or payload["reason"] not in {"completed", "cancelled", "failed"}
    ):
        raise BridgeAuthorityError("revoked bridge authority is incomplete")
    return payload


def load_current(authority_root: Path) -> dict[str, Any] | None:
    return _load(authority_root.expanduser().absolute() / STATE_FILE)


def _caller_owner(pid: int, starttime_ticks: int) -> None:
    if os.getppid() != pid or read_proc_starttime_ticks(pid) != starttime_ticks:
        raise BridgeAuthorityError("bridge authority caller is not the bound shell owner")


def begin(
    authority_root: Path,
    *,
    attempt_id: str,
    owner_pid: int,
    owner_starttime_ticks: int,
) -> dict[str, Any]:
    _caller_owner(owner_pid, owner_starttime_ticks)
    root = authority_root.expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / LOCK_FILE).open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = _load(root / STATE_FILE)
        if current is not None and current["state"] == "ACTIVE":
            if (
                current["attempt_id"] == attempt_id
                and current["owner"]
                == {"pid": owner_pid, "starttime_ticks": owner_starttime_ticks}
            ):
                return current
            if read_proc_starttime_ticks(current["owner"]["pid"]) == current["owner"][
                "starttime_ticks"
            ]:
                raise BridgeAuthorityError("another canonical bridge owner is active")
        payload = {
            "schema": SCHEMA,
            "sequence": 1 if current is None else current["sequence"] + 1,
            "state": "ACTIVE",
            "attempt_id": attempt_id,
            "owner": {"pid": owner_pid, "starttime_ticks": owner_starttime_ticks},
            "activated_at_unix_ns": time.time_ns(),
            "revoked_at_unix_ns": None,
            "reason": None,
        }
        atomic_json(root / STATE_FILE, payload)
        return payload


def revoke(
    authority_root: Path,
    *,
    attempt_id: str,
    owner_pid: int,
    owner_starttime_ticks: int,
    reason: str,
) -> dict[str, Any]:
    _caller_owner(owner_pid, owner_starttime_ticks)
    if reason not in {"completed", "cancelled", "failed"}:
        raise BridgeAuthorityError("bridge authority revocation reason differs")
    root = authority_root.expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / LOCK_FILE).open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = _load(root / STATE_FILE)
        if current is None:
            raise BridgeAuthorityError("bridge owner authority is missing")
        if current["state"] == "REVOKED":
            if current["attempt_id"] == attempt_id:
                return current
            raise BridgeAuthorityError("bridge authority was revoked by another attempt")
        if (
            current["attempt_id"] != attempt_id
            or current["owner"]
            != {"pid": owner_pid, "starttime_ticks": owner_starttime_ticks}
        ):
            raise BridgeAuthorityError("bridge authority owner binding differs")
        payload = {
            **current,
            "sequence": current["sequence"] + 1,
            "state": "REVOKED",
            "revoked_at_unix_ns": time.time_ns(),
            "reason": reason,
        }
        atomic_json(root / STATE_FILE, payload)
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("begin", "revoke"))
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-starttime", type=int, required=True)
    parser.add_argument("--reason", choices=("completed", "cancelled", "failed"))
    args = parser.parse_args(argv)
    try:
        if args.action == "begin":
            if args.reason is not None:
                parser.error("begin does not accept --reason")
            payload = begin(
                args.authority_root,
                attempt_id=args.attempt_id,
                owner_pid=args.owner_pid,
                owner_starttime_ticks=args.owner_starttime,
            )
        else:
            if args.reason is None:
                parser.error("revoke requires --reason")
            payload = revoke(
                args.authority_root,
                attempt_id=args.attempt_id,
                owner_pid=args.owner_pid,
                owner_starttime_ticks=args.owner_starttime,
                reason=args.reason,
            )
    except (OSError, ValueError, BridgeAuthorityError) as exc:
        print(f"bridge authority blocked: {exc}", file=sys.stderr)
        return 2
    print(payload["sequence"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
