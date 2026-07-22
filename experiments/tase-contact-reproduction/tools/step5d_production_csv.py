#!/usr/bin/python3.10
"""Production growing-CSV publication and EOF-follow primitives for Step5d.

The module owns the byte-visibility contract shared by the real bridge and the
offline release gate.  It has no controller, RTDE, bridge-launch, or motion
capability.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TextIO


TERMINAL_TP_STATES = frozenset({75, 76, 77, 78, 90})
TIMEOUT_CODES = frozenset(
    {
        "no_fresh_rows",
        "fresh_rows_never_qualified",
        "terminal_seen_capture_not_sealed",
    }
)


@dataclass(frozen=True)
class ProductionCsvWriterStats:
    rows_written: int
    buffered_flushes: int
    terminal_flushes: int
    durable_fsyncs: int


class ProductionCsvWriter:
    """The bridge's real buffered writer with durable header/terminal publish."""

    def __init__(
        self,
        handle: TextIO,
        fieldnames: Sequence[str],
        *,
        flush_interval_rows: int = 50,
        terminal_state_column: str = "ur_output_int_register_26",
    ) -> None:
        if not hasattr(handle, "write") or handle.closed:
            raise ValueError("CSV handle must be an open writable text stream")
        names = tuple(str(name) for name in fieldnames)
        if not names or len(names) != len(set(names)):
            raise ValueError("CSV fieldnames must be unique and non-empty")
        if type(flush_interval_rows) is not int or flush_interval_rows < 1:
            raise ValueError("flush_interval_rows must be a positive integer")
        self.handle = handle
        self.fieldnames = names
        self.flush_interval_rows = flush_interval_rows
        self.terminal_state_column = terminal_state_column
        self.writer = csv.DictWriter(handle, fieldnames=names)
        self._rows_written = 0
        self._rows_since_flush = 0
        self._buffered_flushes = 0
        self._terminal_flushes = 0
        self._durable_fsyncs = 0
        self.writer.writeheader()
        self.flush(durable=True)

    @staticmethod
    def _state(value: Any) -> int | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or not parsed.is_integer():
            return None
        return int(parsed)

    def writerow(self, row: Mapping[str, Any]) -> bool:
        if not isinstance(row, Mapping):
            raise ValueError("CSV row must be a mapping")
        self.writer.writerow(row)
        self._rows_written += 1
        self._rows_since_flush += 1
        terminal = self._state(row.get(self.terminal_state_column)) in TERMINAL_TP_STATES
        if terminal:
            self._terminal_flushes += 1
            self.flush(durable=True)
        elif self._rows_since_flush >= self.flush_interval_rows:
            self._buffered_flushes += 1
            self.flush(durable=False)
        return terminal

    def publish_row_with_partial_visibility(
        self,
        row: Mapping[str, Any],
        *,
        split_at: int,
        partial_visible_s: float,
    ) -> bool:
        """Exercise a real mid-row visibility/crash cut in a writer subprocess.

        The release gate is the only caller.  Both halves use the production
        dialect and the completed row follows the same terminal flush/fsync
        policy as ``writerow``.
        """

        buffer = io.StringIO(newline="")
        csv.DictWriter(buffer, fieldnames=self.fieldnames).writerow(row)
        encoded = buffer.getvalue()
        if (
            type(split_at) is not int
            or split_at < 1
            or split_at >= len(encoded) - 1
        ):
            raise ValueError("partial row split must be inside the encoded row")
        if partial_visible_s < 0.0:
            raise ValueError("partial_visible_s must be non-negative")
        self.handle.write(encoded[:split_at])
        self.flush(durable=True)
        time.sleep(partial_visible_s)
        self.handle.write(encoded[split_at:])
        self._rows_written += 1
        terminal = self._state(row.get(self.terminal_state_column)) in TERMINAL_TP_STATES
        if terminal:
            self._terminal_flushes += 1
            self.flush(durable=True)
        else:
            self._rows_since_flush += 1
        return terminal

    def flush(self, *, durable: bool) -> None:
        self.handle.flush()
        self._rows_since_flush = 0
        if durable:
            os.fsync(self.handle.fileno())
            self._durable_fsyncs += 1

    @property
    def stats(self) -> ProductionCsvWriterStats:
        return ProductionCsvWriterStats(
            rows_written=self._rows_written,
            buffered_flushes=self._buffered_flushes,
            terminal_flushes=self._terminal_flushes,
            durable_fsyncs=self._durable_fsyncs,
        )


@dataclass
class BridgeCsvFollowerStats:
    rows_seen: int = 0
    partial_line_polls: int = 0
    identity_rejects: int = 0
    predicate_rejects: int = 0
    longest_qualified_dwell_s: float = 0.0


class BridgeCsvTimeout(TimeoutError):
    def __init__(
        self,
        code: str,
        *,
        stats: BridgeCsvFollowerStats,
        missing_columns: Sequence[str] = (),
    ) -> None:
        if code not in TIMEOUT_CODES:
            raise ValueError("bridge CSV timeout code is invalid")
        self.code = code
        self.stats = BridgeCsvFollowerStats(**asdict(stats))
        self.missing_columns = tuple(sorted(set(missing_columns)))
        super().__init__(
            "bridge_csv_timeout:"
            + code
            + ":"
            + json.dumps(
                {
                    "missing_columns": list(self.missing_columns),
                    "stats": asdict(self.stats),
                },
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


class BridgeCsvFollower:
    """Follow only complete rows appended after construction, with rollback."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file() or self.path.is_symlink():
            raise RuntimeError("bridge CSV must be an existing regular file")
        self.handle = self.path.open("r", newline="", encoding="utf-8")
        header = self.handle.readline()
        self.fieldnames = tuple(next(csv.reader([header])))
        if not self.fieldnames or len(self.fieldnames) != len(set(self.fieldnames)):
            raise RuntimeError("bridge CSV header is missing or duplicated")
        self.handle.seek(0, os.SEEK_END)
        self.stats = BridgeCsvFollowerStats()
        self._terminal_seen = False
        self._terminal_capture_path: Path | None = None
        self._missing_columns: set[str] = set()
        self._qualified_start_s: float | None = None

    def note_identity_reject(self) -> None:
        self.stats.identity_rejects += 1
        self._qualified_start_s = None

    def note_predicate_reject(self, *, missing_columns: Sequence[str] = ()) -> None:
        self.stats.predicate_rejects += 1
        self._missing_columns.update(str(name) for name in missing_columns)
        self._qualified_start_s = None

    def note_qualified(self, monotonic_s: float) -> None:
        value = float(monotonic_s)
        if not math.isfinite(value):
            raise ValueError("qualified dwell timestamp must be finite")
        if self._qualified_start_s is None:
            self._qualified_start_s = value
        self.stats.longest_qualified_dwell_s = max(
            self.stats.longest_qualified_dwell_s,
            max(0.0, value - self._qualified_start_s),
        )

    def mark_terminal_seen(self, capture_path: Path) -> None:
        self._terminal_seen = True
        self._terminal_capture_path = capture_path.resolve()

    def terminal_capture_timeout(self) -> BridgeCsvTimeout:
        return BridgeCsvTimeout(
            "terminal_seen_capture_not_sealed",
            stats=self.stats,
            missing_columns=self._missing_columns,
        )

    def _timeout_code(self, rows_at_start: int) -> str:
        if self._terminal_seen and (
            self._terminal_capture_path is None
            or not self._terminal_capture_path.is_file()
            or self._terminal_capture_path.is_symlink()
        ):
            return "terminal_seen_capture_not_sealed"
        if self.stats.rows_seen == rows_at_start:
            return "no_fresh_rows"
        return "fresh_rows_never_qualified"

    def rows(self, *, timeout_s: float) -> Iterator[dict[str, str]]:
        deadline = time.monotonic() + timeout_s
        rows_at_start = self.stats.rows_seen
        while time.monotonic() < deadline:
            position = self.handle.tell()
            line = self.handle.readline()
            if not line or not line.endswith("\n"):
                self.handle.seek(position)
                if line:
                    self.stats.partial_line_polls += 1
                time.sleep(0.01)
                continue
            values = next(csv.reader([line]))
            if len(values) != len(self.fieldnames):
                raise RuntimeError("bridge CSV row width changed")
            self.stats.rows_seen += 1
            yield dict(zip(self.fieldnames, values))
        raise BridgeCsvTimeout(
            self._timeout_code(rows_at_start),
            stats=self.stats,
            missing_columns=self._missing_columns,
        )

    def wait_for(
        self,
        predicate: Callable[[Mapping[str, str]], bool],
        *,
        timeout_s: float,
        required_columns: Sequence[str] = (),
        identity_predicate: Callable[[Mapping[str, str]], bool] | None = None,
    ) -> dict[str, str]:
        missing = tuple(
            name for name in required_columns if name not in self.fieldnames
        )
        for row in self.rows(timeout_s=timeout_s):
            if identity_predicate is not None and not identity_predicate(row):
                self.note_identity_reject()
                continue
            if missing:
                self.note_predicate_reject(missing_columns=missing)
                continue
            try:
                qualified = bool(predicate(row))
            except KeyError as exc:
                self.note_predicate_reject(missing_columns=(str(exc.args[0]),))
                continue
            if not qualified:
                self.note_predicate_reject()
                continue
            timestamp = row.get("t_monotonic_s")
            if timestamp not in (None, ""):
                self.note_qualified(float(timestamp))
            return row
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.handle.close()
