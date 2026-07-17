"""Stable two-table Markdown trial reports."""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Mapping

from .model import canonical_json_bytes
from .repository import Repository, RepositoryError


METRICS = (
    ("Force MAE (N)", "force_mae_n"),
    ("Official objective MAE (N)", "objective_mae_n"),
    ("Complete bins", "complete_bins"),
    ("Correlation", "correlation"),
    ("Lag (s)", "lag_s"),
    ("NRMSE", "nrmse"),
    ("Orientation p95 (rad)", "orientation_p95_rad"),
    ("Orientation max (rad)", "orientation_max_rad"),
    ("Normal-filter saturation", "normal_filter_saturation"),
    ("TP acceleration saturation", "tp_accel_saturation"),
    ("Host-slew saturation", "host_slew_saturation"),
    ("Safe closure", "safe_closure"),
    ("Eligible objective", "eligible_objective"),
)


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "invalid"
        return f"{value:.9g}"
    return str(value).replace("|", "\\|")


def _delta(current: Any, incumbent: Any) -> str:
    if (
        isinstance(current, (int, float))
        and not isinstance(current, bool)
        and isinstance(incumbent, (int, float))
        and not isinstance(incumbent, bool)
    ):
        return f"{float(current) - float(incumbent):+.6g}"
    return "—"


def _coordinates(row: Mapping[str, Any]) -> Mapping[str, Any]:
    import json

    value = row.get("coordinates_json")
    return json.loads(value) if isinstance(value, str) else row.get("coordinates", {})


def render_trial_report(
    repository: Repository,
    *,
    trial_id: str | None = None,
    batch_id: str | None = None,
) -> str:
    if trial_id is None:
        batch_id = batch_id or repository.latest_batch_id()
        if batch_id is None:
            raise RepositoryError("no batch has been imported or enqueued")
        trial_id = repository.latest_trial_id(batch_id=batch_id)
    if trial_id is None:
        raise RepositoryError(f"batch has no trial result: {batch_id}")
    current = repository.trial_detail(trial_id)
    resolved_batch = current["batch_id"]
    if batch_id is not None and batch_id != resolved_batch:
        raise RepositoryError("trial does not belong to the requested batch")
    candidates = (
        repository.list_candidates(batch_id=resolved_batch)
        if resolved_batch is not None
        else [repository.candidate(current["candidate_id"])]
    )
    lines = [
        "| 组 | P | I | D | log2(P) | I 调参 | log2(D) | 状态 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in candidates:
        coordinates = _coordinates(row)
        i_tuning = coordinates.get("i_multiplier")
        if i_tuning is not None:
            i_tuning = f"×{i_tuning}"
        else:
            i_tuning = coordinates.get("log2_i")
        lines.append(
            "| {group} | {p} | {i} | {d} | {lp} | {li} | {ld} | {status} |".format(
                group=_display(row["group_id"]),
                p=_display(row["p_text"]),
                i=_display(row["i_text"]),
                d=_display(row["d_text"]),
                lp=_display(coordinates.get("log2_p")),
                li=_display(i_tuning),
                ld=_display(coordinates.get("log2_d")),
                status=_display(row["status"]),
            )
        )
    current_metrics = (current.get("analysis") or {}).get("metrics") or {}
    incumbent = repository.diagnostic_incumbent(
        deployment_id=current["deployment_id"],
        profile_id=current["profile_id"],
        exclude_trial_id=trial_id,
    )
    if incumbent is None:
        incumbent = repository.historical_diagnostic_reference(
            profile_id=current["profile_id"],
            group_id="G10",
            exclude_trial_id=trial_id,
        )
    incumbent_metrics = incumbent.get("metrics", {}) if incumbent else {}
    current_group = current["group_id"]
    if incumbent is None:
        incumbent_label = "无可用基线"
    elif incumbent.get("historical_reference") is True:
        incumbent_label = f"历史诊断参考 {incumbent['group_id']}（只读）"
    else:
        incumbent_label = f"同 deployment/profile 最佳 {incumbent['group_id']}"
    lines.extend(
        [
            "",
            f"| Metric | 当前 {current_group} | {incumbent_label} | Δ |",
            "|---|---:|---:|---:|",
        ]
    )
    for label, key in METRICS:
        current_value = current_metrics.get(key)
        incumbent_value = incumbent_metrics.get(key)
        lines.append(
            f"| {label} | {_display(current_value)} | {_display(incumbent_value)} | "
            f"{_delta(current_value, incumbent_value)} |"
        )
    return "\n".join(lines) + "\n"


def _report_target(
    repository: Repository, *, trial_id: str, output_root: Path
) -> tuple[dict[str, Any], Path]:
    detail = repository.trial_detail(trial_id)
    if output_root.is_symlink():
        raise RepositoryError("report root must not be a symlink")
    output_root = output_root.resolve()
    filename = (
        f"{detail['group_id']}_P={detail['p_text']}_I={detail['i_text']}_"
        f"D={detail['d_text']}_{trial_id[:12]}.md"
    )
    return detail, output_root / filename


def _read_regular_report(path: Path) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RepositoryError("registered trial report is unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RepositoryError(
                "registered trial report must be one regular non-symlink file"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise RepositoryError("registered trial report ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )
    if identity(before) != identity(after) or len(encoded) != before.st_size:
        raise RepositoryError("registered trial report changed while being read")
    return encoded


def _write_report_file(path: Path, encoded: bytes) -> bool:
    actual = _read_regular_report(path)
    if actual is not None:
        if actual != encoded:
            raise RepositoryError("immutable trial report already differs")
        return False
    output_root = path.parent
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / (
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(
            output_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def _write_trial_report(
    repository: Repository, *, trial_id: str, output_root: Path
) -> tuple[str, Path, bool]:
    _detail, path = _report_target(
        repository, trial_id=trial_id, output_root=output_root
    )
    content = render_trial_report(repository, trial_id=trial_id)
    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    existing = repository.artifact_record_for_trial(
        trial_id, role="trial_markdown_report"
    )
    if existing is not None:
        bound_artifact_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "trial_id": trial_id,
                    "role": "trial_markdown_report",
                    "path": existing["path"],
                    "sha256": existing["sha256"],
                }
            )
        ).hexdigest()
        if (
            not bool(existing["immutable"])
            or existing["path"] != str(path)
            or existing["artifact_id"] != bound_artifact_id
        ):
            raise RepositoryError(
                "registered trial report conflicts with canonical immutable content"
            )
        actual = _read_regular_report(path)
        if actual is not None:
            if hashlib.sha256(actual).hexdigest() != existing["sha256"]:
                raise RepositoryError(
                    "registered trial report bytes do not match its hash"
                )
            try:
                return actual.decode("utf-8"), path, False
            except UnicodeDecodeError as exc:
                raise RepositoryError(
                    "registered trial report is not UTF-8"
                ) from exc
        if existing["sha256"] != digest:
            raise RepositoryError(
                "missing immutable trial report cannot be recovered from changed rendering"
            )
    materialized = _write_report_file(path, encoded)
    actual = _read_regular_report(path)
    if actual is None or hashlib.sha256(actual).hexdigest() != digest:
        raise RepositoryError("registered trial report bytes do not match its hash")
    repository.add_artifact(
        trial_id=trial_id,
        role="trial_markdown_report",
        path=path,
        sha256=digest,
        immutable=True,
    )
    return content, path, materialized or existing is None


def write_trial_report(
    repository: Repository, *, trial_id: str, output_root: Path
) -> tuple[str, Path]:
    content, path, _recovered = _write_trial_report(
        repository, trial_id=trial_id, output_root=output_root
    )
    return content, path


def reconcile_missing_trial_reports(
    repository: Repository, *, deployment_id: str, output_root: Path
) -> list[tuple[str, Path]]:
    """Verify current-epoch reports and recover only deterministic crash cuts."""

    recovered: list[tuple[str, Path]] = []
    for trial_id in repository.completed_trials(deployment_id=deployment_id):
        content, path, changed = _write_trial_report(
            repository, trial_id=trial_id, output_root=output_root
        )
        if changed:
            recovered.append((content, path))
    return recovered
