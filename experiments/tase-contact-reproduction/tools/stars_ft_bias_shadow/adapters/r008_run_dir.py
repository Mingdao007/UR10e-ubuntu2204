"""Strict, streaming adapter for historical R008 shadow inputs.

The adapter is intentionally read-only.  It accepts only a completed
``live_*`` snapshot rooted below the R008 experiment directory and never
imports or invokes controller, bridge, RTDE, or live-run code.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import stat
import time
from typing import Any, Iterator, Mapping, Sequence


class R008RunDirError(ValueError):
    """Typed fail-closed error for preflight, replay, and receipt checks."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "run_dir_invalid",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _error(code: str, message: str, **details: Any) -> R008RunDirError:
    return R008RunDirError(message, code=code, details=details)


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant {token}")


def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_strict_json_bytes(data: bytes, *, label: str) -> Any:
    """Parse UTF-8 JSON while rejecting duplicates and NaN/Infinity."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error("invalid_utf8", f"{label} is not strict UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise _error("invalid_json", f"{label} is not strict JSON: {exc}") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Return the identity-safe canonical JSON representation."""

    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error("noncanonical_json", f"value cannot be canonicalized: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finite_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("invalid_number", f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error("invalid_number", f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise _error("nonfinite_number", f"{label} must be finite")
    return number


def _wrench6(values: Any, label: str) -> tuple[float, float, float, float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 6:
        raise _error("invalid_wrench", f"{label} must be an exact length-6 wrench")
    converted = tuple(_finite_number(value, label=f"{label}[{index}]") for index, value in enumerate(values))
    return converted  # type: ignore[return-value]


@dataclass(frozen=True)
class SoftwareBaseline:
    mean_wrench_n_nm: tuple[float, float, float, float, float, float]
    stdev_wrench_n_nm: tuple[float, float, float, float, float, float] | None
    sample_count: int
    zero_tare_config_write: bool
    schema: str
    path: Path


def load_software_baseline(run_dir: Path) -> SoftwareBaseline:
    path = Path(run_dir) / "software_baseline.json"
    if path.is_symlink() or not path.is_file():
        raise _error("missing_or_symlink_input", f"missing regular software_baseline.json under {run_dir}")
    raw = parse_strict_json_bytes(path.read_bytes(), label="software_baseline.json")
    if not isinstance(raw, dict):
        raise _error("invalid_baseline", "software_baseline.json must be an object")
    if raw.get("zero_tare_config_write") is not False:
        raise _error(
            "tare_or_config_write_forbidden",
            "software_baseline.zero_tare_config_write must be false",
        )
    schema = raw.get("schema")
    if not isinstance(schema, str) or not schema:
        raise _error("invalid_baseline", "software_baseline.schema must be a non-empty string")
    sample_count = raw.get("sample_count")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
        raise _error("invalid_baseline", "software_baseline.sample_count must be a positive integer")
    for name in ("parse_errors", "dropped_bytes"):
        if name in raw and raw[name] != 0:
            raise _error("invalid_baseline", f"software_baseline.{name} must be zero")
    mean = _wrench6(raw.get("mean_wrench_n_nm"), "mean_wrench_n_nm")
    stdev_raw = raw.get("stdev_wrench_n_nm")
    stdev = None if stdev_raw is None else _wrench6(stdev_raw, "stdev_wrench_n_nm")
    if stdev is not None and any(value < 0.0 for value in stdev):
        raise _error("invalid_baseline", "stdev_wrench_n_nm cannot be negative")
    return SoftwareBaseline(
        mean_wrench_n_nm=mean,
        stdev_wrench_n_nm=stdev,
        sample_count=sample_count,
        zero_tare_config_write=False,
        schema=schema,
        path=path,
    )


def _pose6(value: Any) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        result.append(number)
    return tuple(result)  # type: ignore[return-value]


def _tcp_speed(
    previous_pose: Sequence[float] | None,
    pose: Sequence[float] | None,
    dt_s: float | None,
) -> tuple[float | None, float | None]:
    if previous_pose is None or pose is None or dt_s is None or dt_s <= 0.0:
        return None, None
    if len(previous_pose) != 6 or len(pose) != 6:
        return None, None
    try:
        delta = [float(pose[index]) - float(previous_pose[index]) for index in range(6)]
    except (TypeError, ValueError, OverflowError):
        return None, None
    if not all(math.isfinite(value) for value in delta):
        return None, None
    linear = math.sqrt(sum(value * value for value in delta[:3])) / dt_s
    angular = math.sqrt(sum(value * value for value in delta[3:])) / dt_s
    if not math.isfinite(linear) or not math.isfinite(angular):
        return None, None
    return linear, angular


@dataclass(frozen=True)
class TraceRecord:
    source: str
    line_no: int
    row: dict[str, Any]
    t_s: float
    wall_time_s: float
    packet_sequence: int


@dataclass(frozen=True)
class TraceSample:
    source: str
    line_no: int
    row: dict[str, Any]
    t_s: float
    wall_time_s: float
    packet_sequence: int
    dt_raw_s: float | None
    dt_credited_s: float
    gap: bool
    gap_s: float
    source_first: bool
    source_dt_raw_s: float | None
    tcp_speed_m_s: float | None
    tcp_omega_rad_s: float | None

    @property
    def dt_s(self) -> float:
        """Compatibility alias: estimator time is credited time only."""

        return self.dt_credited_s


def _trace_path(run_dir: Path, source: str) -> Path:
    if source == "state20":
        return Path(run_dir) / "r008-state20-search-trace.jsonl"
    if source == "state25":
        return Path(run_dir) / "r008-state25-path-trace.jsonl"
    raise _error("unknown_source", f"unknown trace source {source}")


TRACE_SCHEMAS = {
    "state20": "step5d.autotune-v4/r008-state20-search-trace-v1",
    "state25": "step5d.autotune-v4/r008-state25-path-trace-v1",
}


def _iter_trace_file(source: str, path: Path) -> Iterator[TraceRecord]:
    previous_t: float | None = None
    previous_wall_time: float | None = None
    previous_sequence: int | None = None
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise _error("trace_open_failed", f"cannot open {source} trace") from exc
    with handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                raise _error("blank_trace_line", f"{source} trace line {line_no} is blank")
            try:
                row = parse_strict_json_bytes(raw_line.rstrip(b"\r\n"), label=f"{source} trace line {line_no}")
            except R008RunDirError:
                raise
            if not isinstance(row, dict):
                raise _error("trace_row_not_object", f"{source} trace line {line_no} is not an object")
            if row.get("schema") != TRACE_SCHEMAS[source]:
                raise _error(
                    "trace_schema_mismatch",
                    f"{source} trace line {line_no} schema is not {TRACE_SCHEMAS[source]!r}",
                )
            if "source" in row and row["source"] != source:
                raise _error(
                    "source_binding_mismatch",
                    f"{source} trace line {line_no} source binding is not {source!r}",
                )
            t_s = _finite_number(row.get("monotonic_s"), label=f"{source} line {line_no}.monotonic_s")
            wall_time_s = _finite_number(row.get("wall_time_s"), label=f"{source} line {line_no}.wall_time_s")
            sequence = row.get("packet_sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise _error(
                    "invalid_packet_sequence",
                    f"{source} trace line {line_no}.packet_sequence must be a non-negative integer",
                )
            if previous_t is not None and t_s <= previous_t:
                raise _error(
                    "trace_time_not_monotonic",
                    f"{source} trace line {line_no} time is not strictly increasing",
                )
            if previous_wall_time is not None and wall_time_s <= previous_wall_time:
                raise _error(
                    "trace_wall_time_not_monotonic",
                    f"{source} trace line {line_no} wall time is not strictly increasing",
                )
            if previous_sequence is not None and sequence <= previous_sequence:
                raise _error(
                    "trace_sequence_not_monotonic",
                    f"{source} trace line {line_no} packet sequence is not strictly increasing",
                )
            previous_t = t_s
            previous_wall_time = wall_time_s
            previous_sequence = sequence
            yield TraceRecord(
                source=source,
                line_no=line_no,
                row=row,
                t_s=t_s,
                wall_time_s=wall_time_s,
                packet_sequence=sequence,
            )


def _iter_global_records(run_dir: Path) -> Iterator[TraceRecord]:
    """Two-way chronological merge with one look-ahead row per source."""

    sources = ("state20", "state25")
    iterators: dict[str, Iterator[TraceRecord]] = {}
    heads: dict[str, TraceRecord] = {}
    for source in sources:
        path = _trace_path(run_dir, source)
        if path.is_symlink():
            raise _error("symlink_input", f"{source} trace must not be a symlink")
        if not path.is_file():
            if source == "state25":
                continue
            raise _error("missing_input", f"missing required {source} trace")
        iterator = _iter_trace_file(source, path)
        iterators[source] = iterator
        try:
            heads[source] = next(iterator)
        except StopIteration:
            continue
    rank = {"state20": 0, "state25": 1}
    while heads:
        source = min(heads, key=lambda item: (heads[item].t_s, rank[item], heads[item].line_no))
        current = heads.pop(source)
        yield current
        iterator = iterators[source]
        try:
            heads[source] = next(iterator)
        except StopIteration:
            pass


def iter_trace_samples(
    run_dir: Path,
    *,
    max_contiguous_dt_s: float = 1.0,
) -> Iterator[TraceSample]:
    """Stream both traces in global time order without loading either file."""

    if not math.isfinite(max_contiguous_dt_s) or max_contiguous_dt_s <= 0.0:
        raise _error("invalid_dt_limit", "max_contiguous_dt_s must be finite and positive")
    previous_global_t: float | None = None
    previous_pose: dict[str, tuple[float, float, float, float, float, float] | None] = {}
    previous_source_t: dict[str, float] = {}
    seen_sources: set[str] = set()
    for record in _iter_global_records(Path(run_dir)):
        source_first = record.source not in seen_sources
        raw_dt = None if previous_global_t is None else record.t_s - previous_global_t
        if raw_dt is not None and raw_dt < 0.0:
            raise _error("global_time_not_monotonic", "merged trace time moved backwards")
        gap = raw_dt is not None and raw_dt > max_contiguous_dt_s
        gap_s = raw_dt if gap and raw_dt is not None else 0.0
        credited = 0.0
        if raw_dt is not None and 0.0 < raw_dt <= max_contiguous_dt_s:
            credited = raw_dt
        pose = _pose6(record.row.get("tcp_pose_m_rad"))
        source_dt = (
            None
            if record.source not in previous_source_t
            else record.t_s - previous_source_t[record.source]
        )
        speed, omega = _tcp_speed(previous_pose.get(record.source), pose, source_dt)
        yield TraceSample(
            source=record.source,
            line_no=record.line_no,
            row=record.row,
            t_s=record.t_s,
            wall_time_s=record.wall_time_s,
            packet_sequence=record.packet_sequence,
            dt_raw_s=raw_dt,
            dt_credited_s=credited,
            gap=gap,
            gap_s=gap_s,
            source_first=source_first,
            source_dt_raw_s=source_dt,
            tcp_speed_m_s=speed,
            tcp_omega_rad_s=omega,
        )
        previous_global_t = record.t_s
        previous_pose[record.source] = pose
        previous_source_t[record.source] = record.t_s
        seen_sources.add(record.source)


@dataclass(frozen=True)
class InputFileSnapshot:
    path: str
    required: bool
    present: bool
    bytes: int
    mode: int | None
    mtime_ns: int | None
    inode: int | None
    device: int | None
    sha256: str | None


@dataclass(frozen=True)
class HistoricalRunSnapshot:
    run_dir: Path
    run_id: str
    baseline: SoftwareBaseline
    launch_context_identity_sha256: str
    launch_context_identity_anchor: str
    terminal_status: str
    formal_live_complete: bool
    input_files: tuple[InputFileSnapshot, ...]
    input_manifest: dict[str, Any]
    input_manifest_sha256: str
    source_coverage: dict[str, Any]
    phase_timing_rows: int
    host_non_json_line_count: int
    host_non_object_line_count: int
    host_blank_line_count: int
    terminal_evidence: dict[str, Any]


DECLARED_INPUTS: tuple[tuple[str, bool], ...] = (
    ("software_baseline.json", True),
    ("launch_context.json", True),
    ("host.log", True),
    ("r008-state20-search-trace.jsonl", True),
    ("r008-state25-path-trace.jsonl", False),
    ("r008-phase-timings.jsonl", False),
)


def _regular_snapshot(
    run_dir: Path,
    relative_path: str,
    required: bool,
    *,
    min_input_age_s: float,
    max_input_file_bytes: int,
) -> InputFileSnapshot:
    path = run_dir / relative_path
    if path.is_symlink():
        raise _error("symlink_input", f"input {relative_path} must not be a symlink")
    if not path.exists():
        if required:
            raise _error("missing_input", f"missing required input {relative_path}")
        return InputFileSnapshot(relative_path, required, False, 0, None, None, None, None, None)
    try:
        info = path.stat()
    except OSError as exc:
        raise _error("input_stat_failed", f"cannot stat input {relative_path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise _error("input_not_regular", f"input {relative_path} must be a regular file")
    age_s = time.time() - info.st_mtime
    if min_input_age_s > 0.0 and age_s < min_input_age_s:
        raise _error(
            "input_too_new",
            f"input {relative_path} is younger than configured minimum age",
            age_s=age_s,
            min_input_age_s=min_input_age_s,
        )
    if info.st_size > max_input_file_bytes:
        raise _error(
            "input_file_too_large",
            f"input {relative_path} exceeds per-file size limit",
            bytes=info.st_size,
            max_input_file_bytes=max_input_file_bytes,
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise _error("input_read_failed", f"cannot read input {relative_path}") from exc
    try:
        end_info = path.stat()
    except OSError as exc:
        raise _error("input_stat_failed", f"cannot restat input {relative_path}") from exc
    start_stat = (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev, stat.S_IMODE(info.st_mode))
    end_stat = (
        end_info.st_size,
        end_info.st_mtime_ns,
        end_info.st_ino,
        end_info.st_dev,
        stat.S_IMODE(end_info.st_mode),
    )
    if start_stat != end_stat:
        raise _error("input_mutated_during_preflight", f"input {relative_path} changed while being hashed")
    return InputFileSnapshot(
        path=relative_path,
        required=required,
        present=True,
        bytes=info.st_size,
        mode=stat.S_IMODE(info.st_mode),
        mtime_ns=info.st_mtime_ns,
        inode=info.st_ino,
        device=info.st_dev,
        sha256=digest.hexdigest(),
    )


def _snapshot_declared_inputs(
    run_dir: Path,
    *,
    min_input_age_s: float,
    max_input_file_bytes: int,
    max_total_input_bytes: int,
) -> tuple[InputFileSnapshot, ...]:
    snapshots = tuple(
        _regular_snapshot(
            run_dir,
            relative_path,
            required,
            min_input_age_s=min_input_age_s,
            max_input_file_bytes=max_input_file_bytes,
        )
        for relative_path, required in DECLARED_INPUTS
    )
    total = sum(item.bytes for item in snapshots if item.present)
    if total > max_total_input_bytes:
        raise _error(
            "input_total_too_large",
            "declared inputs exceed total size limit",
            bytes=total,
            max_total_input_bytes=max_total_input_bytes,
        )
    return snapshots


def _manifest_file(item: InputFileSnapshot) -> dict[str, Any]:
    return {
        "path": item.path,
        "required": item.required,
        "present": item.present,
        "bytes": item.bytes,
        "mode": item.mode,
        "mtime_ns": item.mtime_ns,
        "inode": item.inode,
        "device": item.device,
        "sha256": item.sha256,
    }


def _load_launch_context(run_dir: Path) -> tuple[str, str, bool | None]:
    path = run_dir / "launch_context.json"
    raw = parse_strict_json_bytes(path.read_bytes(), label="launch_context.json")
    if not isinstance(raw, dict):
        raise _error("invalid_launch_context", "launch_context.json must be an object")
    if "run_id" in raw and raw["run_id"] != run_dir.name:
        raise _error("launch_context_identity_mismatch", "launch_context.run_id differs from directory")
    anchors = (
        "identity",
        "identity_sha256",
        "launch_context_identity_sha256",
        "contract_sha256",
        "campaign_fingerprint",
        "session_id",
        "route_id",
    )
    anchor_name = next(
        (
            name
            for name in anchors
            if isinstance(raw.get(name), str) and bool(raw.get(name))
        ),
        None,
    )
    if anchor_name is None:
        raise _error(
            "missing_launch_context_identity",
            "launch_context.json must contain an identity anchor",
        )
    launch_formal = raw.get("formal_live_complete")
    if "formal_live_complete" in raw and not isinstance(launch_formal, bool):
        raise _error(
            "invalid_formal_live_complete",
            "launch_context.formal_live_complete must be boolean",
        )
    identity_sha256 = sha256_bytes(canonical_json_bytes(raw))
    return identity_sha256, str(raw[anchor_name]), launch_formal


def _load_terminal_host_status(
    run_dir: Path,
    *,
    launch_context_formal_live_complete: bool | None,
) -> tuple[str, bool, dict[str, Any]]:
    path = run_dir / "host.log"
    terminal: dict[str, Any] | None = None
    terminal_line_no: int | None = None
    last_nonblank_kind: str | None = None
    last_nonblank_line_no: int | None = None
    non_json_line_count = 0
    non_object_line_count = 0
    blank_line_count = 0
    with path.open("rb") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                blank_line_count += 1
                continue
            last_nonblank_line_no = line_no
            try:
                row = parse_strict_json_bytes(
                    raw_line.rstrip(b"\r\n"), label=f"host.log line {line_no}"
                )
            except R008RunDirError:
                # Historical host logs may contain deterministic warning text.
                # It is admitted only before the final terminal object and is
                # explicitly counted in every sealed evidence surface.
                non_json_line_count += 1
                last_nonblank_kind = "non_json"
                continue
            if not isinstance(row, dict):
                non_object_line_count += 1
                last_nonblank_kind = "non_object"
                continue
            terminal = row
            terminal_line_no = line_no
            last_nonblank_kind = "object"
    if last_nonblank_kind is None:
        raise _error("missing_terminal_status", "host.log has no terminal JSON row")
    if last_nonblank_kind == "non_json":
        raise _error(
            "host_log_trailing_corruption",
            "host.log final nonblank line is not strict JSON",
            line=last_nonblank_line_no,
        )
    if last_nonblank_kind == "non_object" or terminal is None:
        raise _error(
            "host_log_terminal_not_object",
            "host.log final nonblank line must be a JSON object",
            line=last_nonblank_line_no,
        )
    status_values = [name for name in ("status", "terminal_status") if name in terminal]
    if len(status_values) == 2 and terminal["status"] != terminal["terminal_status"]:
        raise _error("ambiguous_terminal_status", "host.log terminal status fields disagree")
    status_field = status_values[0] if status_values else None
    status = terminal.get("status", terminal.get("terminal_status"))
    if status == "complete":
        status = "completed"
    if status not in {"completed", "incomplete_stopped"}:
        raise _error(
            "missing_terminal_status",
            "host.log terminal status must be completed or incomplete_stopped",
        )
    host_formal_present = "formal_live_complete" in terminal
    host_formal = terminal.get("formal_live_complete")
    if host_formal_present and not isinstance(host_formal, bool):
        raise _error("invalid_formal_live_complete", "formal_live_complete must be boolean")
    if (
        host_formal_present
        and launch_context_formal_live_complete is not None
        and host_formal != launch_context_formal_live_complete
    ):
        raise _error(
            "ambiguous_formal_live_complete",
            "host.log and launch_context formal_live_complete values disagree",
        )

    if host_formal_present:
        explicit_formal = host_formal
        formal_field = "host.log.formal_live_complete"
        formal_provenance = "explicit_host_json"
    elif launch_context_formal_live_complete is not None:
        explicit_formal = launch_context_formal_live_complete
        formal_field = "launch_context.formal_live_complete"
        formal_provenance = "explicit_launch_context"
    else:
        explicit_formal = None
        formal_field = None
        formal_provenance = None

    if status == "completed":
        if explicit_formal is None:
            raise _error(
                "invalid_formal_live_complete",
                "completed terminal status requires an explicit formal_live_complete boolean",
            )
        formal = explicit_formal
    elif explicit_formal is None:
        # An incomplete terminal status is conclusive one-way evidence.  It
        # cannot promote to formal completion, but it can admit a historical
        # host object that predates the formal flag.
        formal = False
        formal_field = "terminal_status_implies_false"
        formal_provenance = "terminal_status_implies_false"
    elif explicit_formal is True:
        # Preserve the contradictory explicit evidence while making the
        # effective result fail closed.
        formal = False
        formal_provenance = f"{formal_provenance}_status_override_terminal_status_implies_false"
    else:
        formal = False
    terminal_evidence = {
        "source": "host.log",
        "terminal_line_policy": "final_nonblank_line_strict_json_object",
        "terminal_line_number": terminal_line_no,
        "terminal_status_field": status_field,
        "terminal_status": status,
        "formal_live_complete_field": formal_field,
        "formal_live_complete_explicit_value": explicit_formal,
        "formal_live_complete_provenance": formal_provenance,
        "formal_live_complete": formal,
        "non_json_line_count": non_json_line_count,
        "non_object_line_count": non_object_line_count,
        "blank_line_count": blank_line_count,
        "trailing_blank_lines_allowed": True,
    }
    return status, formal, terminal_evidence


def _phase_rows(run_dir: Path) -> int:
    path = run_dir / "r008-phase-timings.jsonl"
    if not path.exists():
        return 0
    if path.is_symlink():
        raise _error("symlink_input", "r008-phase-timings.jsonl must not be a symlink")
    count = 0
    with path.open("rb") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                raise _error("blank_phase_timing_line", f"phase timing line {line_no} is blank")
            row = parse_strict_json_bytes(raw_line.rstrip(b"\r\n"), label=f"phase timing line {line_no}")
            if not isinstance(row, dict):
                raise _error("phase_timing_row_not_object", f"phase timing line {line_no} is not an object")
            count += 1
    return count


def _rooted_run_dir(run_dir: Path, experiment_root: Path) -> Path:
    root = Path(experiment_root).resolve()
    candidate = Path(run_dir)
    if candidate.is_symlink():
        raise _error("symlink_run_dir", "run directory must not be a symlink")
    resolved = candidate.resolve()
    expected_parent = (root / "runs" / "step5d_autotune_v4_r008").resolve()
    if resolved.parent != expected_parent:
        raise _error(
            "run_dir_outside_r008",
            "run directory must be directly below the R008 experiment root",
        )
    if not resolved.is_dir() or not resolved.name.startswith("live_"):
        raise _error("invalid_run_dir", "run directory must be an existing live_* directory")
    return resolved


def preflight_run_dir(
    run_dir: Path,
    *,
    experiment_root: Path,
    min_input_age_s: float,
    max_input_file_bytes: int,
    max_total_input_bytes: int,
    max_contiguous_dt_s: float,
    allow_missing_state25: bool = True,
) -> HistoricalRunSnapshot:
    """Validate and hash a historical run before any output is opened."""

    rooted = _rooted_run_dir(Path(run_dir), Path(experiment_root))
    snapshots = _snapshot_declared_inputs(
        rooted,
        min_input_age_s=min_input_age_s,
        max_input_file_bytes=max_input_file_bytes,
        max_total_input_bytes=max_total_input_bytes,
    )
    state25 = next(item for item in snapshots if item.path == "r008-state25-path-trace.jsonl")
    if not state25.present and not allow_missing_state25:
        raise _error("missing_state25_not_allowed", "state25 is missing and partial coverage is disabled")
    baseline = load_software_baseline(rooted)
    launch_identity, launch_anchor, launch_formal = _load_launch_context(rooted)
    terminal_status, formal_live_complete, terminal_evidence = _load_terminal_host_status(
        rooted,
        launch_context_formal_live_complete=launch_formal,
    )

    source_rows = {"state20": 0, "state25": 0}
    source_credited = {"state20": 0.0, "state25": 0.0}
    source_gaps = {"state20": 0.0, "state25": 0.0}
    total_rows = 0
    first_t: float | None = None
    last_t: float | None = None
    first_wall_t: float | None = None
    last_wall_t: float | None = None
    for sample in iter_trace_samples(rooted, max_contiguous_dt_s=max_contiguous_dt_s):
        if first_t is None:
            first_t = sample.t_s
        if first_wall_t is None:
            first_wall_t = sample.wall_time_s
        last_t = sample.t_s
        last_wall_t = sample.wall_time_s
        source_rows[sample.source] += 1
        source_credited[sample.source] += sample.dt_credited_s
        source_gaps[sample.source] += sample.gap_s
        total_rows += 1
    if source_rows["state20"] <= 0:
        raise _error("empty_state20_trace", "state20 trace must contain at least one row")
    partial = not state25.present
    source_coverage: dict[str, Any] = {
        "state20_present": True,
        "state25_present": state25.present,
        "partial_source_coverage": partial,
        "missing_state25_allowed": partial and allow_missing_state25,
        "rows_by_source": source_rows,
        "credited_observed_time_s_by_source": source_credited,
        "gap_seconds_by_source": source_gaps,
        "total_rows": total_rows,
        "first_monotonic_s": first_t,
        "last_monotonic_s": last_t,
        "first_wall_time_s": first_wall_t,
        "last_wall_time_s": last_wall_t,
    }
    manifest_payload: dict[str, Any] = {
        "schema": "stars_ft_bias_shadow/input_manifest_v1",
        "run_id": rooted.name,
        "files": [_manifest_file(item) for item in snapshots],
        "source_coverage": source_coverage,
        "host_non_json_line_count": terminal_evidence["non_json_line_count"],
        "host_non_object_line_count": terminal_evidence["non_object_line_count"],
        "host_blank_line_count": terminal_evidence["blank_line_count"],
        "terminal_evidence": terminal_evidence,
    }
    manifest_hash = sha256_bytes(canonical_json_bytes(manifest_payload))
    return HistoricalRunSnapshot(
        run_dir=rooted,
        run_id=rooted.name,
        baseline=baseline,
        launch_context_identity_sha256=launch_identity,
        launch_context_identity_anchor=launch_anchor,
        terminal_status=terminal_status,
        formal_live_complete=formal_live_complete,
        input_files=snapshots,
        input_manifest=manifest_payload,
        input_manifest_sha256=manifest_hash,
        source_coverage=source_coverage,
        phase_timing_rows=_phase_rows(rooted),
        host_non_json_line_count=terminal_evidence["non_json_line_count"],
        host_non_object_line_count=terminal_evidence["non_object_line_count"],
        host_blank_line_count=terminal_evidence["blank_line_count"],
        terminal_evidence=terminal_evidence,
    )


def verify_inputs_unchanged(
    snapshot: HistoricalRunSnapshot,
    *,
    min_input_age_s: float,
    max_input_file_bytes: int,
    max_total_input_bytes: int,
) -> None:
    """Reject any size/stat/hash change after replay."""

    current = _snapshot_declared_inputs(
        snapshot.run_dir,
        # Age is a preflight admission gate.  Postflight compares the
        # immutable stat/hash tuple and must report mutations as mutations,
        # even when the changed file is necessarily very new.
        min_input_age_s=0.0,
        max_input_file_bytes=max_input_file_bytes,
        max_total_input_bytes=max_total_input_bytes,
    )
    if current != snapshot.input_files:
        raise _error("input_mutated_after_replay", "a declared input changed during replay")


def absolute_wrench(
    logged_wrench: Mapping[str, Any] | Sequence[Any],
    baseline: SoftwareBaseline,
) -> tuple[float, float, float, float, float, float]:
    """Recover an absolute channel without writing any sensor configuration."""

    values = logged_wrench.get("wrench") if isinstance(logged_wrench, dict) else logged_wrench
    wrench = _wrench6(values, "sample.wrench")
    return tuple(
        wrench[index] + baseline.mean_wrench_n_nm[index] for index in range(6)
    )  # type: ignore[return-value]


def load_phase_timings(run_dir: Path) -> list[dict[str, Any]]:
    """Strict compatibility reader for the optional phase-timing sidecar."""

    path = Path(run_dir) / "r008-phase-timings.jsonl"
    if not path.exists():
        return []
    if path.is_symlink():
        raise _error("symlink_input", "r008-phase-timings.jsonl must not be a symlink")
    result: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                raise _error("blank_phase_timing_line", f"phase timing line {line_no} is blank")
            row = parse_strict_json_bytes(raw_line.rstrip(b"\r\n"), label=f"phase timing line {line_no}")
            if not isinstance(row, dict):
                raise _error("phase_timing_row_not_object", f"phase timing line {line_no} is not an object")
            result.append(row)
    return result


__all__ = [
    "DECLARED_INPUTS",
    "HistoricalRunSnapshot",
    "InputFileSnapshot",
    "R008RunDirError",
    "SoftwareBaseline",
    "TRACE_SCHEMAS",
    "TraceSample",
    "absolute_wrench",
    "canonical_json_bytes",
    "iter_trace_samples",
    "load_phase_timings",
    "load_software_baseline",
    "parse_strict_json_bytes",
    "preflight_run_dir",
    "sha256_bytes",
    "verify_inputs_unchanged",
]
