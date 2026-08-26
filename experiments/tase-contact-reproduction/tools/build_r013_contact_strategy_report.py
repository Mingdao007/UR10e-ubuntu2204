"""Build the canonical R013 contact-strategy HTML report.

The report deliberately contains native artifact charts only.  A legacy
phase004 run can show observed SEARCH/PATH points, but it cannot pass the
new full-lifecycle gate without a ``.r013life`` artifact and receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r013.contact_search_strategy import (
    A0_HARD_TWO_STAGE,
    A1_BOUNDED_TANH_SIGMOID,
    ContactSearchStrategy,
    a1_strategy,
)


TARGET_MAE_N = 0.35
ATTEMPTS = (6, 7, 8)
CURRENT_READBACK_RELATIVE = (
    "controller_readback_step5d_r013_goal035_20260815_phase_004/"
    "step5d_strict_rnn_autotune_v4_r013.script"
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source(source_id: str, label: str, path: str, digest: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": source_id,
        "type": "file",
        "path": path,
        "label": label,
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": f"SELECT * FROM read_json_auto('{path}')",
            "description": label,
            "tables_used": [path],
            "executed_at": "2026-08-15T00:00:00Z",
        },
    }
    if digest:
        value["sha256"] = digest
    return value


def _manifest_source(source_id: str, label: str, path: str) -> dict[str, str]:
    return {"id": source_id, "label": label, "path": path}


def _current_readback(root: Path) -> dict[str, Any]:
    """Read the exact phase004 TP constants used by the current A0 run."""

    path = root / "runs" / CURRENT_READBACK_RELATIVE
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    def number(name: str, default: float | None = None) -> float | None:
        match = re.search(
            rf"local\s+{re.escape(name)}\s*=\s*([-+0-9.eE]+)",
            text,
        )
        return default if match is None else float(match.group(1))

    values = {
        "readback_path": str(path),
        "readback_available": path.is_file(),
        "boundary_mm": (number("d_near_start_travel_m") or 0.0) * 1000.0,
        "far_force_n": number("f_far_n"),
        "far_speed_mm_s": (number("v_far_m_s") or 0.0) * 1000.0,
        "near_speed_mm_s": (number("v_near_m_s") or 0.0) * 1000.0,
        "far_accel_m_s2": number("far_accel_m_s2"),
        "near_accel_m_s2": number("near_accel_m_s2"),
    }
    return values


def _schedule_rows_for_strategies(
    strategies: tuple[ContactSearchStrategy, ...],
    *,
    max_travel_m: float = 0.025,
    points: int = 101,
) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for index in range(points):
        travel = max_travel_m * index / (points - 1)
        for strategy in strategies:
            rows.append(
                {
                    "travel_mm": travel * 1000.0,
                    "speed_mm_s": strategy.speed_m_s(travel) * 1000.0,
                    "series": strategy.strategy_id,
                    "boundary_mm": strategy.boundary_m * 1000.0,
                    "transition_width_mm": strategy.transition_width_m * 1000.0,
                }
            )
    return rows


def _observed_force_rows(run_dir: Path, *, attempt: int) -> list[dict[str, Any]]:
    search = _read_jsonl(run_dir / "r008-state20-search-trace.jsonl")
    path = _read_jsonl(run_dir / "r008-state25-path-trace.jsonl")
    search = [row for row in search if row.get("attempt_ordinal") == attempt]
    path = [row for row in path if row.get("attempt_ordinal") == attempt]
    if not search and not path:
        return []
    first_path_rtde = min(
        (float(row["rtde_timestamp_s"]) for row in path if row.get("rtde_timestamp_s") is not None),
        default=0.0,
    )
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(search):
        rows.append(
            {
                "attempt_sequence": attempt,
                "phase": "SEARCH observed",
                "series": "SEARCH observed",
                "sample_index": index,
                "elapsed_s": float(row["rtde_timestamp_s"]) - first_path_rtde,
                "force_n": float(row["filtered_normal_n"]),
                "normal_load_n": float(row["normal_load_n"]),
                "sensor_fresh": bool(row["sensor_fresh"]),
                "source_row": "r008-state20-search-trace.jsonl",
            }
        )
    for index, row in enumerate(path):
        rows.append(
            {
                "attempt_sequence": attempt,
                "phase": "PATH observed",
                "series": "PATH observed",
                "sample_index": index,
                "elapsed_s": float(row["runtime_strategy_path_time_s"]),
                "force_n": float(row["filtered_normal_n"]),
                "normal_load_n": float(row["normal_load_n"]),
                "sensor_fresh": bool(row["sensor_fresh"]),
                "source_row": "r008-state25-path-trace.jsonl",
            }
        )
    return sorted(rows, key=lambda row: (str(row["series"]), float(row["elapsed_s"])))


def _coverage_rows(run_dir: Path, diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    receipts = {
        int(value.get("ordinal")): value
        for path in run_dir.rglob("*.r013life.json")
        for value in [_read_json(path)]
        if value.get("ordinal") is not None
    }
    state21_path = run_dir / "r013-state21-baseline-trace.jsonl"
    state21_rows = (
        _read_jsonl(state21_path) if state21_path.is_file() else []
    )
    rows = []
    for attempt in ATTEMPTS:
        receipt = receipts.get(attempt)
        if receipt is None:
            rows.append(
                {
                    "attempt_sequence": attempt,
                    "lifecycle_status": "INCOMPLETE",
                    "coverage_complete": False,
                    "capture_started_before_motion": "not evidenced",
                    "path_duration_s": None,
                    "path_sample_count": None,
                    "baseline_sample_count": None,
                    "terminal_home": "legacy sidecars only",
                    "handoff_status": "not evidenced",
                    "reason": "no .r013life receipt; SEARCH→PATH→RETURN/HOME is not one continuous artifact",
                }
            )
            continue
        handoff = receipt.get("handoff")
        baseline_count = sum(
            1 for row in state21_rows if row.get("attempt_ordinal") == attempt
        )
        lifecycle_complete = bool(receipt.get("coverage_complete"))
        handoff_complete = bool(
            isinstance(handoff, Mapping) and handoff.get("continuity_ok") is True
        )
        rows.append(
            {
                "attempt_sequence": attempt,
                "lifecycle_status": (
                    "COMPLETE" if lifecycle_complete and handoff_complete else "INCOMPLETE"
                ),
                "coverage_complete": lifecycle_complete and handoff_complete,
                "capture_started_before_motion": str(receipt.get("capture_started_before_motion")),
                "path_duration_s": receipt.get("path_duration_s"),
                "path_sample_count": receipt.get("path_sample_count"),
                "baseline_sample_count": baseline_count,
                "terminal_home": str(receipt.get("home_verified")),
                "handoff_status": (
                    str(handoff.get("status")) if isinstance(handoff, Mapping) else "not evidenced"
                ),
                "reason": "; ".join(receipt.get("errors") or [])
                or ("handoff continuity incomplete" if not handoff_complete else "none"),
            }
        )
    return rows


def _chart(
    chart_id: str,
    title: str,
    dataset: str,
    source_id: str,
    x_field: str,
    x_label: str,
    y_field: str,
    y_label: str,
    *,
    color_field: str | None = None,
    max_rows: int = 2000,
    reference_lines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    encodings: dict[str, Any] = {
        "x": {"field": x_field, "type": "quantitative", "label": x_label},
        "y": {"field": y_field, "type": "quantitative", "label": y_label},
        "tooltip": [
            {"field": x_field, "type": "quantitative", "label": x_label},
            {"field": y_field, "type": "quantitative", "label": y_label},
        ],
    }
    if color_field:
        encodings["color"] = {"field": color_field, "type": "nominal", "label": "Series"}
    result: dict[str, Any] = {
        "id": chart_id,
        "title": title,
        "subtitle": "Native chart from bounded snapshot rows; missing intervals are not interpolated.",
        "intent": "diagnostic",
        "type": "line",
        "dataset": dataset,
        "sourceId": source_id,
        "encodings": encodings,
        "xAxisTitle": x_label,
        "yAxisTitle": y_label,
        "valueFormat": "number",
        "maxRows": max_rows,
    }
    if reference_lines:
        result["referenceLines"] = reference_lines
    return result


def build_artifact(run_dir: Path, output_dir: Path) -> dict[str, Any]:
    snapshot = _read_json(run_dir / "r013_goal035_report_source_snapshot.json")
    diagnostics = _read_json(run_dir / "r013_goal035_best_attempt6_diagnostics.json")
    summary = snapshot["summary"]
    generated_at = "2026-08-15T00:00:00Z"
    best_attempt = int(diagnostics["attempt_sequence"])
    readback = _current_readback(ROOT)
    observed_rows = _observed_force_rows(run_dir, attempt=best_attempt)
    coverage_rows = _coverage_rows(run_dir, diagnostics)
    a0_current = ContactSearchStrategy(
        strategy_id=A0_HARD_TWO_STAGE,
        near_speed_m_s=float(readback.get("near_speed_mm_s") or 0.5) / 1000.0,
    )
    schedule = _schedule_rows_for_strategies((a0_current, a1_strategy()), points=101)
    status = "INCOMPLETE" if not all(row["coverage_complete"] for row in coverage_rows) else "COMPLETE"

    sources_manifest = [
        _manifest_source("r013_snapshot", "Current R013 baseline snapshot", "r013_goal035_report_source_snapshot.json"),
        _manifest_source("r013_diagnostics", "Best attempt force diagnostics", "r013_goal035_best_attempt6_diagnostics.json"),
        _manifest_source("r013_search", "Observed SEARCH sidecar", "r008-state20-search-trace.jsonl"),
        _manifest_source("r013_path", "Observed PATH sidecar", "r008-state25-path-trace.jsonl"),
        _manifest_source("r013_lifecycle", "Canonical full lifecycle receipts", "*.r013life.json"),
        _manifest_source("r013_strategy", "Typed A0/A1 schedule contract", "tools/step5d_autotune_v4_r013/contact_search_strategy.py"),
        _manifest_source("r013_readback", "Current phase004 controller read-back", CURRENT_READBACK_RELATIVE),
    ]
    sources = [
        _source("r013_snapshot", "Current R013 baseline snapshot", "r013_goal035_report_source_snapshot.json", _sha256(run_dir / "r013_goal035_report_source_snapshot.json")),
        _source("r013_diagnostics", "Best attempt force diagnostics", "r013_goal035_best_attempt6_diagnostics.json", _sha256(run_dir / "r013_goal035_best_attempt6_diagnostics.json")),
        _source("r013_search", "Observed SEARCH sidecar", "r008-state20-search-trace.jsonl", _sha256(run_dir / "r008-state20-search-trace.jsonl")),
        _source("r013_path", "Observed PATH sidecar", "r008-state25-path-trace.jsonl", _sha256(run_dir / "r008-state25-path-trace.jsonl")),
        _source("r013_lifecycle", "Canonical full lifecycle receipts", "*.r013life.json"),
        _source("r013_strategy", "Typed A0/A1 schedule contract", "tools/step5d_autotune_v4_r013/contact_search_strategy.py"),
        _source("r013_readback", "Current phase004 controller read-back", CURRENT_READBACK_RELATIVE),
    ]
    cards = [
        {
            "id": "lifecycle_gate",
            "description": "从 motion 前到 Home 的单一连续 lifecycle receipt；缺任一阶段就不能 admission。",
            "dataset": "report_status",
            "sourceId": "r013_lifecycle",
            "metrics": [{"label": "Lifecycle gate", "field": "status"}],
        },
        {
            "id": "observed_force_rows",
            "description": "当前 legacy sidecar 中实际存在的 force rows；不包含任何补值。",
            "dataset": "report_status",
            "sourceId": "r013_lifecycle",
            "metrics": [{"label": "Observed rows", "field": "observed_force_rows"}],
        },
        {
            "id": "handoff_policy",
            "description": "本轮只改变 State21→State25 的 handoff，不改变 far/near、P/I/damping 或 sigmoid。",
            "dataset": "handoff_contract",
            "sourceId": "r013_strategy",
            "metrics": [{"label": "Policy", "field": "policy"}],
        },
    ]
    charts = [
        _chart(
            "observed_force_lifecycle",
            "Figure 3｜Attempt 6 observed force segments",
            "observed_force",
            "r013_search",
            "elapsed_s",
            "Elapsed from PATH start (s)",
            "force_n",
            "Filtered normal force (N)",
            color_field="series",
            reference_lines=[{"axis": "y", "value": 5.0, "label": "Target 5 N", "color": "neutral", "lineStyle": "dashed"}],
        ),
        _chart(
            "contact_search_schedule",
            "Figure 4｜A0 hard switch versus A1 bounded sigmoid",
            "strategy_schedule",
            "r013_strategy",
            "travel_mm",
            "Search travel (mm)",
            "speed_mm_s",
            "Command speed (mm/s)",
            color_field="series",
            max_rows=300,
        ),
    ]
    tables = [
        {
            "id": "coverage_table",
            "title": "Full lifecycle coverage gate",
            "subtitle": "The legacy phase004 rows remain visible, but are not promoted to complete lifecycle evidence.",
            "dataset": "coverage",
            "sourceId": "r013_lifecycle",
            "columns": [
                {"field": "attempt_sequence", "label": "Attempt", "type": "number"},
                {"field": "lifecycle_status", "label": "Receipt", "type": "text"},
                {"field": "coverage_complete", "label": "Complete", "type": "text"},
                {"field": "capture_started_before_motion", "label": "Before motion", "type": "text"},
                {"field": "path_duration_s", "label": "PATH duration (s)", "type": "number"},
                {"field": "path_sample_count", "label": "PATH samples", "type": "number"},
                {"field": "baseline_sample_count", "label": "State21 samples", "type": "number"},
                {"field": "terminal_home", "label": "Home", "type": "text"},
                {"field": "handoff_status", "label": "Handoff", "type": "text"},
                {"field": "reason", "label": "Reason", "type": "text"},
            ],
        },
        {
            "id": "strategy_table",
            "title": "A0/A1 strategy contract",
            "subtitle": "Only the force-search velocity schedule changes; candidate/runtime controller gains stay fixed.",
            "dataset": "strategy_contract",
            "sourceId": "r013_strategy",
            "columns": [
                {"field": "strategy_id", "label": "Strategy", "type": "text"},
                {"field": "boundary_mm", "label": "Old boundary (mm)", "type": "number"},
                {"field": "transition_width_mm", "label": "Transition width (mm)", "type": "number"},
                {"field": "far_speed_mm_s", "label": "Far speed (mm/s)", "type": "number"},
                {"field": "near_speed_mm_s", "label": "Near speed (mm/s)", "type": "number"},
                {"field": "near_accel_m_s2", "label": "Near accel (m/s²)", "type": "number"},
                {"field": "live_evidence", "label": "Live evidence", "type": "text"},
            ],
        },
        {
            "id": "handoff_table",
            "title": "State21 → State25 freeze-carry contract",
            "subtitle": "Design contract for the next canary; it is not a measured handoff until a complete receipt exists.",
            "dataset": "handoff_contract",
            "sourceId": "r013_strategy",
            "columns": [
                {"field": "phase", "label": "Phase", "type": "text"},
                {"field": "controller_update", "label": "Controller update", "type": "text"},
                {"field": "path_clock", "label": "PATH clock", "type": "text"},
                {"field": "normal_state", "label": "Normal state", "type": "text"},
                {"field": "evidence", "label": "Receipt evidence", "type": "text"},
            ],
        },
        {
            "id": "readback_table",
            "title": "Current phase004 controller read-back",
            "subtitle": "The .2 mm/s value belongs to the earlier offline A1 package; it is not current live evidence.",
            "dataset": "current_parameters",
            "sourceId": "r013_readback",
            "columns": [
                {"field": "parameter", "label": "Parameter", "type": "text"},
                {"field": "value", "label": "Value", "type": "text"},
                {"field": "evidence", "label": "Evidence", "type": "text"},
            ],
        },
    ]
    strategy_contract = [
        {
            "strategy_id": A0_HARD_TWO_STAGE,
            "boundary_mm": 11.029311,
            "transition_width_mm": 0.0,
            "far_speed_mm_s": 5.0,
            "near_speed_mm_s": float(readback.get("near_speed_mm_s") or 0.5),
            "near_accel_m_s2": float(readback.get("near_accel_m_s2") or 0.005),
            "live_evidence": "phase004 controller read-back",
        },
        {
            "strategy_id": A1_BOUNDED_TANH_SIGMOID,
            "boundary_mm": 11.029311,
            "transition_width_mm": 10.0,
            "far_speed_mm_s": 5.0,
            "near_speed_mm_s": 0.2,
            "near_accel_m_s2": None,
            "live_evidence": "not run; offline package only",
        },
    ]
    handoff_contract = [
        {
            "policy": "freeze_carry_v1",
            "phase": "State21 BASELINE",
            "controller_update": "baseline outer-loop only",
            "path_clock": "not started",
            "normal_state": "retain last valid integral + normal velocity",
            "evidence": "phase counts + State21 sidecar",
        },
        {
            "policy": "freeze_carry_v1",
            "phase": "State25 first real tick",
            "controller_update": "start PATH synchronously",
            "path_clock": "start at 0 s",
            "normal_state": "carry baseline state; no blind reset",
            "evidence": "handoff continuity deltas = 0",
        },
        {
            "policy": "freeze_carry_v1",
            "phase": "State25 subsequent ticks",
            "controller_update": "PATH controller/path clock update",
            "path_clock": "authoritative runtime clock",
            "normal_state": "normal state evolves from carried value",
            "evidence": "per-row fields + lifecycle receipt",
        },
    ]
    current_parameters = [
        {"parameter": "FAR travel boundary", "value": f"{readback['boundary_mm']:.6f} mm", "evidence": "phase004 controller read-back"},
        {"parameter": "FAR branch", "value": f"{readback['far_speed_mm_s']:.3f} mm/s; accel {readback['far_accel_m_s2']:.3f} m/s²", "evidence": "travel < boundary AND force < 0.3 N"},
        {"parameter": "NEAR branch", "value": f"{readback['near_speed_mm_s']:.3f} mm/s; accel {readback['near_accel_m_s2']:.3f} m/s²", "evidence": "otherwise"},
        {"parameter": "Contact confirmation", "value": "normal ≥ 0.5 N OR norm ≥ 0.7 N for 80 ms", "evidence": "separate from FAR/NEAR switch"},
        {"parameter": "Safety bounds", "value": "force fuse 50 N; max travel 25 mm", "evidence": "phase004 controller read-back"},
    ]
    blocks = [
        {"id": "title", "type": "markdown", "body": "# R013 Contact Transient Strategy Report"},
        {
            "id": "technical_summary",
            "type": "markdown",
            "sourceId": "r013_snapshot",
            "body": (
                "## 技术结论\n\n"
                f"**当前 phase004 live read-back 的 A0 是 FAR 5.000 mm/s、NEAR {readback['near_speed_mm_s']:.3f} mm/s，边界 {readback['boundary_mm']:.6f} mm；本轮没有改这组 far/near 参数。**\n\n"
                f"**完整性 gate 当前为 {status}。** 旧 run 只有 SEARCH/PATH sidecars，没有 R013 `.r013life` receipt 覆盖从 motion 前、接触搜索、State21、60 s PATH、RETURN 到 Home 的单一连续轨迹，因此本报告不把它升级为完整 force trajectory。"
            ),
        },
        {"id": "headline_metrics", "type": "metric-strip", "cardIds": [card["id"] for card in cards]},
        {
            "id": "figure3_finding",
            "type": "markdown",
            "sourceId": "r013_search",
            "body": (
                "## Figure 3｜实际观测到的 force segments\n\n"
                f"这里画的是 attempt {best_attempt} 的原始 SEARCH/PATH sidecar 点：SEARCH 以相对 PATH 起点的 RTDE 时间显示，PATH 使用实际 `runtime_strategy_path_time_s`。"
                "两段使用不同 series，避免把 SEARCH 末端、PATH 起点、RETURN/HOME 缺失段用直线连接。图中没有补值；因此它是 observed segments，不是完整 lifecycle。"
            ),
        },
        {"id": "figure3", "type": "chart", "chartId": "observed_force_lifecycle", "layout": "full"},
        {
            "id": "figure4_finding",
            "type": "markdown",
            "sourceId": "r013_strategy",
            "body": (
                "## Figure 4｜下一次 canary 的接触速度规划\n\n"
                f"当前 A0 在 {readback['boundary_mm']:.6f} mm 处硬切换 {readback['far_speed_mm_s']:.3f}→{readback['near_speed_mm_s']:.3f} mm/s；A1 仍只是旧的 offline bounded tanh-sigmoid package，不能当作当前 live 参数。"
                "图中的曲线是 command contract，不是已观测的 robot velocity；真正实际速度必须来自完整 lifecycle capture。"
            ),
        },
        {"id": "figure4", "type": "chart", "chartId": "contact_search_schedule", "layout": "full"},
        {"id": "coverage_finding", "type": "markdown", "sourceId": "r013_lifecycle", "body": "## 完整采集与 handoff gate\n\n每个 attempt 必须在 motion 前打开 lifecycle trace，并同时满足 ARM_ENTRY、CONTACT_SEARCH、BASELINE、PATH、RETURN、HOME；PATH 至少覆盖 59.5 s，force scalar、pose/TCP speed 行不能缺失，队列溢出、gap、invalid row、observer error 都会 fail closed。State21→State25 还必须有 `freeze_carry_v1` receipt，证明 first PATH tick 没有丢 integral/normal velocity。"},
        {"id": "coverage_table_block", "type": "table", "tableId": "coverage_table", "layout": "full"},
        {"id": "strategy_table_block", "type": "table", "tableId": "strategy_table", "layout": "full"},
        {"id": "handoff_table_block", "type": "table", "tableId": "handoff_table", "layout": "full"},
        {"id": "readback_table_block", "type": "table", "tableId": "readback_table", "layout": "full"},
        {"id": "limitations", "type": "markdown", "sourceId": "r013_diagnostics", "body": "## 限制与下一步\n\n- 现有 phase004 的 sealed [5,60) MAE 与 n=3 summary 只保留作历史 context，不是本轮接触瞬态结论。\n- 当前旧 run 没有 State21 sidecar 和 `.r013life`，所以不能声称已经测到完整 handoff，也不能把三次旧 attempt 写成 confirmation repeats of a new strategy。\n- 下一步先跑 unchanged A0 diagnostic canary x1；随后按 A0-B1-B1-A0-A0-B1 做完整 lifecycle A/B，每次 Home→full attempt→Home。只有每个 receipt complete，才比较 initial peak、[0,5) transient metrics、settling/rebound 和 sealed [5,60) MAE。"},
    ]
    strategy_rows = schedule
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "R013 Contact Transient Strategy Report",
            "description": "A0 baseline、A1 bounded sigmoid contact-search schedule 与 full lifecycle capture gate。",
            "generatedAt": generated_at,
            "sources": sources_manifest,
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "summary": [summary],
                "report_status": [
                    {"status": status, "observed_force_rows": len(observed_rows)}
                ],
                "observed_force": observed_rows,
                "strategy_schedule": strategy_rows,
                "coverage": coverage_rows,
                "strategy_contract": strategy_contract,
                "handoff_contract": handoff_contract,
                "current_parameters": current_parameters,
            },
        },
        "sources": sources,
    }
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    artifact = build_artifact(args.run_dir.resolve(), args.output_dir.resolve())
    (args.output_dir / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"artifact": str(args.output_dir / "artifact.json"), "status": artifact["snapshot"]["datasets"]["report_status"][0]["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
