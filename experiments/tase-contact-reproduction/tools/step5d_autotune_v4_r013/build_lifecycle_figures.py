"""Build the two source-backed R013 contact figures.

The loader prefers the v1 full-lifecycle binary artifact.  For historical
phase004 data it falls back to the State20 sidecar plus R008RAW2 PATH bytes and
draws the unobserved intervals as hatching.  It never joins a gap with a
fabricated line and never turns a safe-return boolean into force samples.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import math
import re
from pathlib import Path
import sys
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
import numpy as np


REPO = Path(__file__).resolve().parents[2]
DEFAULT_RUN = REPO / "runs/step5d_autotune_v4_r013/live_20260815_goal035_phase_004"
DEFAULT_OUT = Path("/home/andy/.codex/visualizations/2026/08/15/01a00414-66e4-7411-ab97-648ad71785a2")
ATTEMPTS = (6, 7, 8)
INK = "#211f1c"
MUTED = "#625d55"
GRID = "#d9d4ca"
BG = "#fbfaf7"
PANEL = "#f3f0ea"
COLORS = {6: "#1769aa", 7: "#d97706", 8: "#8b3a8b"}
PHASE_COLORS = {
    "ARM_ENTRY": "#6b7280",
    "CONTACT_SEARCH": "#a65d00",
    "BASELINE": "#7c3aed",
    "PATH": "#1769aa",
    "RETURN": "#2f855a",
    "HOME": "#166534",
    "STOPPED": "#b91c1c",
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for value in [json.loads(line)]
        if isinstance(value, dict)
    ]


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _split_segments(x: np.ndarray, y: np.ndarray, *, max_gap: float) -> list[tuple[np.ndarray, np.ndarray]]:
    if len(x) < 2:
        return [(x, y)]
    cuts = np.flatnonzero(np.diff(x) > max_gap) + 1
    return list(zip(np.split(x, cuts), np.split(y, cuts), strict=True))


def _plot_segments(ax: Any, x: np.ndarray, y: np.ndarray, *, color: str, lw: float = 1.1, alpha: float = 0.9, max_gap: float = 0.25) -> None:
    finite = np.isfinite(x) & np.isfinite(y)
    x = x[finite]
    y = y[finite]
    for xs, ys in _split_segments(x, y, max_gap=max_gap):
        if len(xs):
            ax.plot(xs, ys, color=color, lw=lw, alpha=alpha, solid_capstyle="round")


def _style(ax: Any) -> None:
    ax.set_facecolor(BG)
    ax.grid(True, axis="y", color=GRID, linewidth=0.65, alpha=0.78)
    ax.grid(False, axis="x")
    ax.tick_params(colors=MUTED, labelsize=8.2)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#8a8378")
    ax.spines["bottom"].set_color("#8a8378")


def _strategy_from_script(path: Path) -> dict[str, float]:
    text = path.read_text(encoding="utf-8")

    def read(name: str, fallback: float) -> float:
        match = re.search(rf"local\s+{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)", text)
        return fallback if match is None else float(match.group(1))

    return {
        "near_start_travel_m": read("d_near_start_travel_m", 0.011029311),
        "far_force_n": read("f_far_n", 0.300000000),
        "force_fuse_n": read("force_fuse_n", 50.000000000),
        "far_speed_m_s": read("v_far_m_s", 0.005000000),
        "near_speed_m_s": read("v_near_m_s", 0.000200000),
        "far_accel_m_s2": read("far_accel_m_s2", 0.010000000),
        "near_accel_m_s2": read("near_accel_m_s2", 0.005000000),
        "travel_cap_m": 0.025,
        "timeout_s": 90.0,
        "normal_trigger_n": 0.5,
        "norm_trigger_n": 0.7,
        "confirm_s": 0.08,
    }


@dataclass
class AttemptData:
    attempt: int
    kind: str
    source: str
    coverage: str
    search_t: np.ndarray
    search_force: np.ndarray
    search_norm: np.ndarray
    search_travel_mm: np.ndarray
    search_speed_mm_s: np.ndarray
    search_trigger_index: int | None
    path_t: np.ndarray
    path_force: np.ndarray
    full_t: np.ndarray | None = None
    full_force: np.ndarray | None = None
    full_phase: np.ndarray | None = None
    full_phase_code: np.ndarray | None = None
    lifecycle_receipt: dict[str, Any] | None = None

    @property
    def search_duration_s(self) -> float:
        return float(self.search_t[-1] - self.search_t[0]) if len(self.search_t) else math.nan

    @property
    def handoff_gap_s(self) -> float:
        if not len(self.search_t) or not len(self.path_t):
            return math.nan
        return float(-self.search_t[-1])


def _raw_path_for_attempt(run: Path, attempt: int) -> tuple[np.ndarray, np.ndarray]:
    if str(REPO / "tools") not in sys.path:
        sys.path.insert(0, str(REPO / "tools"))
    from step5d_autotune_v4_r008.raw_force_binary_v2 import detect_and_decode

    metadata_path = None
    for candidate in sorted((run / "raw_force_evidence").glob("*.json")):
        if int(_read_json(candidate).get("attempt_sequence", -1)) == attempt:
            metadata_path = candidate
            break
    if metadata_path is None:
        return np.array([]), np.array([])
    rows = detect_and_decode(metadata_path.with_suffix(".r008raw").read_bytes(), include_audit=False)
    return (
        np.asarray([_finite(row.get("path_time_s")) for row in rows], dtype=float),
        np.asarray([_finite(row.get("filtered_normal_n")) for row in rows], dtype=float),
    )


def _search_rows(run: Path, attempt: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int | None]:
    rows = [
        row
        for row in _read_jsonl(run / "r008-state20-search-trace.jsonl")
        if int(row.get("attempt_ordinal", -1)) == attempt
    ]
    rows.sort(key=lambda row: _finite(row.get("monotonic_s")))
    if not rows:
        empty = np.array([])
        return empty, empty, empty, empty, None
    t_abs = np.asarray([_finite(row.get("monotonic_s")) for row in rows], dtype=float)
    t = t_abs - t_abs[0]
    z0 = _finite(rows[0].get("tcp_pose_m_rad", [math.nan] * 6)[2])
    travel = np.asarray(
        [(z0 - _finite(row.get("tcp_pose_m_rad", [math.nan] * 6)[2])) * 1000.0 for row in rows],
        dtype=float,
    )
    force = np.asarray([_finite(row.get("filtered_normal_n")) for row in rows], dtype=float)
    norm = np.asarray([_finite(row.get("force_norm_n")) for row in rows], dtype=float)
    if len(t) >= 2:
        speed = np.gradient(travel, t, edge_order=1)
    else:
        speed = np.full_like(travel, math.nan)
    trigger = np.flatnonzero((force >= 0.5) | (norm >= 0.7))
    return t, force, norm, travel, speed, int(trigger[0]) if len(trigger) else None


def _load_full_attempt(run: Path, attempt: int) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    if str(REPO / "tools") not in sys.path:
        sys.path.insert(0, str(REPO / "tools"))
    from step5d_autotune_v4_r013.lifecycle_trace import load_lifecycle_artifact

    for receipt_path in sorted(run.glob(f"*-{attempt:06d}-*.r013life.json")):
        receipt = _read_json(receipt_path)
        artifact = Path(str(receipt.get("artifact_path", "")))
        if not artifact.is_absolute():
            artifact = run / artifact
        if artifact.is_file():
            _metadata, rows = load_lifecycle_artifact(artifact)
            return receipt, rows
    # Also support the ordinary ordinal-first name without requiring a glob
    # shape if a future execution_id contains unusual punctuation.
    for receipt_path in sorted(run.glob("*.r013life.json")):
        receipt = _read_json(receipt_path)
        if int(receipt.get("ordinal", -1)) != attempt:
            continue
        artifact = Path(str(receipt.get("artifact_path", "")))
        if not artifact.is_absolute():
            artifact = run / artifact
        if artifact.is_file():
            _metadata, rows = load_lifecycle_artifact(artifact)
            return receipt, rows
    return None


def load_attempts(run: Path, strategy_script: Path) -> list[AttemptData]:
    strategy = _strategy_from_script(strategy_script)
    del strategy  # parsed in main and rendered in the strategy figure
    kinds: dict[int, str] = {}
    for path in sorted((run / "raw_force_evidence").glob("*.json")):
        meta = _read_json(path)
        kinds[int(meta.get("attempt_sequence", -1))] = str(meta.get("kind", "UNKNOWN"))
    attempts: list[AttemptData] = []
    for attempt in ATTEMPTS:
        full = _load_full_attempt(run, attempt)
        if full is not None:
            receipt, rows = full
            rows = sorted(rows, key=lambda row: float(row["monotonic_s"]))
            if rows:
                t0 = _finite(rows[0].get("monotonic_s"))
                t = np.asarray([_finite(row.get("monotonic_s")) - t0 for row in rows], dtype=float)
                force = np.asarray([_finite(row.get("filtered_normal_n")) for row in rows], dtype=float)
                phase = np.asarray([str(row.get("phase", "UNKNOWN")) for row in rows], dtype=object)
                phase_code = np.asarray([int(row.get("phase_code", 0)) for row in rows], dtype=int)
                search_mask = phase_code == 20
                search_t = t[search_mask]
                search_force = force[search_mask]
                search_norm = np.asarray([_finite(row.get("force_norm_n")) for row in rows], dtype=float)[search_mask]
                pose_z = np.asarray([_finite(row.get("pose", [math.nan] * 6)[2]) for row in rows], dtype=float)
                search_travel = (pose_z[search_mask][0] - pose_z[search_mask]) * 1000.0 if search_mask.any() else np.array([])
                search_speed = np.gradient(search_travel, search_t, edge_order=1) if len(search_t) >= 2 else np.full_like(search_travel, math.nan)
                trigger = np.flatnonzero((search_force >= 0.5) | (search_norm >= 0.7))
                attempts.append(
                    AttemptData(
                        attempt=attempt,
                        kind=str(receipt.get("kind", kinds.get(attempt, "UNKNOWN"))),
                        source="r013life binary",
                        coverage=str(receipt.get("status", "unknown")),
                        search_t=search_t,
                        search_force=search_force,
                        search_norm=search_norm,
                        search_travel_mm=search_travel,
                        search_speed_mm_s=search_speed,
                        search_trigger_index=int(trigger[0]) if len(trigger) else None,
                        path_t=t[phase_code == 25],
                        path_force=force[phase_code == 25],
                        full_t=t,
                        full_force=force,
                        full_phase=phase,
                        full_phase_code=phase_code,
                        lifecycle_receipt=receipt,
                    )
                )
                continue

        search_t0, search_force, search_norm, travel, speed, trigger = _search_rows(run, attempt)
        path_t, path_force = _raw_path_for_attempt(run, attempt)
        # Align legacy SEARCH to PATH entry.  Negative search times are
        # measured from the two source clocks; the gap itself remains blank.
        search_t = search_t0.copy()
        if len(search_t) and len(path_t):
            search_rows = [
                row
                for row in _read_jsonl(run / "r008-state20-search-trace.jsonl")
                if int(row.get("attempt_ordinal", -1)) == attempt
            ]
            search_rows.sort(key=lambda row: _finite(row.get("monotonic_s")))
            path_sidecar = [
                row
                for row in _read_jsonl(run / "r008-state25-path-trace.jsonl")
                if int(row.get("attempt_ordinal", -1)) == attempt
            ]
            path_sidecar.sort(key=lambda row: _finite(row.get("monotonic_s")))
            if search_rows and path_sidecar:
                path_zero = _finite(path_sidecar[0].get("monotonic_s"))
                search_t = np.asarray([_finite(row.get("monotonic_s")) - path_zero for row in search_rows], dtype=float)
        attempts.append(
            AttemptData(
                attempt=attempt,
                kind=kinds.get(attempt, "UNKNOWN"),
                source="State20 10 Hz + R008RAW2 PATH",
                coverage="partial_pre_search_and_return_home",
                search_t=search_t,
                search_force=search_force,
                search_norm=search_norm,
                search_travel_mm=travel,
                search_speed_mm_s=speed,
                search_trigger_index=trigger,
                path_t=path_t,
                path_force=path_force,
            )
        )
    return attempts


def build_lifecycle_png(attempts: list[AttemptData], output: Path) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.dpi": 160, "savefig.dpi": 220})
    fig, axes = plt.subplots(3, 1, figsize=(15.6, 10.8), gridspec_kw={"hspace": 0.30})
    fig.patch.set_facecolor(PANEL)
    for ax, data in zip(axes, attempts, strict=True):
        _style(ax)
        if data.full_t is not None and data.full_force is not None and data.full_phase is not None:
            t = data.full_t
            force = data.full_force
            for phase_name in sorted(set(data.full_phase), key=str):
                mask = data.full_phase == phase_name
                _plot_segments(ax, t[mask], force[mask], color=PHASE_COLORS.get(str(phase_name), "#555555"), lw=0.8, max_gap=0.020)
            x_max = max(1.0, float(np.nanmax(t)))
            source_text = f"{data.source} · {data.coverage} · {len(t):,} rows"
        else:
            _plot_segments(ax, data.search_t, data.search_force, color="#a65d00", lw=1.3, max_gap=0.25)
            # Keep all raw PATH times and values; a raster stride only reduces
            # overdraw and does not change the underlying source coverage.
            stride = max(1, len(data.path_t) // 9000)
            _plot_segments(ax, data.path_t[::stride], data.path_force[::stride], color="#1769aa", lw=0.72, max_gap=0.2)
            x_max = 65.0
            if len(data.search_t):
                ax.axvspan(float(data.search_t[-1]), 0.0, facecolor="#f1f2f0", edgecolor="#aaa49b", hatch="////", alpha=0.56)
                ax.text((float(data.search_t[-1]) + 0.0) / 2.0, 0.11, "unobserved\nhandoff", transform=ax.get_xaxis_transform(), ha="center", va="center", fontsize=7.2, color="#77716a")
            ax.axvspan(60.0, x_max, facecolor="#e5e7eb", edgecolor="#9ca3af", hatch="\\\\", alpha=0.42)
            ax.text(62.5, 0.86, "RETURN/HOME\nforce rows absent", transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=7.6, color="#6b3b2f", fontweight="bold")
            source_text = f"{data.source} · {data.coverage} · SEARCH {len(data.search_t)} + PATH {len(data.path_t):,} rows"
        ax.axvline(0.0, color=MUTED, lw=0.8)
        ax.axvline(60.0, color=MUTED, lw=0.8)
        ax.axhline(5.0, color="#b94a3a", linestyle="--", lw=0.9)
        ax.set_xlim(float(data.search_t[0]) - 0.25 if len(data.search_t) else -1.0, x_max)
        ax.set_ylim(-1.0, 7.5)
        ax.set_ylabel("filtered normal force (N)", fontsize=8.6)
        ax.set_title(f"attempt {data.attempt} · {data.kind} · {source_text}", loc="left", fontsize=10.2, color=INK, fontweight="bold", pad=5)
        ax.text(0.0, 0.98, "PATH entry", transform=ax.get_xaxis_transform(), ha="left", va="top", fontsize=7.8, color="#1769aa", fontweight="bold")
        ax.text(60.0, 0.98, "PATH end", transform=ax.get_xaxis_transform(), ha="right", va="top", fontsize=7.8, color="#625d55", fontweight="bold")
    axes[-1].set_xlabel("time relative to PATH entry (s); negative values are the observed SEARCH segment", fontsize=9.0)
    handles = [
        Line2D([0], [0], color="#a65d00", lw=1.4, label="observed State20 SEARCH force"),
        Line2D([0], [0], color="#1769aa", lw=1.2, label="observed PATH force (R008RAW2 or r013life)"),
        Line2D([0], [0], color="#b94a3a", lw=0.9, linestyle="--", label="5 N PATH target"),
        Patch(facecolor="#f1f2f0", edgecolor="#aaa49b", hatch="////", alpha=0.56, label="unobserved SEARCH→PATH handoff"),
        Patch(facecolor="#e5e7eb", edgecolor="#9ca3af", hatch="\\\\", alpha=0.42, label="RETURN/HOME force rows absent in phase004"),
    ]
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.985, 0.920), ncol=3, frameon=False, fontsize=7.8)
    fig.suptitle("R013 force lifecycle coverage — raw observed segments only", x=0.065, y=0.985, ha="left", fontsize=17, fontweight="bold", color=INK)
    fig.text(0.065, 0.952, "phase004 attempts 6/7/8 use the same current R013 contact strategy; this figure reports coverage, not mean/best performance.", ha="left", va="top", fontsize=9.2, color=MUTED)
    fig.text(0.065, 0.022, "Coverage finding for phase004: force recording starts at the first State20 row; no pre-SEARCH force source and no RETURN/HOME force rows are present. The blank/hatch regions are not interpolated.", ha="left", va="bottom", fontsize=8.7, color="#6d3b2f")
    fig.subplots_adjust(left=0.072, right=0.985, top=0.865, bottom=0.075)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)


def build_strategy_png(attempts: list[AttemptData], strategy: dict[str, float], output: Path) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.dpi": 160, "savefig.dpi": 220})
    fig = plt.figure(figsize=(15.6, 10.0), facecolor=PANEL)
    grid = fig.add_gridspec(2, 2, left=0.065, right=0.985, top=0.855, bottom=0.145, hspace=0.36, wspace=0.22)
    ax_contract = fig.add_subplot(grid[0, 0])
    ax_speed = fig.add_subplot(grid[0, 1])
    ax_force = fig.add_subplot(grid[1, 0])
    ax_timeline = fig.add_subplot(grid[1, 1])
    for ax in (ax_contract, ax_speed, ax_force, ax_timeline):
        _style(ax)

    near_mm = strategy["near_start_travel_m"] * 1000.0
    far_mm_s = strategy["far_speed_m_s"] * 1000.0
    near_mm_s = strategy["near_speed_m_s"] * 1000.0
    cap_mm = strategy["travel_cap_m"] * 1000.0

    # A: command contract.  The step is the force-low branch; the hatched
    # region makes explicit that force >= 0.3 N can switch to NEAR early.
    ax_contract.step([0.0, near_mm, near_mm, cap_mm], [far_mm_s, far_mm_s, near_mm_s, near_mm_s], where="post", color="#222222", lw=2.2)
    ax_contract.axvspan(0.0, near_mm, color="#1769aa", alpha=0.08)
    ax_contract.axvspan(near_mm, cap_mm, color="#d97706", alpha=0.09)
    ax_contract.axvspan(0.0, near_mm, facecolor="none", edgecolor="#a65d00", hatch="..", alpha=0.34)
    ax_contract.axvline(near_mm, color="#756e64", linestyle="--", lw=1.0)
    ax_contract.text(near_mm / 2.0, far_mm_s * 1.25, "FAR\ntravel < 11.029 mm\nAND force < 0.3 N", ha="center", va="bottom", fontsize=8.0, color="#1769aa", fontweight="bold")
    ax_contract.text((near_mm + cap_mm) / 2.0, near_mm_s * 1.35, "NEAR\notherwise", ha="center", va="bottom", fontsize=8.2, color="#a65d00", fontweight="bold")
    ax_contract.annotate("force condition can enter NEAR\nbefore travel boundary", xy=(near_mm * 0.66, near_mm_s), xytext=(near_mm * 0.90, near_mm_s * 7.0), arrowprops={"arrowstyle": "-", "color": "#a65d00", "lw": 0.8}, fontsize=7.5, color="#a65d00", ha="center")
    ax_contract.set_title("A  Commanded contact-search speed contract", loc="left")
    ax_contract.set_xlabel("State20 travel from search start (mm)")
    ax_contract.set_ylabel("commanded TCP approach speed (mm/s)")
    ax_contract.set_xlim(0.0, cap_mm)
    ax_contract.set_ylim(0.12, 8.0)
    ax_contract.set_yscale("log")
    ax_contract.set_yticks([0.2, 1.0, 5.0])
    ax_contract.set_yticklabels(["0.2", "1", "5"])
    ax_contract.text(0.02, -0.25, f"accel FAR={strategy['far_accel_m_s2']:.3f} m/s² · NEAR={strategy['near_accel_m_s2']:.3f} m/s²", transform=ax_contract.transAxes, fontsize=7.8, color=MUTED)

    # B: observed actual approach speed reconstructed from consecutive pose
    # rows.  This is not the commanded speed and is deliberately labelled.
    for data in attempts:
        _plot_segments(ax_speed, data.search_travel_mm, data.search_speed_mm_s, color=COLORS[data.attempt], lw=1.1, max_gap=0.6)
        if data.search_trigger_index is not None and data.search_trigger_index < len(data.search_travel_mm):
            i = data.search_trigger_index
            ax_speed.scatter([data.search_travel_mm[i]], [data.search_speed_mm_s[i]], color=COLORS[data.attempt], s=28, edgecolor="white", linewidth=0.5, zorder=4)
    ax_speed.axvline(near_mm, color="#756e64", linestyle="--", lw=1.0)
    ax_speed.axhline(far_mm_s, color="#1769aa", linestyle=":", lw=0.9)
    ax_speed.axhline(near_mm_s, color="#a65d00", linestyle=":", lw=0.9)
    ax_speed.set_title("B  Observed approach speed during State20", loc="left")
    ax_speed.set_xlabel("travel from State20 start (mm)")
    ax_speed.set_ylabel("TCP +Z approach speed from pose Δz (mm/s)")
    ax_speed.set_xlim(0.0, max(cap_mm, 14.5))
    ax_speed.set_ylim(-1.0, 7.0)
    ax_speed.text(0.02, -0.25, "10 Hz sidecar finite difference; dots mark first trigger condition when visible; not a command read-back", transform=ax_speed.transAxes, fontsize=7.8, color=MUTED)

    # C: observed force and the two distinct thresholds: FAR/NEAR switch at
    # 0.3 N versus contact confirmation at 0.5/0.7 N for 80 ms.
    for data in attempts:
        _plot_segments(ax_force, data.search_travel_mm, data.search_force, color=COLORS[data.attempt], lw=1.15, max_gap=0.6)
        if data.search_trigger_index is not None and data.search_trigger_index < len(data.search_travel_mm):
            i = data.search_trigger_index
            ax_force.scatter([data.search_travel_mm[i]], [data.search_force[i]], color=COLORS[data.attempt], s=30, edgecolor="white", linewidth=0.5, zorder=4)
    ax_force.axvline(near_mm, color="#756e64", linestyle="--", lw=1.0)
    ax_force.axhline(strategy["far_force_n"], color="#a65d00", linestyle=":", lw=1.0)
    ax_force.axhline(strategy["normal_trigger_n"], color="#b94a3a", linestyle="--", lw=0.9)
    ax_force.set_title("C  Force conditions that drive the speed branch", loc="left")
    ax_force.set_xlabel("travel from State20 start (mm)")
    ax_force.set_ylabel("filtered normal force (N)")
    ax_force.set_xlim(0.0, max(cap_mm, 14.5))
    ax_force.set_ylim(-1.0, 7.5)
    ax_force.text(0.02, 0.96, "0.3 N: FAR eligibility\n0.5 N normal OR 0.7 N norm: contact confirm", transform=ax_force.transAxes, ha="left", va="top", fontsize=7.7, color=MUTED)
    ax_force.text(0.02, -0.25, "force fuse = 50 N · travel cap = 25 mm · timeout = 90 s", transform=ax_force.transAxes, fontsize=7.8, color="#6d3b2f")

    # D: measured timing sequence, with the absence of return force shown as
    # a hatched endpoint rather than a synthetic duration.
    y_positions = np.arange(len(attempts))[::-1]
    for y, data in zip(y_positions, attempts, strict=True):
        search_duration = max(0.0, data.search_duration_s)
        gap = max(0.0, data.handoff_gap_s)
        ax_timeline.barh(y, search_duration, left=0.0, height=0.42, color="#d97706", alpha=0.82)
        ax_timeline.barh(y, gap, left=search_duration, height=0.42, color="#d1d5db", edgecolor="#9ca3af", hatch="////", alpha=0.8)
        ax_timeline.barh(y, 60.0, left=search_duration + gap, height=0.42, color="#1769aa", alpha=0.78)
        ax_timeline.barh(y, 5.0, left=search_duration + gap + 60.0, height=0.42, color="#e5e7eb", edgecolor="#9ca3af", hatch="\\\\", alpha=0.75)
        ax_timeline.text(search_duration / 2.0, y, "SEARCH", ha="center", va="center", fontsize=7.4, color="white", fontweight="bold")
        ax_timeline.text(search_duration + gap + 30.0, y, "PATH 60 s", ha="center", va="center", fontsize=7.4, color="white", fontweight="bold")
        ax_timeline.text(search_duration + gap + 62.5, y, "RETURN/HOME\nforce absent", ha="center", va="center", fontsize=6.8, color="#6d3b2f", fontweight="bold")
        ax_timeline.text(search_duration + 0.1, y - 0.27, f"handoff gap {gap:.2f} s", ha="left", va="top", fontsize=7.0, color=MUTED)
    ax_timeline.set_yticks(y_positions, [f"attempt {data.attempt}" for data in attempts])
    ax_timeline.set_title("D  Observed sequence and recording boundary", loc="left")
    ax_timeline.set_xlabel("seconds from first State20 SEARCH sample; endpoint hatch is a coverage marker, not measured return duration")
    ax_timeline.set_xlim(0.0, max((max(0.0, d.search_duration_s) + max(0.0, d.handoff_gap_s) + 65.0 for d in attempts), default=65.0))
    ax_timeline.set_ylim(-0.65, len(attempts) - 0.35)
    ax_timeline.grid(False, axis="y")

    legend = [Line2D([0], [0], color=COLORS[a], lw=2.0, label=f"attempt {a} · same R013 strategy") for a in ATTEMPTS]
    legend.extend([
        Line2D([0], [0], color="#222222", lw=2.0, label="commanded FAR→NEAR branch"),
        Patch(facecolor="#d1d5db", edgecolor="#9ca3af", hatch="////", alpha=0.8, label="unobserved handoff"),
        Patch(facecolor="#e5e7eb", edgecolor="#9ca3af", hatch="\\\\", alpha=0.75, label="RETURN/HOME force absent"),
    ])
    fig.legend(handles=legend, loc="upper right", bbox_to_anchor=(0.985, 0.922), ncol=3, frameon=False, fontsize=7.8)
    fig.suptitle("R013 contact-search speed planning and observed execution", x=0.065, y=0.982, ha="left", fontsize=17, fontweight="bold", color=INK)
    fig.text(0.065, 0.948, f"FAR = {far_mm_s:.1f} mm/s when travel < {near_mm:.3f} mm and both force tests < {strategy['far_force_n']:.1f} N; otherwise NEAR = {near_mm_s:.1f} mm/s. Attempts 6/7/8 are the same current strategy, not three new-strategy repeats.", ha="left", va="top", fontsize=9.1, color=MUTED)
    fig.text(0.065, 0.028, "The TP owns the commanded schedule; the plotted speed in B is RTDE/pose-observed actual motion. Contact confirmation is a separate 80 ms force condition and does not equal the FAR/NEAR switch.", ha="left", va="bottom", fontsize=8.6, color="#6d3b2f")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)


def build_html(png: Path, output: Path, *, title: str, note: str) -> None:
    encoded = base64.b64encode(png.read_bytes()).decode("ascii")
    output.write_text(
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>"
        + title
        + "</title><style>body{margin:0;background:#f3f0ea;color:#211f1c;font-family:Arial,Helvetica,sans-serif}main{max-width:1700px;margin:0 auto;padding:20px}img{display:block;width:100%;height:auto}p{color:#625d55;line-height:1.5}</style></head><body><main><h1>"
        + title
        + "</h1><p>"
        + note
        + "</p><img src=\"data:image/png;base64,"
        + encoded
        + "\" alt=\""
        + title
        + "\"></main></body></html>",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    run = args.run.resolve()
    out = args.out.resolve()
    strategy_script = REPO / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r013.script"
    strategy = _strategy_from_script(strategy_script)
    attempts = load_attempts(run, strategy_script)
    if len(attempts) != len(ATTEMPTS):
        raise RuntimeError(f"expected attempts {ATTEMPTS}, got {[a.attempt for a in attempts]}")
    lifecycle_png = out / "r013-full-force-lifecycle.png"
    lifecycle_html = out / "r013-full-force-lifecycle.html"
    strategy_png = out / "r013-contact-speed-strategy.png"
    strategy_html = out / "r013-contact-speed-strategy.html"
    build_lifecycle_png(attempts, lifecycle_png)
    build_strategy_png(attempts, strategy, strategy_png)
    build_html(
        lifecycle_png,
        lifecycle_html,
        title="R013 force lifecycle coverage",
        note="phase004 raw observed segments only. The hatched SEARCH→PATH and RETURN/HOME regions are missing source coverage; no force line is fabricated. Attempts 6/7/8 use the same current R013 strategy.",
    )
    build_html(
        strategy_png,
        strategy_html,
        title="R013 contact-search speed strategy",
        note="The figure separates the TP-commanded FAR/NEAR contract from RTDE/pose-observed actual speed and from the independent force-confirmation condition.",
    )
    manifest = {
        "schema": "step5d.autotune-v4/r013-figure-source-v1",
        "run": str(run),
        "attempts": [
            {
                "attempt": data.attempt,
                "kind": data.kind,
                "source": data.source,
                "coverage": data.coverage,
                "search_rows": int(len(data.search_t)),
                "path_rows": int(len(data.path_t)),
                "search_duration_s": data.search_duration_s,
                "handoff_gap_s": data.handoff_gap_s,
                "lifecycle_receipt": data.lifecycle_receipt,
            }
            for data in attempts
        ],
        "strategy": strategy,
        "outputs": {
            "lifecycle_png": str(lifecycle_png),
            "lifecycle_html": str(lifecycle_html),
            "strategy_png": str(strategy_png),
            "strategy_html": str(strategy_html),
        },
    }
    (out / "r013-figure-source.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for path in (lifecycle_png, lifecycle_html, strategy_png, strategy_html, out / "r013-figure-source.json"):
        print(path)


if __name__ == "__main__":
    main()
