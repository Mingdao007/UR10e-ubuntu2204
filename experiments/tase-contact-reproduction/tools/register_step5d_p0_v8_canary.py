#!/usr/bin/env python3
"""Atomically register one verified P0 v8 phase on the frozen fingerprint."""

from __future__ import annotations

import argparse, fcntl, hashlib, json, os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    args = parser.parse_args()
    summary_path = args.summary.resolve()
    summary = load(summary_path)
    if summary.get("ok") is not True or summary.get("canary_passed") is not True:
        raise SystemExit("refusing: canary verifier summary did not pass")
    phase = float(summary.get("phase_s", 0))
    if phase not in (2.0, 10.0, 60.0):
        raise SystemExit("refusing: canary phase is not canonical")
    binding = summary.get("binding") or {}
    fingerprint = binding.get("composite_fingerprint")
    manifest_path = Path(binding.get("manifest", "")).resolve()
    if not manifest_path.is_file() or sha(manifest_path) != binding.get("manifest_sha256"):
        raise SystemExit("refusing: canary run manifest hash is stale")
    manifest = load(manifest_path)
    canary = manifest.get("p0_v8_canary") or {}
    if float(canary.get("phase_s", 0)) != phase or canary.get("composite_fingerprint") != fingerprint:
        raise SystemExit("refusing: run manifest does not match verifier phase/fingerprint")
    current_path = ROOT / "config/current_stage.json"
    lock_path = ROOT / "config/.p0_v8_canary_ledger.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = load(current_path)
        candidate = current.get("p0_v8_candidate") or {}
        if candidate.get("composite_fingerprint") != fingerprint:
            raise SystemExit("refusing: current P0 v8 fingerprint changed")
        completed = candidate.get("completed_canaries") or []
        required = [] if phase == 2 else [2] if phase == 10 else [2, 10]
        for prior_phase in required:
            rows = [row for row in completed if float(row.get("phase_s", -1)) == prior_phase
                    and row.get("composite_fingerprint") == fingerprint
                    and row.get("canary_passed") is True]
            if len(rows) != 1:
                raise SystemExit(f"refusing: prior {prior_phase}s ledger entry missing")
            artifact = (ROOT / rows[0].get("artifact", "")).resolve()
            if not artifact.is_file() or sha(artifact) != rows[0].get("artifact_sha256"):
                raise SystemExit(f"refusing: prior {prior_phase}s artifact hash is stale")
        relative = str(summary_path.relative_to(ROOT))
        entry = {"phase_s": phase, "composite_fingerprint": fingerprint,
                 "canary_passed": True, "artifact": relative,
                 "artifact_sha256": sha(summary_path)}
        candidate["completed_canaries"] = [row for row in completed
                                             if float(row.get("phase_s", -1)) != phase] + [entry]
        temporary = current_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, current_path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
