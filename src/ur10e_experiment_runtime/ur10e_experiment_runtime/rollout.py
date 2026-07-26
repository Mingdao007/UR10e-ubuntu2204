"""Streaming, content-sanitizing extraction of Codex rollout timelines."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping


class RolloutFormatError(ValueError):
    """Raised when a rollout cannot be used as deterministic evidence."""


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_SAMPLING_TYPES = frozenset(
    {"sampling_round", "model_sampling_completed", "token_count"}
)
_TOOL_CALL_TYPES = frozenset(
    {"function_call", "custom_tool_call", "local_shell_call", "tool_call"}
)
_TOOL_RESULT_TYPES = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "local_shell_call_output",
        "tool_result",
    }
)
_COMPACTION_TYPES = frozenset(
    {"compacted", "context_compacted", "context_compaction", "compaction"}
)
_ERROR_TYPES = frozenset(
    {"error", "tool_error", "turn_error", "task_failed", "model_error"}
)


class _StrictLineJSONError(ValueError):
    """Internal signal for ambiguous JSONL records."""


def _reject_duplicate_json_keys(
    pairs: Iterable[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictLineJSONError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise _StrictLineJSONError(f"non-finite number {value!r}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _StrictLineJSONError(f"non-finite number {value!r}")
    return parsed


def _strict_rollout_record(raw_line: bytes, line_number: int) -> dict[str, Any]:
    try:
        text = raw_line.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RolloutFormatError(
            f"invalid rollout JSON at line {line_number}: input is not UTF-8"
        ) from exc
    try:
        record = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
            parse_float=_parse_finite_json_float,
        )
    except (_StrictLineJSONError, json.JSONDecodeError) as exc:
        raise RolloutFormatError(
            f"invalid rollout JSON at line {line_number}: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise RolloutFormatError(
            f"rollout line {line_number} must be a JSON object"
        )
    return record


def _open_regular_nofollow(path: Path) -> BinaryIO:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RolloutFormatError("platform cannot enforce O_NOFOLLOW")
    flags = (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise RolloutFormatError(
            "rollout source must be a no-follow regular file"
        ) from exc
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise RolloutFormatError(
                "rollout source must be a no-follow regular file"
            )
        stream = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise
    return stream


def _safe_name(value: object | None) -> str | None:
    if value is None:
        return None
    sanitized = _SAFE_NAME_RE.sub("_", str(value)).strip("_")[:128]
    return sanitized or None


def _event_types(record: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    record_type = str(record.get("type", "unknown")).strip().lower()
    payload = record.get("payload", {})
    if not isinstance(payload, Mapping):
        payload = {}
    payload_type = str(payload.get("type", record_type)).strip().lower()
    return record_type, payload_type, payload


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _timeline_event(
    sequence: int,
    record: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    record_type, payload_type, payload = _event_types(record)
    event_kind: str | None = None
    name: str | None = None
    if payload_type in _SAMPLING_TYPES or record_type in _SAMPLING_TYPES:
        event_kind = "sampling_round"
    elif payload_type in _TOOL_CALL_TYPES or record_type in _TOOL_CALL_TYPES:
        event_kind = "tool_call"
        name = _safe_name(payload.get("name") or record.get("name"))
    elif payload_type in _TOOL_RESULT_TYPES or record_type in _TOOL_RESULT_TYPES:
        event_kind = "tool_result"
        name = _safe_name(payload.get("name") or record.get("name"))
    elif payload_type in _COMPACTION_TYPES or record_type in _COMPACTION_TYPES:
        event_kind = "compaction"
    elif payload_type in _ERROR_TYPES or record_type in _ERROR_TYPES:
        event_kind = "error"
        name = _safe_name(payload.get("name") or payload.get("code"))
    elif payload_type in {"task_started", "task_complete", "task_completed"}:
        event_kind = payload_type

    if event_kind is None:
        return None, None
    item: dict[str, Any] = {
        "sequence": sequence,
        "kind": event_kind,
    }
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str):
        item["timestamp"] = timestamp
    if name:
        item["name"] = name
    return event_kind, item


def _extract_stream(
    stream: BinaryIO,
    *,
    max_timeline_events: int,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    timeline: list[dict[str, Any]] = []
    timeline_candidates = 0
    counts = {
        "records": 0,
        "sampling_rounds": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "compactions": 0,
        "errors": 0,
    }
    source_bytes = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    first_timestamp_text: str | None = None
    last_timestamp_text: str | None = None

    for line_number, raw_line in enumerate(stream, start=1):
        digest.update(raw_line)
        source_bytes += len(raw_line)
        if not raw_line.strip():
            continue
        record = _strict_rollout_record(raw_line, line_number)

        counts["records"] += 1
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is not None:
            if first_timestamp is None or timestamp < first_timestamp:
                first_timestamp = timestamp
                first_timestamp_text = str(record["timestamp"])
            if last_timestamp is None or timestamp > last_timestamp:
                last_timestamp = timestamp
                last_timestamp_text = str(record["timestamp"])

        kind, item = _timeline_event(counts["records"], record)
        if kind == "sampling_round":
            counts["sampling_rounds"] += 1
        elif kind == "tool_call":
            counts["tool_calls"] += 1
        elif kind == "tool_result":
            counts["tool_results"] += 1
        elif kind == "compaction":
            counts["compactions"] += 1
        elif kind == "error":
            counts["errors"] += 1
        if item is not None:
            timeline_candidates += 1
            if len(timeline) < max_timeline_events:
                timeline.append(item)

    duration_s: float | None = None
    if first_timestamp is not None and last_timestamp is not None:
        duration_s = max(0.0, (last_timestamp - first_timestamp).total_seconds())
    timeline_truncated = timeline_candidates > len(timeline)
    return {
        "schema": "ur10e_experiment_runtime.rollout_timeline/v1",
        "source_sha256": digest.hexdigest(),
        "source_bytes": source_bytes,
        "counts": counts,
        "time": {
            "start": first_timestamp_text,
            "end": last_timestamp_text,
            "duration_s": duration_s,
        },
        "timeline": timeline,
        "timeline_limit": max_timeline_events,
        "timeline_truncated": timeline_truncated,
        "content_policy": "metadata_only_no_prompt_arguments_or_outputs",
    }


def extract_rollout_timeline(
    source: str | Path,
    *,
    max_timeline_events: int = 2_000,
) -> dict[str, Any]:
    """Stream a JSONL rollout into a deterministic, sanitized short timeline.

    The result never includes message content, tool arguments, tool outputs,
    reasoning, summaries, absolute input paths, or per-run random identifiers.
    """

    if (
        isinstance(max_timeline_events, bool)
        or not isinstance(max_timeline_events, int)
        or max_timeline_events < 1
    ):
        raise RolloutFormatError("max_timeline_events must be a positive integer")
    path = Path(source)
    with _open_regular_nofollow(path) as stream:
        return _extract_stream(stream, max_timeline_events=max_timeline_events)
