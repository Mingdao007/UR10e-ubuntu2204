#!/usr/bin/env python3
"""Mechanical parallel Review v3 runner; never authorizes bridge or motion."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from step5d_review_v3 import canonical_composite


FABLE_LIMIT_RE = re.compile(
    r"(?:quota|rate[ -]?limit|session[ -]?limit|usage[ -]?limit|limit reached)",
    re.IGNORECASE,
)


def fable_limit_returned(text: str) -> bool:
    return FABLE_LIMIT_RE.search(text) is not None


FABLE_LIMIT_RE = re.compile(
    r"(?:quota|rate[ -]?limit|session[ -]?limit|usage[ -]?limit|limit reached)",
    re.IGNORECASE,
)


def fable_limit_returned(text: str) -> bool:
    return FABLE_LIMIT_RE.search(text) is not None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_lane(name: str, command: list[str], output: Path, timeout: float | None,
             requested_model: str, requested_effort: str, composite: str,
             binding_sha256: str) -> dict[str, Any]:
    started = now()
    try:
        env = os.environ.copy()
        env.update({"UR10E_REVIEW_COMPOSITE_FINGERPRINT": composite,
                    "UR10E_REVIEW_BINDING_SHA256": binding_sha256})
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout,
                                   check=False, env=env)
        transcript = completed.stdout + completed.stderr
        if name == "physical_operator_safety" and fable_limit_returned(transcript):
            status = "skipped_unavailable"
        else:
            status = "pass" if completed.returncode == 0 else "unavailable_error" if name == "physical_operator_safety" else "fail"
        if name == "physical_operator_safety" and fable_limit_returned(transcript):
            status = "skipped_unavailable"
    except subprocess.TimeoutExpired as exc:
        transcript = (exc.stdout or "") + (exc.stderr or "")
        status = "timeout"
    output.write_text(transcript, encoding="utf-8")
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads(transcript.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        pass
    actual_effort = metadata.get("actual_effort", "unverified")
    input_verified = (metadata.get("reviewed_composite_fingerprint") == composite
                      and metadata.get("reviewed_binding_sha256") == binding_sha256)
    exact_model_verified = (metadata.get("actual_model") == requested_model
                            and actual_effort == requested_effort and input_verified)
    if status == "pass" and not exact_model_verified:
        status = "model_unverified" if name == "physical_operator_safety" else "fail"
    lane = {"provider": "codex" if name == "control_timing_claim" else "fable5",
            "requested_model": requested_model, "actual_model": metadata.get("actual_model", "unverified"),
            "requested_effort": requested_effort, "actual_effort": actual_effort,
            "effort": actual_effort, "runtime_evidence_sha256": sha(output), "started_at": started,
            "ended_at": now(), "status": status, "findings": metadata.get("findings", []),
            "exact_model_verified": exact_model_verified,
            "runtime_evidence_path": str(output)}
    lane["reviewed_composite_fingerprint"] = metadata.get("reviewed_composite_fingerprint")
    lane["reviewed_binding_sha256"] = metadata.get("reviewed_binding_sha256")
    if name == "physical_operator_safety":
        if status != "pass":
            lane["degraded_transcript"] = {"path": str(output), "sha256": sha(output), "status": status}
    return lane


def fable_preflight(command: list[str], explicit: list[str] | None) -> dict[str, Any]:
    """Bound Fable availability probing without consuming the review timeout."""
    started = now()
    if explicit:
        try:
            completed = subprocess.run(
                explicit, text=True, capture_output=True, timeout=20.0, check=False
            )
            detail = (completed.stdout + completed.stderr)[-4000:]
            if fable_limit_returned(detail):
                status = "skipped_unavailable"
            else:
                status = "available" if completed.returncode == 0 else "unavailable_error"
            if fable_limit_returned(detail):
                status = "skipped_unavailable"
        except subprocess.TimeoutExpired:
            status, detail = "timeout", "Fable preflight exceeded 20 seconds"
    else:
        status = "available" if shutil.which(command[0]) else "unavailable_error"
        detail = f"executable={command[0]}"
    return {"status": status, "started_at": started, "ended_at": now(), "detail": detail}


def reserve_full_review(index_path: Path, composite: str) -> None:
    lock_path = index_path.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index = json.loads(index_path.read_text())
        if int((index.get("full_review_count_by_composite_fingerprint") or {}).get(composite, 0) or 0):
            raise SystemExit("full review already exists for composite fingerprint")
        reservations = index.setdefault("full_review_in_progress_by_composite_fingerprint", {})
        existing = reservations.get(composite) or {}
        existing_pid = int(existing.get("pid", -1)) if isinstance(existing, dict) else -1
        if existing_pid > 0:
            try:
                os.kill(existing_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise SystemExit("full review already running for composite fingerprint")
        reservations[composite] = {"pid": os.getpid(), "started_at": now()}
        temporary = index_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        temporary.replace(index_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--codex-command", nargs="+", required=True)
    parser.add_argument("--fable-command", nargs="+", required=True)
    parser.add_argument("--fable-preflight-command", nargs="+")
    parser.add_argument("--explicit-wait-override", action="store_true")
    args = parser.parse_args()
    binding = json.loads(args.binding.read_text())
    composite = canonical_composite(binding)
    binding_sha256 = sha(args.binding)
    reserve_full_review(args.index, composite)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    timeout = None if args.explicit_wait_override else 300.0
    preflight = fable_preflight(args.fable_command, args.fable_preflight_command)
    specs = {
        "control_timing_claim": (args.codex_command, "gpt-5.6-sol", "xhigh"),
        "physical_operator_safety": (args.fable_command, "claude-fable-5", "xhigh"),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(
                run_lane, name, command, args.output_dir / f"{name}.transcript.txt",
                timeout, model, effort, composite, binding_sha256,
            )
            for name, (command, model, effort) in specs.items()
            if name != "physical_operator_safety" or preflight["status"] == "available"
        }
        lanes = {name: future.result() for name, future in futures.items()}
    if "physical_operator_safety" not in lanes:
        transcript = args.output_dir / "physical_operator_safety.transcript.txt"
        transcript.write_text(preflight["detail"] + "\n", encoding="utf-8")
        lanes["physical_operator_safety"] = {
            "provider": "fable5", "requested_model": "claude-fable-5",
            "actual_model": "unverified", "effort": "xhigh",
            "requested_effort": "xhigh", "actual_effort": "unverified",
            "runtime_evidence_sha256": sha(transcript),
            "runtime_evidence_path": str(transcript),
            "started_at": preflight["started_at"], "ended_at": preflight["ended_at"],
            "status": preflight["status"], "findings": [], "exact_model_verified": False,
            "degraded_transcript": {"path": str(transcript), "sha256": sha(transcript),
                                    "status": preflight["status"]},
        }
    blocking_findings = [
        (lane_name, finding) for lane_name, lane in lanes.items()
        for finding in lane.get("findings", []) if isinstance(finding, dict)
        and finding.get("severity") in {"P0", "P1"}
        and finding.get("status", "open") != "closed"
    ]
    blocking_ids = [str(finding.get("id") or "") for _, finding in blocking_findings]
    if any(not value for value in blocking_ids) or len(set(blocking_ids)) != len(blocking_ids):
        raise SystemExit("blocking Review v3 findings require unique nonempty IDs")
    manifest = {"schema_version": "ur10e_review_manifest_v3", "review_mode": "full",
                "explicit_wait_override": args.explicit_wait_override,
                "explicit_wait_override_excludes_limit_status": True,
                "fable5_preflight": preflight,
                "binding_document_sha256": binding_sha256,
                "composite_binding": binding, "composite_fingerprint": composite,
                "created_at": now(), "lanes": lanes}
    manifest_path = args.output_dir / "review_manifest_v3.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    lock_path = args.index.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index = json.loads(args.index.read_text())
        if int((index.get("full_review_count_by_composite_fingerprint") or {}).get(composite, 0) or 0):
            raise SystemExit("full review already exists for composite fingerprint")
        index.setdefault("review_records", []).append({
            "review_mode": "full", "composite_fingerprint": composite,
            "manifest": str(manifest_path), "manifest_sha256": sha(manifest_path),
            "open_blocking_finding_ids": sorted({
                str(finding.get("id")) for lane in lanes.values()
                for finding in lane.get("findings", [])
                if isinstance(finding, dict) and finding.get("severity") in {"P0", "P1"}
                and finding.get("status", "open") != "closed"
            }),
            "open_blocking_findings": {
                str(finding["id"]): {"lane": lane_name, "severity": finding["severity"]}
                for lane_name, finding in blocking_findings
            },
        })
        index.setdefault("full_review_count_by_composite_fingerprint", {})[composite] = 1
        (index.setdefault("full_review_in_progress_by_composite_fingerprint", {})).pop(composite, None)
        temporary = args.index.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.index)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
