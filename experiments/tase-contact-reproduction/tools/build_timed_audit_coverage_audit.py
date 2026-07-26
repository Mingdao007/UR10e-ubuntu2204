#!/usr/bin/env python3
"""Build a fail-closed timed Opus/subagent coverage audit for the 17h goal.

This is a report-level offline gate. It scans saved handoff artifacts only and
does not invoke Claude, subagents, Gazebo, ROS, or any live bench surface.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any


HANDOFF_ROOT = Path("/home/andy/codex_handoffs")
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
GOAL_START_AT = "2026-06-21T00:56:00+08:00"
EXPECTED_SUBAGENT_LENSES = ("visual-observer", "geometry-frame", "report-claim")

SUBAGENT_RECORD_RE = re.compile(
    r"ur10e-gazebo-hour(?P<hour>\d+)-(?P<lens>visual-observer|geometry-frame|report-claim)-subagent-(?P<kind>prompt|result|unavailable)-"
)
STAMP_RE = re.compile(r"(?P<date>20\d{6})-(?P<time>\d{4,6})(?:-HKT)?")


def normalize_iso_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def rel(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def sorted_files(root: Path, pattern: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(root.rglob(pattern), key=lambda path: str(path))


def parse_key_value_meta(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return data
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def timestamp_from_name(path: Path) -> str | None:
    match = STAMP_RE.search(path.name)
    if not match:
        return None
    date = match.group("date")
    time = match.group("time").ljust(6, "0")
    return f"{date[:4]}-{date[4:6]}-{date[6:8]}T{time[:2]}:{time[2:4]}:{time[4:6]}+08:00"


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def file_nonempty(path: Path | None) -> bool:
    try:
        return bool(path and path.is_file() and path.stat().st_size > 0)
    except OSError:
        return False


def coerce_exit_code(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def opus_record_from_json_meta(path: Path) -> dict[str, Any] | None:
    if "health" in path.name.lower():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = path.name.lower()
    if "opus" not in name and "claude" not in name and payload.get("model") != "opus":
        return None
    response_path = Path(payload["response_path"]) if payload.get("response_path") else None
    prompt_path = Path(payload["prompt_path"]) if payload.get("prompt_path") else None
    stderr_path = Path(payload["stderr_path"]) if payload.get("stderr_path") else None
    exit_code = coerce_exit_code(payload.get("exit_code"))
    status = "complete" if exit_code == "0" and file_nonempty(response_path) else "failed_or_incomplete"
    return {
        "record_type": "json_meta",
        "status": status,
        "exit_code": exit_code,
        "meta_path": rel(path),
        "prompt_path": rel(prompt_path),
        "response_path": rel(response_path),
        "stderr_path": rel(stderr_path) if stderr_path and stderr_path.exists() else None,
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at") or payload.get("ended_at") or timestamp_from_name(path),
    }


def opus_record_from_text_meta(path: Path) -> dict[str, Any] | None:
    if "health" in path.name.lower():
        return None
    name = path.name.lower()
    if "opus" not in name and "claude" not in name:
        return None
    meta = parse_key_value_meta(path)
    if not meta:
        return None
    response_path = Path(meta["response"]) if meta.get("response") else first_existing(
        [
            Path(str(path).replace("-meta-", "-response-")).with_suffix(".md"),
            Path(str(path).replace("-audit-meta-", "-audit-response-")).with_suffix(".md"),
        ]
    )
    prompt_path = Path(meta["prompt"]) if meta.get("prompt") else first_existing(
        [
            Path(str(path).replace("-meta-", "-prompt-")).with_suffix(".md"),
            Path(str(path).replace("-audit-meta-", "-audit-prompt-")).with_suffix(".md"),
        ]
    )
    stderr_path = Path(meta["stderr"]) if meta.get("stderr") else first_existing(
        [
            Path(str(path).replace("-meta-", "-stderr-")).with_suffix(".txt"),
            Path(str(path).replace("-audit-meta-", "-audit-stderr-")).with_suffix(".txt"),
        ]
    )
    exit_code = coerce_exit_code(meta.get("exit_code"))
    status = "complete" if exit_code == "0" and file_nonempty(response_path) else "failed_or_incomplete"
    return {
        "record_type": "text_meta",
        "status": status,
        "exit_code": exit_code,
        "meta_path": rel(path),
        "prompt_path": rel(prompt_path),
        "response_path": rel(response_path),
        "stderr_path": rel(stderr_path) if stderr_path and stderr_path.exists() else None,
        "started_at": meta.get("started_at") or meta.get("start"),
        "finished_at": meta.get("finished_at") or meta.get("ended_at") or meta.get("end") or timestamp_from_name(path),
    }


def opus_record_from_exitcode(path: Path) -> dict[str, Any] | None:
    name = path.name.lower()
    if "health" in name or ("opus" not in name and "claude" not in name):
        return None
    try:
        exit_code = path.read_text(encoding="utf-8").strip()
    except OSError:
        exit_code = None
    stem = path.name.removesuffix(".exitcode.txt")
    stdout_path = path.with_name(stem + ".stdout.txt")
    stderr_path = path.with_name(stem + ".stderr.txt")
    prompt_path = path.with_name(stem.replace("-response-", "-prompt-") + ".md")
    status = "complete" if exit_code == "0" and file_nonempty(stdout_path) else "failed_or_incomplete"
    return {
        "record_type": "exitcode_triplet",
        "status": status,
        "exit_code": exit_code,
        "exitcode_path": rel(path),
        "prompt_path": rel(prompt_path) if prompt_path.is_file() else None,
        "response_path": rel(stdout_path) if stdout_path.is_file() else None,
        "stderr_path": rel(stderr_path) if stderr_path.is_file() else None,
        "started_at": None,
        "finished_at": timestamp_from_name(path),
    }


def discover_opus_records(handoff_root: Path = HANDOFF_ROOT) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted_files(handoff_root, "*meta*.json"):
        record = opus_record_from_json_meta(path)
        if record is None:
            continue
        key = record.get("response_path") or record.get("meta_path")
        if key and key not in seen:
            records.append(record)
            seen.add(key)
    for path in sorted_files(handoff_root, "*meta*.txt"):
        record = opus_record_from_text_meta(path)
        if record is None:
            continue
        key = record.get("response_path") or record.get("meta_path")
        if key and key not in seen:
            records.append(record)
            seen.add(key)
    for path in sorted_files(handoff_root, "*.exitcode.txt"):
        record = opus_record_from_exitcode(path)
        if record is None:
            continue
        key = record.get("response_path") or record.get("exitcode_path")
        if key and key not in seen:
            records.append(record)
            seen.add(key)
    return sorted(records, key=lambda record: record.get("finished_at") or record.get("meta_path") or "")


def expected_subagent_hours(goal_start_at: str | None, generated_at: str | None) -> list[int]:
    if not goal_start_at or not generated_at:
        return []
    start = normalize_iso_datetime(goal_start_at)
    generated = normalize_iso_datetime(generated_at)
    elapsed_hours = int((generated - start).total_seconds() // 3600)
    if elapsed_hours <= 0:
        return []
    return list(range(1, elapsed_hours + 1))


def expected_opus_checkpoint_hours(goal_start_at: str | None, generated_at: str | None) -> list[int]:
    if not goal_start_at or not generated_at:
        return []
    start = normalize_iso_datetime(goal_start_at)
    generated = normalize_iso_datetime(generated_at)
    elapsed_hours = int((generated - start).total_seconds() // 3600)
    if elapsed_hours < 0:
        return []
    return list(range(0, elapsed_hours + 1, 2))


def elapsed_hour_for_record(record: dict[str, Any], goal_start_at: str | None) -> float | None:
    if not goal_start_at or not record.get("finished_at"):
        return None
    try:
        start = normalize_iso_datetime(goal_start_at)
        finished = normalize_iso_datetime(str(record["finished_at"]))
    except (TypeError, ValueError):
        return None
    return (finished - start).total_seconds() / 3600.0


def opus_checkpoint_coverage(
    records: list[dict[str, Any]],
    *,
    goal_start_at: str | None,
    generated_at: str | None,
) -> dict[str, Any]:
    expected = expected_opus_checkpoint_hours(goal_start_at, generated_at)
    complete_records = [record for record in records if record["status"] == "complete"]
    coverage_rows: list[dict[str, Any]] = []
    missing: list[int] = []
    for hour in expected:
        matching: list[dict[str, Any]] = []
        for record in complete_records:
            elapsed = elapsed_hour_for_record(record, goal_start_at)
            if elapsed is None:
                continue
            if hour <= elapsed < hour + 2:
                matching.append(record)
        complete = bool(matching)
        if not complete:
            missing.append(hour)
        coverage_rows.append(
            {
                "checkpoint_hour": hour,
                "complete": complete,
                "record_paths": [record.get("response_path") or record.get("meta_path") for record in matching],
            }
        )
    if not expected:
        return {
            "expected_opus_checkpoint_hours": [],
            "missing_opus_checkpoint_hours": [],
            "opus_checkpoint_rows": [],
            "opus_coverage_complete": bool(complete_records),
        }
    return {
        "expected_opus_checkpoint_hours": expected,
        "missing_opus_checkpoint_hours": missing,
        "opus_checkpoint_rows": coverage_rows,
        "opus_coverage_complete": not missing,
    }


def discover_subagent_triplets(
    handoff_root: Path = HANDOFF_ROOT,
    *,
    expected_hours: list[int] | None = None,
) -> list[dict[str, Any]]:
    hours: dict[str, dict[str, Any]] = {}
    for path in sorted_files(handoff_root, "ur10e-gazebo-hour*-subagent-*.md"):
        match = SUBAGENT_RECORD_RE.search(path.name)
        if not match:
            continue
        hour = match.group("hour")
        lens = match.group("lens")
        kind = match.group("kind")
        item = hours.setdefault(
            hour,
            {
                "hour": int(hour),
                "prompt_lenses": [],
                "result_lenses": [],
                "unavailable_lenses": [],
                "prompt_paths": {},
                "result_paths": {},
                "unavailable_paths": {},
                "record_observed": True,
            },
        )
        item["record_observed"] = True
        key = f"{kind}_paths"
        item[key][lens] = rel(path)
        lens_key = f"{kind}_lenses"
        if lens not in item[lens_key]:
            item[lens_key].append(lens)

    for hour in expected_hours or []:
        hours.setdefault(
            str(hour),
            {
                "hour": hour,
                "prompt_lenses": [],
                "result_lenses": [],
                "unavailable_lenses": [],
                "prompt_paths": {},
                "result_paths": {},
                "unavailable_paths": {},
                "record_observed": False,
            },
        )

    triplets: list[dict[str, Any]] = []
    expected_lenses = set(EXPECTED_SUBAGENT_LENSES)
    for hour in sorted(hours, key=lambda value: int(value)):
        item = hours[hour]
        prompt_lenses = set(item["prompt_lenses"])
        result_lenses = set(item["result_lenses"])
        unavailable_lenses = set(item["unavailable_lenses"])
        result_or_unavailable = result_lenses | unavailable_lenses
        item["prompt_lenses"] = sorted(prompt_lenses)
        item["result_lenses"] = sorted(result_lenses)
        item["unavailable_lenses"] = sorted(unavailable_lenses)
        item["missing_prompt_lenses"] = sorted(expected_lenses - prompt_lenses)
        item["missing_result_lenses"] = sorted(expected_lenses - result_lenses)
        item["missing_result_or_unavailable_lenses"] = sorted(expected_lenses - result_or_unavailable)
        item["triplet_prompt_complete"] = not item["missing_prompt_lenses"]
        item["triplet_result_complete"] = not item["missing_result_lenses"]
        item["triplet_result_or_unavailable_recorded"] = not item["missing_result_or_unavailable_lenses"]
        if item["triplet_result_complete"]:
            item["status"] = "complete"
        elif item["triplet_result_or_unavailable_recorded"]:
            item["status"] = "unavailable_recorded"
        else:
            item["status"] = "incomplete"
        triplets.append(item)
    return triplets


def timed_audit_coverage_summary(
    handoff_root: Path = HANDOFF_ROOT,
    *,
    generated_at: str | None = None,
    goal_start_at: str | None = None,
) -> dict[str, Any]:
    expected_hours = expected_subagent_hours(goal_start_at, generated_at)
    triplets = discover_subagent_triplets(handoff_root, expected_hours=expected_hours)
    observed_hours = [item["hour"] for item in triplets if item.get("record_observed")]
    observed_set = set(observed_hours)
    if expected_hours:
        missing_sequence_hours = [hour for hour in expected_hours if hour not in observed_set]
    elif observed_hours:
        missing_sequence_hours = [
            hour for hour in range(min(observed_hours), max(observed_hours) + 1) if hour not in observed_set
        ]
    else:
        missing_sequence_hours = []

    incomplete_hours = [item["hour"] for item in triplets if not item["triplet_result_complete"]]
    unavailable_hours = [item["hour"] for item in triplets if item["unavailable_lenses"]]
    prompt_only_hours = [
        item["hour"]
        for item in triplets
        if item["triplet_prompt_complete"] and not item["triplet_result_or_unavailable_recorded"]
    ]

    opus_records = discover_opus_records(handoff_root)
    opus_coverage = opus_checkpoint_coverage(
        opus_records,
        goal_start_at=goal_start_at,
        generated_at=generated_at,
    )
    latest_opus_record = opus_records[-1] if opus_records else {
        "status": "missing",
        "exit_code": None,
        "meta_path": None,
        "exitcode_path": None,
        "prompt_path": None,
        "response_path": None,
        "stderr_path": None,
        "finished_at": None,
    }

    subagent_coverage_complete = bool(triplets) and not incomplete_hours and not missing_sequence_hours and not unavailable_hours
    opus_complete = bool(opus_records) and opus_coverage["opus_coverage_complete"]
    full_ready = subagent_coverage_complete and opus_complete

    unresolved: list[str] = []
    if not opus_complete:
        unresolved.append("Opus advisory checkpoint coverage missing, stale, or nonzero")
    if incomplete_hours:
        unresolved.append("hourly subagent triplet results incomplete")
    if missing_sequence_hours:
        unresolved.append("hourly subagent triplet sequence has gaps")
    if unavailable_hours:
        unresolved.append("hourly subagent triplet has unavailable records")

    return {
        "full_acceptance_timed_audit_ready": full_ready,
        "claim_tier": "visual_only",
        "goal_lineage": GOAL_LINEAGE,
        "goal_start_at": goal_start_at,
        "generated_at": generated_at,
        "handoff_root": rel(handoff_root),
        "expected_subagent_triplet_hours": expected_hours,
        "hourly_subagent_triplets": triplets,
        "complete_subagent_triplet_count": sum(1 for item in triplets if item["triplet_result_complete"]),
        "incomplete_subagent_triplet_hours": incomplete_hours,
        "missing_subagent_triplet_sequence_hours": missing_sequence_hours,
        "missing_expected_subagent_triplet_hours": [hour for hour in expected_hours if hour not in observed_set],
        "prompt_only_subagent_triplet_hours": prompt_only_hours,
        "unavailable_subagent_triplet_hours": unavailable_hours,
        "subagent_coverage_complete": subagent_coverage_complete,
        "opus_records": opus_records,
        "complete_opus_record_count": sum(1 for record in opus_records if record["status"] == "complete"),
        "latest_opus_record": latest_opus_record,
        **opus_coverage,
        "unresolved_p0_p1_findings": unresolved,
        "blocker": "Timed Opus and hourly subagent coverage must be verified before any full acceptance claim.",
    }


def build_audit(
    *,
    generated_at: str | None = None,
    goal_start_at: str = GOAL_START_AT,
    handoff_root: Path = HANDOFF_ROOT,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schema": "ur10e_timed_audit_coverage_audit_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_report_level_timed_audit_coverage",
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "timed_audit_coverage": timed_audit_coverage_summary(
            handoff_root=handoff_root,
            generated_at=generated,
            goal_start_at=goal_start_at,
        ),
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    goal_start_at: str = GOAL_START_AT,
    handoff_root: Path = HANDOFF_ROOT,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "timed_audit_coverage_audit.json"
    payload = build_audit(generated_at=generated_at, goal_start_at=goal_start_at, handoff_root=handoff_root)
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--goal-start-at", default=GOAL_START_AT)
    parser.add_argument("--handoff-root", type=Path, default=HANDOFF_ROOT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        write_audit(
            args.output_dir,
            generated_at=args.generated_at,
            goal_start_at=args.goal_start_at,
            handoff_root=args.handoff_root,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
