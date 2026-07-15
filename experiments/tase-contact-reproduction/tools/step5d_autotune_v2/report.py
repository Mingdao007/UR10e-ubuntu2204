"""Stable two-table Markdown trial reports."""

from __future__ import annotations

import hashlib
import math
import os
import secrets
from pathlib import Path
from typing import Any, Mapping

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
    resolved_batch = batch_id or current["batch_id"]
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
    incumbent = repository.diagnostic_incumbent(exclude_trial_id=trial_id)
    incumbent_metrics = incumbent.get("metrics", {}) if incumbent else {}
    current_group = current["group_id"]
    incumbent_group = incumbent["group_id"] if incumbent else "无历史基线"
    lines.extend(
        [
            "",
            f"| Metric | 当前 {current_group} | 历史最佳 {incumbent_group} | Δ |",
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


def write_trial_report(
    repository: Repository, *, trial_id: str, output_root: Path
) -> tuple[str, Path]:
    detail = repository.trial_detail(trial_id)
    batch_id = detail["batch_id"] or repository.latest_batch_id()
    content = render_trial_report(
        repository, trial_id=trial_id, batch_id=batch_id
    )
    if output_root.is_symlink():
        raise RepositoryError("report root must not be a symlink")
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{detail['group_id']}_P={detail['p_text']}_I={detail['i_text']}_"
        f"D={detail['d_text']}_{trial_id[:12]}.md"
    )
    path = output_root / filename
    encoded = content.encode("utf-8")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise RepositoryError("immutable trial report already differs")
    else:
        temporary = output_root / (
            f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
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
    repository.add_artifact(
        trial_id=trial_id,
        role="trial_markdown_report",
        path=path,
        sha256=hashlib.sha256(encoded).hexdigest(),
        immutable=True,
    )
    return content, path
