#!/usr/bin/env python3
"""Mechanical parallel Review v3 runner; never authorizes bridge or motion."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from step5d_review_v3 import canonical_composite


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_lane(name: str, command: list[str], output: Path, timeout: float | None,
             requested_model: str, effort: str) -> dict[str, Any]:
    started = now()
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        transcript = completed.stdout + completed.stderr
        status = "pass" if completed.returncode == 0 else "unavailable_error" if name == "physical_operator_safety" else "fail"
    except subprocess.TimeoutExpired as exc:
        transcript = (exc.stdout or "") + (exc.stderr or "")
        status = "timeout"
    output.write_text(transcript, encoding="utf-8")
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads(transcript.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        pass
    lane = {"provider": "codex" if name == "control_timing_claim" else "fable5",
            "requested_model": requested_model, "actual_model": metadata.get("actual_model", "unverified"),
            "effort": effort, "runtime_evidence_sha256": sha(output), "started_at": started,
            "ended_at": now(), "status": status, "findings": metadata.get("findings", [])}
    if name == "physical_operator_safety":
        lane["exact_model_verified"] = lane["actual_model"] == requested_model
        if status != "pass":
            lane["degraded_transcript"] = {"path": str(output), "sha256": sha(output), "status": status}
    return lane


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--codex-command", nargs="+", required=True)
    parser.add_argument("--fable-command", nargs="+", required=True)
    parser.add_argument("--explicit-wait-override", action="store_true")
    args = parser.parse_args()
    binding = json.loads(args.binding.read_text())
    composite = canonical_composite(binding)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    timeout = None if args.explicit_wait_override else 300.0
    specs = {
        "control_timing_claim": (args.codex_command, "gpt-5.6-sol", "xhigh"),
        "physical_operator_safety": (args.fable_command, "claude-fable-5", "xhigh"),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {name: pool.submit(run_lane, name, command, args.output_dir / f"{name}.transcript.txt",
                                     timeout, model, effort)
                   for name, (command, model, effort) in specs.items()}
        lanes = {name: future.result() for name, future in futures.items()}
    manifest = {"schema_version": "ur10e_review_manifest_v3", "review_mode": "full",
                "explicit_wait_override": args.explicit_wait_override,
                "composite_binding": binding, "composite_fingerprint": composite,
                "created_at": now(), "lanes": lanes}
    manifest_path = args.output_dir / "review_manifest_v3.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    index = json.loads(args.index.read_text())
    if int((index.get("full_review_count_by_composite_fingerprint") or {}).get(composite, 0) or 0):
        raise SystemExit("full review already exists for composite fingerprint")
    index.setdefault("review_records", []).append({"review_mode": "full", "composite_fingerprint": composite,
                                                    "manifest": str(manifest_path), "manifest_sha256": sha(manifest_path)})
    index.setdefault("full_review_count_by_composite_fingerprint", {})[composite] = 1
    temporary = args.index.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.index)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
