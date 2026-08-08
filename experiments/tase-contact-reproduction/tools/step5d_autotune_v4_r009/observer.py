"""R009 incremental JSONL observer and freshness-safe dashboard seam."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .identity import (
    DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG,
    ExecutableBehaviorConfig,
    R009ObservabilityConfig,
)


R009_TAIL_SCHEMA = "step5d.autotune-v4/r009-incremental-jsonl-tail-v1"


class R009ObserverError(RuntimeError):
    """A malformed R009 observer input."""


def freshness_ttl_s(poll_interval_s: float) -> float:
    """Return the exact R009 freshness TTL formula."""

    if isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float)):
        raise ValueError("poll_interval_s must be finite and positive")
    interval = float(poll_interval_s)
    if not math.isfinite(interval) or interval <= 0.0:
        raise ValueError("poll_interval_s must be finite and positive")
    return max(3.0 * interval, 5.0)


@dataclass(frozen=True)
class R009TailPoll:
    rows: tuple[Mapping[str, Any], ...]
    bytes_read: int
    offset: int
    reset_reason: str | None
    malformed_rows: int
    file_exists: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": R009_TAIL_SCHEMA,
            "rows": [dict(row) for row in self.rows],
            "bytes_read": int(self.bytes_read),
            "offset": int(self.offset),
            "reset_reason": self.reset_reason,
            "malformed_rows": int(self.malformed_rows),
            "file_exists": bool(self.file_exists),
        }


class R009IncrementalJsonlTail:
    """Seek from the last byte offset and recover deterministically on reset."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._offset = 0
        self._partial = b""
        self._identity: tuple[int, int] | None = None
        self._size = 0
        self._mtime_ns: int | None = None
        self._ctime_ns: int | None = None
        self._poll_count = 0
        self._last_reset_reason: str | None = None
        self._malformed_rows = 0

    @property
    def offset(self) -> int:
        return int(self._offset)

    @property
    def last_reset_reason(self) -> str | None:
        return self._last_reset_reason

    @property
    def poll_count(self) -> int:
        return int(self._poll_count)

    @property
    def malformed_rows(self) -> int:
        return int(self._malformed_rows)

    def _reset(self, reason: str) -> None:
        self._offset = 0
        self._partial = b""
        self._last_reset_reason = reason

    @staticmethod
    def _parse_lines(data: bytes) -> tuple[list[Mapping[str, Any]], bytes, int]:
        complete, partial = (data.rsplit(b"\n", 1) if b"\n" in data else (b"", data))
        rows: list[Mapping[str, Any]] = []
        malformed = 0
        if complete:
            for line in complete.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed += 1
                    continue
                if not isinstance(value, Mapping):
                    malformed += 1
                    continue
                rows.append(dict(value))
        return rows, partial, malformed

    def poll(self) -> R009TailPoll:
        self._poll_count += 1
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # Missing is a state reset, not an empty poll.  A later
            # recreation may reuse the path (and sometimes the inode), so no
            # old offset, partial line, or file identity may survive it.
            self._reset("missing")
            self._identity = None
            self._size = 0
            self._mtime_ns = None
            self._ctime_ns = None
            return R009TailPoll((), 0, 0, "missing", 0, False)
        except OSError as exc:
            raise R009ObserverError(f"cannot stat R009 observer path {self.path}") from exc

        identity = (int(stat.st_dev), int(stat.st_ino))
        reset_reason: str | None = None
        if self._identity is None:
            reset_reason = "initial"
            self._reset(reset_reason)
        elif identity != self._identity:
            reset_reason = "replacement"
            self._reset(reset_reason)
        elif int(stat.st_size) < self._offset:
            reset_reason = "truncation"
            self._reset(reset_reason)
        elif (
            int(stat.st_size) == self._offset
            and self._mtime_ns is not None
            and (
                int(stat.st_mtime_ns) != self._mtime_ns
                or (
                    self._ctime_ns is not None
                    and int(stat.st_ctime_ns) != self._ctime_ns
                )
            )
            and self._offset > 0
        ):
            # Same inode and same length can still be a deterministic rewrite;
            # mtime/ctime change makes us reread from byte zero exactly once.
            reset_reason = "rewrite"
            self._reset(reset_reason)

        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                end_offset = handle.tell()
        except OSError as exc:
            raise R009ObserverError(f"cannot read R009 observer path {self.path}") from exc

        self._identity = identity
        self._size = int(stat.st_size)
        self._mtime_ns = int(stat.st_mtime_ns)
        self._ctime_ns = int(stat.st_ctime_ns)
        self._offset = int(end_offset)
        rows, self._partial, malformed = self._parse_lines(self._partial + chunk)
        self._malformed_rows += malformed
        return R009TailPoll(
            rows=tuple(rows),
            bytes_read=len(chunk),
            offset=self._offset,
            reset_reason=reset_reason,
            malformed_rows=malformed,
            file_exists=True,
        )


@dataclass(frozen=True)
class R009ForceSample:
    stream: str
    observed_at_s: float
    row: Mapping[str, Any]

    def age_s(self, now_s: float) -> float:
        return float(now_s) - self.observed_at_s

    def as_dict(self, *, age_s: float, stale: bool) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "observed_at_s": self.observed_at_s,
            "age_s": float(age_s),
            "stale": bool(stale),
            "row": dict(self.row),
        }


@dataclass(frozen=True)
class R009ObserverPoll:
    rows_by_stream: Mapping[str, tuple[Mapping[str, Any], ...]]
    tail_by_stream: Mapping[str, R009TailPoll]
    latest_sample: R009ForceSample | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_by_stream": {
                name: [dict(row) for row in rows]
                for name, rows in self.rows_by_stream.items()
            },
            "tail_by_stream": {
                name: poll.as_dict() for name, poll in self.tail_by_stream.items()
            },
            "latest_sample": (
                None
                if self.latest_sample is None
                else self.latest_sample.as_dict(age_s=0.0, stale=False)
            ),
        }


def _resolve_config(
    config: R009ObservabilityConfig | ExecutableBehaviorConfig | Mapping[str, Any] | None,
) -> R009ObservabilityConfig:
    if config is None:
        executable = ExecutableBehaviorConfig.from_mapping(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG)
        resolved = executable.observability
    elif isinstance(config, R009ObservabilityConfig):
        resolved = config
    elif isinstance(config, ExecutableBehaviorConfig):
        resolved = config.observability
    elif isinstance(config, Mapping):
        resolved = R009ObservabilityConfig.from_mapping(config)
    else:
        raise TypeError("R009 observer config must be typed")
    if resolved is None:
        raise R009ObserverError(
            "R009 observer requires identity-bound observability behavior config"
        )
    return resolved


class R009DashboardObserver:
    """Incrementally consume state20/state25 rows and gate force freshness."""

    _FORCE_FIELDS = (
        "force_norm_n",
        "normal_load_n",
        "filtered_normal_n",
        "force_integral_n_s",
    )
    _CURRENT_FORCE_SAMPLE_ALIASES = (
        "force_sample",
        "current_force_sample",
        "force_sample_meta",
        "current_force_sample_meta",
        "sample",
        "current_sample",
        "sample_meta",
        "current_sample_meta",
    )

    def __init__(
        self,
        *,
        paths: Mapping[str, Path],
        config: R009ObservabilityConfig | ExecutableBehaviorConfig | Mapping[str, Any] | None = None,
    ) -> None:
        self.config = _resolve_config(config)
        self.poll_interval_s = self.config.observer_poll_interval_s
        self.freshness_ttl_s = freshness_ttl_s(self.poll_interval_s)
        self.tails = {
            str(name): R009IncrementalJsonlTail(Path(path))
            for name, path in sorted(paths.items())
        }
        self._latest_sample: R009ForceSample | None = None

    @property
    def latest_sample(self) -> R009ForceSample | None:
        return self._latest_sample

    def _sample_from_row(self, stream: str, row: Mapping[str, Any]) -> R009ForceSample | None:
        if not any(field in row for field in self._FORCE_FIELDS):
            return None
        raw_time = row.get("r009_observed_at_s", row.get("monotonic_s"))
        if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)):
            return None
        timestamp = float(raw_time)
        if not math.isfinite(timestamp):
            return None
        return R009ForceSample(stream=stream, observed_at_s=timestamp, row=dict(row))

    def poll(self) -> R009ObserverPoll:
        rows_by_stream: dict[str, tuple[Mapping[str, Any], ...]] = {}
        tail_by_stream: dict[str, R009TailPoll] = {}
        candidates: list[R009ForceSample] = []
        for stream, tail in self.tails.items():
            result = tail.poll()
            rows_by_stream[stream] = result.rows
            tail_by_stream[stream] = result
            candidates.extend(
                sample
                for row in result.rows
                if (sample := self._sample_from_row(stream, row)) is not None
            )
        if candidates:
            # Poll order is sorted by stream name; the last equal-time row is
            # therefore deterministic as well.
            candidates.sort(key=lambda sample: (sample.observed_at_s, sample.stream))
            candidate = candidates[-1]
            if self._latest_sample is None or (
                candidate.observed_at_s,
                candidate.stream,
            ) >= (
                self._latest_sample.observed_at_s,
                self._latest_sample.stream,
            ):
                self._latest_sample = candidate
        return R009ObserverPoll(rows_by_stream, tail_by_stream, self._latest_sample)

    def decorate_current_event(
        self, event: Mapping[str, Any], *, now_s: float
    ) -> dict[str, Any]:
        """Add only a fresh sample; stale data is metadata, never current data."""

        if not isinstance(event, Mapping):
            raise TypeError("dashboard event must be a mapping")
        now = float(now_s)
        if not math.isfinite(now):
            raise ValueError("now_s must be finite")
        decorated = dict(event)
        # Remove our current-data keys first so a caller cannot accidentally
        # carry a stale value forward into the new dashboard event.
        for key in self._CURRENT_FORCE_SAMPLE_ALIASES:
            decorated.pop(key, None)
        sample = self._latest_sample
        if sample is None:
            decorated.pop("stale_force_sample", None)
            decorated.pop("stale_force_sample_meta", None)
            return decorated
        age = sample.age_s(now)
        stale = age < 0.0 or age > self.freshness_ttl_s
        if stale:
            decorated["stale_force_sample"] = sample.as_dict(age_s=age, stale=True)
            decorated["stale_force_sample_meta"] = {
                "stale": True,
                "age_s": age,
                "ttl_s": self.freshness_ttl_s,
                "source_stream": sample.stream,
            }
            return decorated
        decorated.pop("stale_force_sample", None)
        decorated.pop("stale_force_sample_meta", None)
        decorated["force_sample"] = sample.as_dict(age_s=age, stale=False)
        decorated["force_sample_meta"] = {
            "stale": False,
            "age_s": age,
            "ttl_s": self.freshness_ttl_s,
            "source_stream": sample.stream,
        }
        return decorated

    current_event = decorate_current_event


__all__ = [
    "R009DashboardObserver",
    "R009ForceSample",
    "R009IncrementalJsonlTail",
    "R009ObserverError",
    "R009ObserverPoll",
    "R009TailPoll",
    "R009_TAIL_SCHEMA",
    "freshness_ttl_s",
]
