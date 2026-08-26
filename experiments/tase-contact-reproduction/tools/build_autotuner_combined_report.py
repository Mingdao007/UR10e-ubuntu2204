#!/usr/bin/env python3
"""Build the image-backed V4/V5 weekly HTML report.

The input is a single, cold-derived JSON snapshot.  This builder never
invents points: a plot is emitted only when its corresponding numeric arrays
are present, and every image caption names the source field and statistic
convention.  Raw lifecycle binaries are deliberately excluded from the Mac
delivery ZIP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import zipfile
from typing import Any, Iterable, Mapping, Sequence


REPORT_SCHEMA = "step6.autotune/autotuner-v4-v5-combined-report-v1"
REPORT_VERSION = 1


class CombinedReportError(RuntimeError):
    pass


def _finite_values(value: Any, field: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CombinedReportError(f"{field} must be a numeric sequence")
    result = tuple(float(item) for item in value)
    if not result or any(not math.isfinite(item) for item in result):
        raise CombinedReportError(f"{field} is empty or non-finite")
    return result


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CombinedReportError("combined-report snapshot is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != "step6.autotune/autotuner-v4-v5-combined-input-v1":
        raise CombinedReportError("combined-report snapshot schema differs")
    return value


def _plot(path: Path, title: str, x: Sequence[float], ys: Sequence[Sequence[float]], labels: Sequence[str], ylabel: str) -> None:
    if len(ys) != len(labels) or any(len(y) != len(x) for y in ys):
        raise CombinedReportError(f"plot {title} cardinality differs")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(11, 4.8), dpi=150)
    for y, label in zip(ys, labels, strict=True):
        axis.plot(x, y, linewidth=1.4, label=label)
    axis.set_title(title)
    axis.set_xlabel("ordinal / time (source-defined)")
    axis.set_ylabel(ylabel)
    axis.grid(True, alpha=0.25)
    if len(labels) > 1:
        axis.legend(loc="best")
    figure.tight_layout()
    figure.savefig(path, format="png")
    plt.close(figure)


def _plot_scatter(path: Path, title: str, x: Sequence[float], y: Sequence[float], xlabel: str, ylabel: str) -> None:
    if len(x) != len(y) or not x:
        raise CombinedReportError(f"plot {title} cardinality differs")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(7.8, 5.4), dpi=150)
    axis.scatter(x, y, s=18, alpha=0.8)
    axis.set_title(title)
    axis.set_xlabel(xlabel)
    axis.set_ylabel(ylabel)
    axis.grid(True, alpha=0.25)
    figure.tight_layout()
    figure.savefig(path, format="png")
    plt.close(figure)


def _section_image(rel: str, title: str, caption: str) -> str:
    return (
        f'<figure><img src="{rel}" alt="{title}" loading="lazy">'
        f"<figcaption><strong>{title}</strong> — {caption}</figcaption></figure>"
    )


def _build_images(snapshot: Mapping[str, Any], image_dir: Path) -> list[tuple[str, str, str]]:
    image_dir.mkdir(parents=True, exist_ok=True)
    plots: list[tuple[str, str, str]] = []
    stages = snapshot.get("v4_stages")
    if isinstance(stages, Sequence) and stages:
        x: list[float] = []
        ys: list[list[float]] = []
        labels: list[str] = []
        for stage in stages:
            if not isinstance(stage, Mapping) or not isinstance(stage.get("mae_n"), Sequence):
                continue
            values = _finite_values(stage["mae_n"], "v4_stages.mae_n")
            if not x:
                x = list(range(1, len(values) + 1))
            if len(values) != len(x):
                continue
            ys.append(list(values))
            labels.append(str(stage.get("stage", "V4")))
        if ys:
            name = "v4-convergence.png"
            _plot(image_dir / name, "V4 Stage convergence", x, ys, labels, "sealed [5,60) MAE (N)")
            plots.append((f"images/{name}", "V4 convergence", "Each line is a sealed stage series; no ddof is applied to the per-attempt MAE."))
    timing = snapshot.get("timing_gap_ms")
    if isinstance(timing, Sequence) and timing:
        values = _finite_values(timing, "timing_gap_ms")
        name = "timing-gap.png"
        _plot(image_dir / name, "Timing fresh-gap samples", list(range(1, len(values) + 1)), [values], ["max fresh gap"], "gap (ms)")
        plots.append((f"images/{name}", "Timing gap", "Source: timing_gap_ms; formal 2 ms cadence is separate from the <20 ms hard failure line."))
    trajectory = snapshot.get("figure8_trajectory")
    if isinstance(trajectory, Mapping) and isinstance(trajectory.get("actual_x"), Sequence) and isinstance(trajectory.get("actual_y"), Sequence):
        actual_x = _finite_values(trajectory["actual_x"], "figure8_trajectory.actual_x")
        actual_y = _finite_values(trajectory["actual_y"], "figure8_trajectory.actual_y")
        if len(actual_x) == len(actual_y):
            name = "figure8-trajectory.png"
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            figure, axis = plt.subplots(figsize=(7.4, 6.4), dpi=150)
            axis.plot(actual_x, actual_y, label="actual", linewidth=1.2)
            if isinstance(trajectory.get("commanded_x"), Sequence) and isinstance(trajectory.get("commanded_y"), Sequence):
                command_x = _finite_values(trajectory["commanded_x"], "figure8_trajectory.commanded_x")
                command_y = _finite_values(trajectory["commanded_y"], "figure8_trajectory.commanded_y")
                if len(command_x) == len(command_y):
                    axis.plot(command_x, command_y, label="commanded", linewidth=1.0, alpha=0.75)
            axis.set_title("Figure-8 actual / commanded trajectory")
            axis.set_xlabel("x (m)")
            axis.set_ylabel("y (m)")
            axis.axis("equal")
            axis.grid(True, alpha=0.25)
            axis.legend(loc="best")
            figure.tight_layout()
            figure.savefig(image_dir / name, format="png")
            plt.close(figure)
            plots.append((f"images/{name}", "Figure-8 trajectory", "Source: sealed trajectory arrays; actual and commanded are not interpolated."))
    for field, title, xlabel, ylabel, filename in (
        ("force_curves", "Force curves", "sample / time", "force (N)", "force-curves.png"),
        ("running_mae", "Running MAE", "ordinal", "MAE (N)", "running-mae.png"),
        ("incumbent_evolution", "BO incumbent evolution", "ordinal", "incumbent MAE (N)", "incumbent-evolution.png"),
        ("parameter_evolution", "Candidate parameter evolution", "ordinal", "parameter value", "parameter-evolution.png"),
    ):
        raw = snapshot.get(field)
        if not isinstance(raw, Mapping) or not raw:
            continue
        x_raw = raw.get("x", list(range(1, len(next(iter(raw.values()))) + 1)))
        x = _finite_values(x_raw, f"{field}.x")
        series: list[Sequence[float]] = []
        labels: list[str] = []
        for key, values in raw.items():
            if key == "x":
                continue
            series.append(_finite_values(values, f"{field}.{key}"))
            labels.append(str(key))
        if series:
            name = filename
            _plot(image_dir / name, title, x, series, labels, ylabel)
            plots.append((f"images/{name}", title, f"Source: {field}; x/y values are sealed inputs, with no hidden aggregation."))
    return plots


def build_report(snapshot_path: Path, output_root: Path, *, zip_path: Path | None = None) -> dict[str, Any]:
    snapshot = _load_snapshot(snapshot_path)
    output = Path(output_root).resolve()
    if output.exists() and any(output.iterdir()):
        raise CombinedReportError("combined-report output root must be fresh")
    output.mkdir(parents=True, exist_ok=True)
    plots = _build_images(snapshot, output / "images")
    if len(plots) < 1:
        raise CombinedReportError("report needs at least one real image asset")
    sections = "\n".join(_section_image(*plot) for plot in plots)
    source_sha = str(snapshot.get("source_sha256", ""))
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autotuner V4/V5 combined report</title>
<style>body{{font-family:system-ui,sans-serif;background:#f5f3ee;color:#202020;margin:0}}main{{max-width:1320px;margin:auto;padding:28px}}figure{{background:#fff;padding:14px;border-radius:10px;box-shadow:0 1px 5px #0002;margin:0 0 22px}}img{{display:block;width:100%;height:auto}}figcaption{{padding:10px 4px;line-height:1.45;color:#4a4640}}code{{word-break:break-all}}</style></head>
<body><main><h1>Autotuner V4/V5 combined report</h1><p>Image-backed report generated from one cold-derived snapshot. Source identity: <code>{source_sha}</code></p>{sections}</main></body></html>
"""
    html_path = output / "report.html"
    html_path.write_text(html, encoding="utf-8")
    files = {}
    for path in sorted(p for p in output.rglob("*") if p.is_file()):
        files[str(path.relative_to(output))] = {"sha256": _sha(path), "size": path.stat().st_size}
    manifest_body = {
        "schema": REPORT_SCHEMA,
        "version": REPORT_VERSION,
        "snapshot": str(Path(snapshot_path).resolve()),
        "source_sha256": source_sha,
        "image_count": len(plots),
        "img_tag_count": html.count("<img "),
        "files": files,
        "raw_excluded": True,
    }
    manifest = {**manifest_body, "manifest_sha256": hashlib.sha256(json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if zip_path is not None:
        archive = Path(zip_path).resolve()
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(p for p in output.rglob("*") if p.is_file()):
                bundle.write(path, path.relative_to(output).as_posix())
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--zip", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_report(args.snapshot, args.output_root, zip_path=args.zip), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
