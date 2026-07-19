from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PACKAGE_SOURCE = REPO / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(PACKAGE_SOURCE))

from ur10e_experiment_runtime.rollout import (  # noqa: E402
    RolloutFormatError,
    extract_rollout_timeline,
)
from ur10e_experiment_runtime import rollout as rollout_module  # noqa: E402


def rollout_bytes() -> bytes:
    records = [
        {
            "timestamp": "2026-07-19T00:00:00+00:00",
            "type": "event_msg",
            "payload": {"type": "task_started", "prompt": "PRIVATE_PROMPT"},
        },
        {
            "timestamp": "2026-07-19T00:00:01+00:00",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": "TOKEN=PRIVATE_ARGUMENT",
                "call_id": "volatile-call-id",
            },
        },
        {
            "timestamp": "2026-07-19T00:00:02+00:00",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "PRIVATE_TOOL_OUTPUT",
            },
        },
        {
            "timestamp": "2026-07-19T00:00:03+00:00",
            "type": "event_msg",
            "payload": {"type": "token_count", "private": "PRIVATE_TOKENS"},
        },
        {
            "timestamp": "2026-07-19T00:00:04+00:00",
            "type": "compacted",
            "payload": {"type": "context_compacted", "summary": "PRIVATE_SUMMARY"},
        },
        {
            "timestamp": "2026-07-19T00:00:05+00:00",
            "type": "event_msg",
            "payload": {"type": "task_completed", "answer": "PRIVATE_ANSWER"},
        },
    ]
    return b"".join(
        json.dumps(record, sort_keys=True).encode("utf-8") + b"\n" for record in records
    )


def test_streaming_extractor_counts_and_never_republishes_content(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    raw = rollout_bytes()
    source.write_bytes(raw)

    summary = extract_rollout_timeline(source)

    assert summary["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert summary["counts"] == {
        "records": 6,
        "sampling_rounds": 1,
        "tool_calls": 1,
        "tool_results": 1,
        "compactions": 1,
        "errors": 0,
    }
    assert summary["time"]["duration_s"] == 5.0
    serialized = json.dumps(summary, sort_keys=True)
    for secret in (
        "PRIVATE_PROMPT",
        "PRIVATE_ARGUMENT",
        "PRIVATE_TOOL_OUTPUT",
        "PRIVATE_TOKENS",
        "PRIVATE_SUMMARY",
        "PRIVATE_ANSWER",
        "volatile-call-id",
    ):
        assert secret not in serialized
    assert any(
        item == {
            "sequence": 2,
            "kind": "tool_call",
            "timestamp": "2026-07-19T00:00:01+00:00",
            "name": "exec_command",
        }
        for item in summary["timeline"]
    )


def test_summary_depends_on_content_not_absolute_source_path(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    nested = tmp_path / "elsewhere"
    nested.mkdir()
    second = nested / "renamed.jsonl"
    first.write_bytes(rollout_bytes())
    second.write_bytes(rollout_bytes())

    assert extract_rollout_timeline(first) == extract_rollout_timeline(second)


def test_timeline_is_bounded_without_changing_complete_counts(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(rollout_bytes())

    summary = extract_rollout_timeline(source, max_timeline_events=2)

    assert len(summary["timeline"]) == 2
    assert summary["timeline_truncated"] is True
    assert summary["counts"]["tool_calls"] == 1
    assert summary["counts"]["compactions"] == 1


def test_malformed_or_symlinked_source_fails_closed(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"type": "event_msg"}\nnot-json\n')
    with pytest.raises(RolloutFormatError, match="line 2"):
        extract_rollout_timeline(malformed)

    link = tmp_path / "link.jsonl"
    link.symlink_to(malformed)
    with pytest.raises(RolloutFormatError, match="no-follow regular file"):
        extract_rollout_timeline(link)


@pytest.mark.parametrize(
    ("raw_record", "expected_error"),
    [
        (b'{"type":"first","type":"second"}\n', "duplicate key"),
        (b'{"payload":{"objective":NaN}}\n', "non-finite number"),
        (b'{"payload":{"objective":Infinity}}\n', "non-finite number"),
        (b'{"payload":{"objective":-Infinity}}\n', "non-finite number"),
        (b'{"payload":{"objective":1e999}}\n', "non-finite number"),
        (b'{"payload":{"message":"\xff"}}\n', "not UTF-8"),
    ],
)
def test_rollout_rejects_ambiguous_jsonl_records(
    tmp_path: Path,
    raw_record: bytes,
    expected_error: str,
) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(raw_record)

    with pytest.raises(RolloutFormatError, match=expected_error):
        extract_rollout_timeline(source)


def test_rollout_open_uses_nofollow_and_fstat_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(rollout_bytes())
    real_open = rollout_module.os.open
    observed_flags: list[int] = []

    def recording_open(path: object, flags: int) -> int:
        observed_flags.append(flags)
        return real_open(path, flags)

    monkeypatch.setattr(rollout_module.os, "open", recording_open)
    extract_rollout_timeline(source)

    assert len(observed_flags) == 1
    assert observed_flags[0] & os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        assert observed_flags[0] & os.O_CLOEXEC
    with pytest.raises(RolloutFormatError, match="no-follow regular file"):
        extract_rollout_timeline(tmp_path)

    fifo = tmp_path / "rollout.fifo"
    os.mkfifo(fifo)
    with pytest.raises(RolloutFormatError, match="no-follow regular file"):
        extract_rollout_timeline(fifo)


def test_rollout_nofollow_rejects_swap_to_symlink_at_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rollout.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    source.write_bytes(rollout_bytes())
    replacement.write_bytes(rollout_bytes())
    real_open = rollout_module.os.open
    swapped = False

    def swap_then_open(path: object, flags: int) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(replacement)
        return real_open(path, flags)

    monkeypatch.setattr(rollout_module.os, "open", swap_then_open)
    with pytest.raises(RolloutFormatError, match="no-follow regular file"):
        extract_rollout_timeline(source)
