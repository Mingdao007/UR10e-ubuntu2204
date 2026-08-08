#!/usr/bin/env python3
"""Offline analyzer: Phase-5 cutover metrics from a step5d r008 B3 run directory.

Reads seal daemon stage timings, pre-HOME seal joins, reason=43 counts, and
Phase-5 artifact checks (R008RAW2 magic, receipt version, empty JSON samples).

Works on historical (pre-Phase-5) runs — reports legacy vs phase5 codec mix.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

MAGIC_V2 = b"R008RAW2"
PHASE5_RECEIPT_VERSION = "r008-sealed-binary-raw-v1"
PHASE5_RAW_CODEC = "r008raw_v2"
PHASE5_CAMPAIGN_PREFIX = "b461ed52"

_SEAL_SUMMARY_RE = re.compile(r"R008_SEAL_STAGE:summary=(\{.*\})")
_SEAL_STAGE_RE = re.compile(r"R008_SEAL_STAGE:(\w+)=([\d.]+)s")
_PREHOME_JOIN_RE = re.compile(r"R008_PREHOME_SEAL_JOIN:waited_s=([\d.]+)")
_REASON43_RE = re.compile(
    r"(?:reason_code[=:]\s*43|reason=43|FRESHNESS_FAILURE|reason 43)",
    re.IGNORECASE,
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_seal_daemon(log_path: Path) -> dict[str, Any]:
    text = _read_text(log_path)
    summaries: list[dict[str, float]] = []
    for match in _SEAL_SUMMARY_RE.finditer(text):
        try:
            summaries.append({k: float(v) for k, v in json.loads(match.group(1)).items()})
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    if not summaries:
        # Fallback: stitch per-line stage markers into pseudo-summaries.
        current: dict[str, float] = {}
        for line in text.splitlines():
            m = _SEAL_STAGE_RE.search(line)
            if not m:
                continue
            current[m.group(1)] = float(m.group(2))
            if m.group(1) == "seal_total":
                summaries.append(dict(current))
                current = {}

    def _stats(key: str) -> dict[str, float | int]:
        vals = [row[key] for row in summaries if key in row]
        if not vals:
            return {"count": 0}
        return {
            "count": len(vals),
            "min_s": min(vals),
            "max_s": max(vals),
            "mean_s": statistics.fmean(vals),
            "p50_s": statistics.median(vals),
        }

    return {
        "seal_events": len(summaries),
        "seal_total": _stats("seal_total"),
        "r006_append": _stats("r006_append"),
        "ledger_append": _stats("ledger_append"),
        "pickle_load": _stats("pickle_load"),
    }


def _parse_host_log(log_path: Path) -> dict[str, Any]:
    text = _read_text(log_path)
    prehome_waits = [float(m.group(1)) for m in _PREHOME_JOIN_RE.finditer(text)]
    reason43 = len(_REASON43_RE.findall(text))
    return {
        "prehome_seal_join": {
            "count": len(prehome_waits),
            "min_s": min(prehome_waits) if prehome_waits else None,
            "max_s": max(prehome_waits) if prehome_waits else None,
            "mean_s": statistics.fmean(prehome_waits) if prehome_waits else None,
            "p50_s": statistics.median(prehome_waits) if prehome_waits else None,
        },
        "reason_43_hits": reason43,
    }


def _scan_raw_artifacts(run_dir: Path) -> dict[str, Any]:
    r008raw_files = sorted(run_dir.glob("**/*.r008raw"))
    magic_v2 = 0
    magic_other = 0
    for path in r008raw_files:
        try:
            head = path.read_bytes()[:8]
        except OSError:
            continue
        if head.startswith(MAGIC_V2):
            magic_v2 += 1
        else:
            magic_other += 1

    slim_json = 0
    fat_json = 0
    for prefix in ("raw_force_evidence", "r006_raw_objectives"):
        for path in (run_dir / prefix).glob("*.json") if (run_dir / prefix).is_dir() else ():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            samples = doc.get("samples")
            if isinstance(samples, list):
                if samples == []:
                    slim_json += 1
                else:
                    fat_json += 1

    return {
        "r008raw_files": len(r008raw_files),
        "r008raw_magic_v2": magic_v2,
        "r008raw_other_magic": magic_other,
        "slim_json_artifacts_empty_samples": slim_json,
        "legacy_json_artifacts_with_samples": fat_json,
    }


def _campaign_fingerprint(run_dir: Path) -> str | None:
    for name in ("launch_context.json", "controller_receipt.json"):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fp = doc.get("campaign_fingerprint")
        if isinstance(fp, str):
            return fp
    ledger = run_dir / "r006-observations.jsonl"
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines()[:3]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            fp = row.get("campaign_fingerprint")
            if isinstance(fp, str):
                return fp
    return None


def analyze_run_dir(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"run directory missing: {run_dir}")

    fp = _campaign_fingerprint(run_dir)
    artifacts = _scan_raw_artifacts(run_dir)
    seal_log = run_dir / "seal_daemon.stderr.log"
    host_log = run_dir / "host.log"

    phase5_fp = bool(fp and fp.startswith(PHASE5_CAMPAIGN_PREFIX))
    # A healthy Phase-5 run always has raw2 files *and* their slim JSON
    # sidecars side by side (binary_seal.py writes both by design) -- that
    # coexistence is not a "mix". A real mix is legacy fat-JSON (pre-Phase-5
    # codec, samples embedded inline) coexisting with raw2 in the same run,
    # e.g. a mid-campaign cutover. 2026-08-07: the old `A or B` definition
    # here was always true in a healthy run and was never actually a mix
    # check -- fixed to the real definition below.
    phase5_artifact_mix = (
        artifacts["legacy_json_artifacts_with_samples"] > 0 and artifacts["r008raw_magic_v2"] > 0
    )

    report: dict[str, Any] = {
        "run_dir": str(run_dir),
        "campaign_fingerprint": fp,
        "phase5_fingerprint_expected_prefix": PHASE5_CAMPAIGN_PREFIX,
        "phase5_fingerprint_match": phase5_fp,
        "seal_daemon": _parse_seal_daemon(seal_log) if seal_log.is_file() else {"missing": True},
        "host": _parse_host_log(host_log) if host_log.is_file() else {"missing": True},
        "artifacts": artifacts,
        "phase5_success_checks": {
            "expected_receipt_version": PHASE5_RECEIPT_VERSION,
            "expected_raw_codec": PHASE5_RAW_CODEC,
            "r008raw2_present": artifacts["r008raw_magic_v2"] > 0,
            "slim_json_empty_samples": artifacts["slim_json_artifacts_empty_samples"] > 0,
            "legacy_fat_json_only": (
                artifacts["legacy_json_artifacts_with_samples"] > 0
                and artifacts["slim_json_artifacts_empty_samples"] == 0
                and artifacts["r008raw_magic_v2"] == 0
            ),
            "phase5_artifact_mix_detected": phase5_artifact_mix,
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Formal run directory")
    parser.add_argument("--json", action="store_true", help="Emit compact JSON only")
    args = parser.parse_args(argv)

    report = analyze_run_dir(args.run_dir)
    if args.json:
        print(json.dumps(report, sort_keys=True))
        return 0

    print(f"run_dir: {report['run_dir']}")
    print(f"campaign_fingerprint: {report['campaign_fingerprint']}")
    print(f"phase5_fp (prefix {PHASE5_CAMPAIGN_PREFIX}): {report['phase5_fingerprint_match']}")
    print()

    sd = report["seal_daemon"]
    if sd.get("missing"):
        print("seal_daemon.stderr.log: (missing)")
    else:
        print(f"seal events: {sd['seal_events']}")
        for key in ("seal_total", "r006_append", "ledger_append"):
            stats = sd.get(key, {})
            if stats.get("count"):
                print(
                    f"  {key}: n={stats['count']} "
                    f"p50={stats['p50_s']:.3f}s mean={stats['mean_s']:.3f}s "
                    f"max={stats['max_s']:.3f}s"
                )

    host = report["host"]
    if host.get("missing"):
        print("host.log: (missing)")
    else:
        ph = host["prehome_seal_join"]
        note = " (0 = HOME never had to wait -- healthy, not a gap)" if ph["count"] == 0 else ""
        print(
            f"R008_PREHOME_SEAL_JOIN: count={ph['count']} "
            f"p50={ph['p50_s']}s mean={ph['mean_s']}s max={ph['max_s']}s{note}"
        )
        print(f"reason=43 hits: {host['reason_43_hits']}")

    art = report["artifacts"]
    print()
    print(
        f"artifacts: r008raw={art['r008raw_files']} (magic_v2={art['r008raw_magic_v2']}) "
        f"slim_json={art['slim_json_artifacts_empty_samples']} "
        f"legacy_fat_json={art['legacy_json_artifacts_with_samples']}"
    )
    checks = report["phase5_success_checks"]
    print(
        f"phase5: raw2={checks['r008raw2_present']} "
        f"slim_empty={checks['slim_json_empty_samples']} "
        f"legacy_only={checks['legacy_fat_json_only']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
