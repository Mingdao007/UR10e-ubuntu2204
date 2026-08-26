"""Fixed, evidence-backed HTML report for the Figure-eight campaign."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import subprocess
import sys
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r004.path_reference import PATH_STAGE_ID
from step5d_autotune_v4_r013.lifecycle_trace import iter_lifecycle_rows
from step5d_autotune_v4_r013.path_context import (
    FIGURE8_BIN_WIDTH_S,
    FIGURE8_DURATION_S,
    FIGURE8_FORMAL_BIN_COUNT,
    FIGURE8_FORMAL_START_S,
    FIGURE8_FULL_BIN_COUNT,
)

from .core import CompleteCandidateV1, EvidenceReferenceV1
from .live_composition import FIGURE8_HOME_POSE, figure8_runtime_path_reference


REPORT_SCHEMA = "step5d.autotuner-report/v1"
CANONICAL_VALIDATOR = Path(
    "/home/andy/codex-private-skills-shared-main/skills/"
    "build-autotuner-report/scripts/validate_autotuner_report.py"
)
PORTABLE_BUILDER_ENV = "AUTOTUNER_REPORT_BUILDER_ROOT"
_ACTIVE_ANALYTICS_CACHE = Path(
    "/home/andy/.codex/plugins/cache/openai-curated-remote/data-analytics"
)
_FROZEN_ANALYTICS_FALLBACK = Path(
    "/home/andy/codex_handoffs/skill-sync-rollout-readonly-20260802/"
    "authoritative-v5/provider-state/luna_max/codex-home/plugins/cache/"
    "openai-curated-remote/data-analytics/0.2.8-13ceeea1f599"
)
_PORTABLE_SCRIPT_RELATIVE = Path(
    "skills/build-report/scripts/deliver_portable_artifact.mjs"
)
CHROME_OPENER = Path(
    "/home/andy/.codex/skills/open-local-artifact-in-chrome/scripts/"
    "open_local_artifacts_in_chrome.py"
)
REQUIRED_BLOCKS = (
    "title",
    "technical_summary",
    "headline_metrics",
    "identity_scope",
    "bo_progress_finding",
    "bo_progress_figure",
    "best_diagnostics_finding",
    "best_diagnostics_figure",
    "repeatability_finding",
    "repeatability_figure",
    "evaluation_table",
    "metric_acceptance_table",
    "candidate_optimizer_table",
    "qualification_closeout_table",
    "limitations",
    "next_steps",
)


class FigureEightReportError(RuntimeError):
    """A report claim could not be derived from cold campaign evidence."""


def resolve_portable_report_builder() -> tuple[Path, Path]:
    """Resolve one canonical Data Analytics portable-report builder.

    The plugin cache is intentionally replaceable, so a versioned cache path is
    not a source contract.  Prefer an explicit owner-provided root, then the
    currently installed plugin, and finally the immutable local provider-state
    snapshot that originally supplied this renderer.  Every candidate must
    carry the complete sibling script set used by the delivery entrypoint.
    """

    candidates: list[Path] = []
    override = os.environ.get(PORTABLE_BUILDER_ENV)
    if override:
        candidates.append(Path(override))
    if _ACTIVE_ANALYTICS_CACHE.is_dir():
        candidates.extend(
            path
            for path in sorted(_ACTIVE_ANALYTICS_CACHE.iterdir(), reverse=True)
            if path.is_dir()
        )
    candidates.append(_FROZEN_ANALYTICS_FALLBACK)
    required = (
        "build_portable_artifact.mjs",
        "deliver_portable_artifact.mjs",
        "extract_portable_chart_svgs.mjs",
        "portable_browser_cli.mjs",
        "portable_browser_helpers.mjs",
        "verify_portable_artifact.mjs",
    )
    for candidate in candidates:
        root = candidate.resolve()
        script = root / _PORTABLE_SCRIPT_RELATIVE
        scripts = script.parent
        if not root.is_dir() or any(
            not (scripts / name).is_file() for name in required
        ):
            continue
        return root, script
    raise FigureEightReportError(
        "canonical Data Analytics portable builder is unavailable; set "
        f"{PORTABLE_BUILDER_ENV} to a complete data-analytics plugin root"
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FigureEightReportError(f"report source is not an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_key(candidate: Mapping[str, Any]) -> str:
    return CompleteCandidateV1.from_mapping(candidate).candidate_key


def _token(candidate_key: str) -> str:
    return hashlib.sha256(candidate_key.encode("utf-8")).hexdigest()[:12]


def _source_entry(state_root: Path, path: Path, source_id: str, source_type: str) -> dict[str, Any]:
    target = Path(path).resolve()
    try:
        portable = target.relative_to(Path(state_root).resolve()).as_posix()
    except ValueError:
        portable = target.as_posix()
    return {
        "id": source_id,
        "type": source_type,
        "path": portable,
        "sha256": _sha256(target),
    }


def _bounded_rows(rows: Sequence[Any], limit: int) -> list[Any]:
    """Return a deterministic, endpoint-preserving bounded visual sample."""

    values = list(rows)
    if limit <= 0:
        raise FigureEightReportError("report visual row limit must be positive")
    if len(values) <= limit:
        return values
    if limit == 1:
        return [values[0]]
    indices = [
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    ]
    return [values[index] for index in indices]


def _sql_materialize_snapshot_datasets(
    datasets: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    upstream_sources: Sequence[Mapping[str, Any]],
    query_dir: Path,
    state_root: Path,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, str],
    list[dict[str, Any]],
]:
    """Materialize canonical datasets through actual, lossless SQLite queries.

    The report's analytical rows are first derived from SHA-bound campaign files.
    This final row-json round trip makes the exact SQL exposed by the canonical
    reader the query that actually produces each embedded widget dataset, while
    retaining the raw-file provenance as explicit upstream metadata.
    """

    connection = sqlite3.connect(":memory:")
    materialized: dict[str, list[dict[str, Any]]] = {}
    source_ids: dict[str, str] = {}
    query_sources: list[dict[str, Any]] = []
    Path(query_dir).mkdir(parents=True, exist_ok=True)
    upstream = [
        {
            "id": str(source["id"]),
            "path": str(source["path"]),
            "sha256": str(source["sha256"]),
        }
        for source in upstream_sources
    ]
    try:
        for dataset_id, rows in datasets.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", dataset_id):
                raise FigureEightReportError(
                    f"canonical dataset id is not SQL-safe: {dataset_id!r}"
                )
            table_name = f"artifact_snapshot_{dataset_id}"
            source_id = f"snapshot_{dataset_id}_sql"
            query = (
                f'SELECT row_json FROM "{table_name}" '
                "ORDER BY row_ordinal"
            )
            connection.execute(
                f'CREATE TABLE "{table_name}" '
                "(row_ordinal INTEGER PRIMARY KEY, row_json TEXT NOT NULL)"
            )
            connection.executemany(
                f'INSERT INTO "{table_name}" (row_ordinal, row_json) '
                "VALUES (?, ?)",
                [
                    (
                        index,
                        json.dumps(
                            dict(row),
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    )
                    for index, row in enumerate(rows)
                ],
            )
            output_rows = [
                json.loads(value)
                for (value,) in connection.execute(query).fetchall()
            ]
            if output_rows != [dict(row) for row in rows]:
                raise FigureEightReportError(
                    f"canonical SQL materialization changed dataset {dataset_id}"
                )
            query_path = Path(query_dir) / f"{dataset_id}.sql"
            query_path.write_text(query + ";\n", encoding="utf-8")
            materialized[dataset_id] = output_rows
            source_ids[dataset_id] = source_id
            query_sources.append(
                {
                    **_source_entry(
                        Path(state_root),
                        query_path,
                        source_id,
                        "report_sql_materialization",
                    ),
                    "label": f"Materialized report dataset: {dataset_id}",
                    "query": {
                        "engine": "sqlite",
                        "id": f"figure8_{dataset_id}_v1",
                        "sql": query,
                        "description": (
                            "Executed over an in-memory row-json staging table "
                            "after all upstream campaign files passed SHA-256 "
                            "verification. JSON round-trip preserves exact row types."
                        ),
                    },
                    "upstream": upstream,
                }
            )
    finally:
        connection.close()
    return materialized, source_ids, query_sources


def _load_report_rows(state_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    scheduler = _read_json(Path(state_root) / "scheduler_state.json")
    receipts = [
        _read_json(path)
        for path in sorted((Path(state_root) / "trial_receipts").glob("trial-*.json"))
    ]
    evaluations = []
    for index, receipt in enumerate(receipts, start=1):
        if receipt.get("censored") is True:
            plan = receipt.get("plan")
            censored = receipt.get("censored_receipt")
            if not isinstance(plan, Mapping) or not isinstance(censored, Mapping):
                raise FigureEightReportError(
                    "censored trial lacks its typed plan/receipt"
                )
            raw_path = Path(str(receipt.get("raw_prefix_artifact_path", "")))
            if (
                not raw_path.is_file()
                or _sha256(raw_path)
                != receipt.get("raw_prefix_artifact_sha256")
            ):
                raise FigureEightReportError(
                    "censored raw prefix artifact binding differs"
                )
            raw_prefix = _read_json(raw_path)
            if (
                raw_prefix.get("schema")
                != "step6.autotune/figure8-censored-raw-prefix-v1"
                or raw_prefix.get("candidate_key") != censored.get("candidate_key")
                or raw_prefix.get("campaign_fingerprint_sha256")
                != censored.get("campaign_fingerprint_sha256")
                or int(raw_prefix.get("closed_bin_count", -1))
                != int(censored.get("closed_bin_count", -2))
            ):
                raise FigureEightReportError(
                    "censored raw prefix artifact identity differs"
                )
            candidate_key = _canonical_key(censored["candidate"])
            if candidate_key != censored.get("candidate_key"):
                raise FigureEightReportError("censored candidate key differs")
            evaluations.append({
                "epoch": index,
                "attempt_id": censored.get("attempt_id"),
                "novel_ordinal": int(plan["novel_ordinal"]),
                "phase": plan["phase"],
                "kind": plan["kind"],
                "candidate_key": candidate_key,
                "candidate_token": _token(candidate_key),
                "objective_n": None,
                "plot_value_n": float(censored["prefix_mean_n"]),
                "admitted": False,
                "sealed": False,
                "physical_qualification": False,
                "censored": True,
                "trial_admission_passed": False,
                "motion_gate": False,
                "timing_gate": False,
                "safety_gate": censored.get("safe_return_closed") is True,
                "complete_bins": int(censored["closed_bin_count"]),
                "prefix_mean_n": float(censored["prefix_mean_n"]),
                "causal_lower_bound_n": float(censored["causal_lower_bound_n"]),
                "trigger_time_s": float(censored["watermark_s"]),
                "saved_path_time_s": max(
                    0.0, FIGURE8_DURATION_S - float(censored["watermark_s"])
                ),
                "raw_prefix_artifact_path": str(raw_path),
                "raw_prefix_artifact_sha256": receipt[
                    "raw_prefix_artifact_sha256"
                ],
                "exclusion_reason": "trial_censored_nontrainable",
                "receipt": receipt,
            })
            continue
        plan = receipt["plan"]
        admission = receipt["physical_admission"]
        candidate_key = _canonical_key(plan["candidate"])
        admitted = bool(
            admission.get("trial_admission_passed") is True
            and admission.get("motion_gate") is True
            and admission.get("timing_gate") is True
            and admission.get("sealed") is True
        )
        evaluations.append({
            "epoch": index,
            "attempt_id": admission.get("dispatch_id"),
            "novel_ordinal": int(plan["novel_ordinal"]),
            "phase": plan["phase"],
            "kind": plan["kind"],
            "candidate_key": candidate_key,
            "candidate_token": _token(candidate_key),
            "objective_n": float(admission["sealed_mae_n"]) if admission.get("sealed") else None,
            "plot_value_n": float(admission["sealed_mae_n"]) if admission.get("sealed") else None,
            "admitted": admitted,
            "sealed": admission.get("sealed") is True,
            "physical_qualification": (
                admitted and receipt.get("safe_return") is True
            ),
            "censored": False,
            "trial_admission_passed": admission.get("trial_admission_passed") is True,
            "motion_gate": admission.get("motion_gate") is True,
            "timing_gate": admission.get("timing_gate") is True,
            "safety_gate": receipt.get("safe_return") is True,
            "complete_bins": FIGURE8_FORMAL_BIN_COUNT if admitted else None,
            "exclusion_reason": None if admitted else "trial_admission_failed",
            "receipt": receipt,
        })
    return scheduler, receipts, evaluations


def _require_closeout_evidence(root: Path, *, final: bool) -> dict[str, Any]:
    """Cold-read the release/launch gates used by the report claims.

    The renderer must never turn a missing release artifact into a claimed
    Home/Safety closeout.  Snapshot reports are emitted after the 80-novel
    writer-release boundary; final reports additionally require the derived
    launch receipt and all of its immutable evidence references.
    """

    release_path = Path(root) / (
        "final_writer_release.json" if final else "checkpoint_080_writer_release.json"
    )
    if not release_path.is_file():
        raise FigureEightReportError(f"report release evidence is missing: {release_path}")
    release = _read_json(release_path)
    if (
        release.get("passed") is not True
        or release.get("writer_released") is not True
        or release.get("safety_mode") not in (1, "NORMAL")
        or float(release.get("position_error_m", float("inf"))) > 0.003
        or float(release.get("orientation_error_rad", float("inf"))) > 0.02
        or float(release.get("linear_speed_m_s", float("inf"))) > 0.0005
        or float(release.get("angular_speed_rad_s", float("inf"))) > 0.005
    ):
        raise FigureEightReportError(
            f"report release evidence is not passed Home/Safety closeout: {release_path}"
        )

    result: dict[str, Any] = {
        "release_path": release_path,
        "release": release,
    }
    if not final:
        return result

    launch_path = Path(root) / "launch_evidence" / "figure8_launch_receipt.json"
    if not launch_path.is_file():
        raise FigureEightReportError(f"final report launch receipt is missing: {launch_path}")
    launch = _read_json(launch_path)
    if (
        launch.get("schema") != "step6.autotune/figure8-launch-receipt-v2"
        or launch.get("version") != 2
        or launch.get("live_ready") is not True
        or launch.get("derived_by_verifier") is not True
    ):
        raise FigureEightReportError("final report launch receipt is not verifier-derived/live-ready")
    refs = launch.get("evidence")
    required_roles = {
        "package_readback",
        "no_contact_canary",
        "home_frame",
        "controller_identity",
        "source_identity",
        "eoat_tcp_payload",
        "admission_policy",
        "final_campaign_fingerprint",
    }
    if not isinstance(refs, list) or {str(item.get("role")) for item in refs if isinstance(item, Mapping)} != required_roles:
        raise FigureEightReportError("final report launch receipt evidence roles are incomplete")
    for item in refs:
        try:
            EvidenceReferenceV1.from_mapping(item).verify()
        except Exception as exc:
            raise FigureEightReportError(
                "final report launch receipt contains unverifiable evidence"
            ) from exc
    result["launch_path"] = launch_path
    result["launch"] = launch
    return result


def build_figure8_report(
    state_root: Path,
    *,
    checkpoint_novel: int,
    final: bool,
) -> dict[str, Any]:
    root = Path(state_root).resolve()
    closeout = _require_closeout_evidence(root, final=final)
    scheduler, receipts, evaluations = _load_report_rows(root)
    admitted = [row for row in evaluations if row["admitted"] and row["objective_n"] is not None]
    censored_rows = [row for row in evaluations if row.get("censored") is True]
    if not admitted:
        raise FigureEightReportError("Figure-eight report has no trial-admitted trial")
    single = min(admitted, key=lambda row: float(row["objective_n"]))
    groups = scheduler.get("observation_groups", {})
    ranked_groups = sorted(
        groups.items(),
        key=lambda item: (float(item[1]["mean_n"]), item[0]),
    )
    robust_key, robust_group = ranked_groups[0]
    robust_rows = [row for row in admitted if row["candidate_key"] == robust_key]
    if len(robust_rows) != int(robust_group["n"]):
        raise FigureEightReportError("robust group does not resolve to physical receipts")
    if final:
        if int(scheduler.get("novel_count", -1)) != 200:
            raise FigureEightReportError(
                "final report requires exactly 200 strict Exact novel candidates"
            )
        if len(ranked_groups) < 3 or any(
            int(group.get("n", 0)) < 5 for _key, group in ranked_groups[:3]
        ):
            raise FigureEightReportError(
                "final report requires the observed top three candidates at n=5"
            )
    posterior = None
    last_response: dict[str, Any] | None = None
    calibration_rows: list[dict[str, Any]] = []
    response_paths = sorted((root / "optimizer").glob("qlognei-*-response.json"))
    if response_paths:
        last_response = _read_json(response_paths[-1])
        means = last_response.get("observed_posterior_mean_n", {})
        variances = last_response.get("observed_posterior_variance_n2", {})
        if (
            isinstance(means, Mapping)
            and means
            and isinstance(variances, Mapping)
            and set(means) == set(variances)
        ):
            posterior_key = min(means, key=lambda key: float(means[key]))
            posterior = {
                "candidate_key": posterior_key,
                "candidate_token": _token(posterior_key),
                "posterior_mean_n": float(means[posterior_key]),
                "source": str(response_paths[-1]),
            }
            for candidate_key, predicted in means.items():
                group = groups.get(candidate_key)
                if not isinstance(group, Mapping):
                    continue
                variance = float(variances[candidate_key])
                observed_mean = float(group["mean_n"])
                sigma = math.sqrt(max(1e-12, variance))
                calibration_rows.append({
                    "candidate_token": _token(str(candidate_key)),
                    "observed_mean_n": observed_mean,
                    "posterior_mean_n": float(predicted),
                    "posterior_sigma_n": sigma,
                    "standardized_residual": (
                        observed_mean - float(predicted)
                    ) / sigma,
                    "group_n": int(group["n"]),
                    "empirical_yvar_n2": float(group["yvar_n2"]),
                })
    best_receipt = single["receipt"]
    lifecycle = best_receipt["force_lifecycle"]
    lifecycle_path = Path(lifecycle["artifact_path"])
    lifecycle_rows = list(iter_lifecycle_rows(lifecycle_path))
    if not lifecycle_rows:
        raise FigureEightReportError("best trial lifecycle is empty")
    start_mono = float(lifecycle_rows[0]["monotonic_s"])
    display_rows = []
    for row in lifecycle_rows:
        display_rows.append({
            **row,
            "relative_time_s": float(row["monotonic_s"]) - start_mono,
        })
    path_rows = [row for row in lifecycle_rows if int(row["phase_code"]) == 25]
    path_start = float(path_rows[0]["monotonic_s"])
    actual_xy: list[tuple[float, float]] = []
    desired_xy: list[tuple[float, float]] = []
    position_error: list[tuple[float, float]] = []
    orientation_error: list[tuple[float, float]] = []
    for row in path_rows:
        time_s = float(row["monotonic_s"]) - path_start
        pose = tuple(float(value) for value in row["pose"])
        reference = figure8_runtime_path_reference(PATH_STAGE_ID, pose[:2], time_s)
        desired = tuple(float(value) for value in reference["desired_xy"])
        actual_xy.append((pose[0], pose[1]))
        desired_xy.append(desired)
        position_error.append((time_s, math.dist(pose[:2], desired)))
        orientation_error.append((time_s, math.dist(pose[3:6], FIGURE8_HOME_POSE[3:6])))
    robust_receipts = [row["receipt"] for row in robust_rows]
    repeat_values = []
    for receipt in robust_receipts:
        repeat_values.append(float(receipt["physical_admission"]["sealed_mae_n"]))
    segments = []
    for start in range(int(FIGURE8_FORMAL_START_S), int(FIGURE8_DURATION_S), 5):
        rows = [
            row
            for receipt in robust_receipts
            for row in _read_json(Path(receipt["raw_artifact_path"]))["samples"]
            if start <= float(row["path_time_s"]) < start + 5
        ]
        errors = [float(row["filtered_normal_n"]) - 5.0 for row in rows]
        segments.append({
            "window_s": [start, start + 5],
            "mae_n": math.fsum(abs(value) for value in errors) / len(errors),
            "signed_bias_n": math.fsum(errors) / len(errors),
        })
    source_paths = {
        "scheduler": root / "scheduler_state.json",
        "launch": root / "launch_evidence" / "figure8_launch_receipt.json",
        "release": root / "final_writer_release.json" if final else root / "checkpoint_080_writer_release.json",
        "best_raw": Path(best_receipt["raw_artifact_path"]),
        "best_lifecycle": lifecycle_path,
        "handoff_ab": root / "matched_handoff_ab.json",
        "home_calibration": root / "home_calibration_receipt.json",
    }
    if response_paths:
        source_paths["optimizer_response"] = response_paths[-1]
    for index, receipt in enumerate(robust_receipts, start=1):
        source_paths[f"robust_raw_{index:02d}"] = Path(
            receipt["raw_artifact_path"]
        )
    sources = [
        _source_entry(root, path, source_id, "campaign_evidence")
        for source_id, path in source_paths.items()
        if path.is_file()
    ]
    sources.extend(
        _source_entry(
            root,
            Path(row["raw_prefix_artifact_path"]),
            f"censored_raw_{row['epoch']:06d}",
            "censored_prefix_evidence",
        )
        for row in censored_rows
    )
    dispatch_count = int(
        scheduler.get("novel_dispatch_count", scheduler.get("novel_count", 0))
    )
    censor_rate = (
        0.0 if dispatch_count == 0 else len(censored_rows) / dispatch_count
    )
    saved_path_time_s = math.fsum(
        float(row["saved_path_time_s"]) for row in censored_rows
    )
    repeated_available = len(robust_rows) >= 3
    repeated_evidence: dict[str, Any]
    if repeated_available:
        repeated_evidence = {
            "available": True,
            "candidate_token": _token(robust_key),
            "epochs": [row["epoch"] for row in robust_rows],
            "n": len(robust_rows),
            "acceptance_statistic": "mean",
            "mean_n": statistics.fmean(repeat_values),
            "std_n": statistics.pstdev(repeat_values),
            "min_n": min(repeat_values),
            "max_n": max(repeat_values),
            "target_margin_n": None,
            "target_margin_reason": "campaign target stop is disabled",
            "yvar_n2": robust_group.get("yvar_n2"),
        }
    else:
        repeated_evidence = {
            "available": False,
            "reason": "observed leading candidate has fewer than three exact repeats",
            "candidate_token": _token(robust_key),
            "n": len(robust_rows),
        }
    evidence = {
        "schema_version": REPORT_SCHEMA,
        "report": {
            "mode": "closeout" if final else "snapshot",
            "language": "zh-CN",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "target_achieved_claim": False,
            "checkpoint_novel": checkpoint_novel,
        },
        "campaign": {
            "campaign_id": "r013-figure8-direct-campaign-v1",
            "lineage": "step6_figure8_direct_v4_equivalent_restored_v1",
            "revision": 1,
            "source_commit": "dirty-source-closure-bound-by-source_sha256",
            "target_force_n": 5.0,
            "state": "completed" if final else "active",
            "authority_state": "writer_released" if closeout["release"].get("writer_released") is True else "unknown",
            "safety_state": "NORMAL_HOME" if closeout["release"].get("safety_mode") in (1, "NORMAL") else "unknown",
            "fingerprint_sha256": scheduler.get("campaign_fingerprint_sha256"),
        },
        "metric_contract": {
            "sealed": {
                "signal": "filtered_normal_n raw measured scalar",
                "window_s": [FIGURE8_FORMAL_START_S, FIGURE8_DURATION_S],
                "bin_seconds": FIGURE8_BIN_WIDTH_S,
                "bin_count": FIGURE8_FORMAL_BIN_COUNT,
                "full_bin_count": FIGURE8_FULL_BIN_COUNT,
                "aggregation": "mean_absolute_error",
                "unit": "N",
                "threshold_n": None,
                "threshold_policy": "no_absolute_mae_early_stop",
            },
            "ext60": {
                "available": False,
                "reason": (
                    "formal objective ends at 60 s; no independent ext60 "
                    "metric was recorded"
                ),
            },
        },
        "optimizer": {
            "model": "botorch.SingleTaskGP grouped mean + empirical train_Yvar",
            "acquisition": "serial q=1 qLogNEI with forced/global Sobol",
            "q": 1,
            "async_policy": "safe_return_only_proposal_prefetch_v1; physical q=1 single writer",
            "dimensions": 6,
            "budget": [200, 200],
            "exact_novel_target": 200,
            "novel_dispatch_count": scheduler.get("novel_dispatch_count", scheduler.get("novel_count", 0)),
            "pool_size": 128,
            "stop_reason": (
                "exact_novel_target_200_and_top3_n5"
                if final
                else None
            ),
            "in_flight": 0,
            "convergence_check_count": len(scheduler.get("convergence_checks", ())),
        },
        "trial_censor": {
            "active": True,
            "guard_bins": 25,
            "kappa": 2.0,
            "nontrainable": True,
            "exact_budget_exempt": True,
            "count": len(censored_rows),
            "novel_dispatch_count": dispatch_count,
            "censor_rate": censor_rate,
            "saved_path_time_s": saved_path_time_s,
            "rows": [
                {
                    key: row[key]
                    for key in (
                        "epoch",
                        "novel_ordinal",
                        "candidate_token",
                        "complete_bins",
                        "prefix_mean_n",
                        "causal_lower_bound_n",
                        "trigger_time_s",
                        "saved_path_time_s",
                        "raw_prefix_artifact_path",
                        "raw_prefix_artifact_sha256",
                    )
                }
                for row in censored_rows
            ],
        },
        "evaluations": [
            {key: value for key, value in row.items() if key != "receipt"}
            for row in evaluations
        ],
        "incumbent": {
            "single_trial": {
                "candidate_token": single["candidate_token"],
                "epoch": single["epoch"],
                "sealed_mae_n": single["objective_n"],
            },
            "repeated": repeated_evidence,
            "posterior": posterior,
            "top_three": [
                {
                    "candidate_token": _token(candidate_key),
                    "rank": rank,
                    "n": int(group["n"]),
                    "mean_n": float(group["mean_n"]),
                    "std_n": float(group.get("sample_std_n", 0.0)),
                    "yvar_n2": float(group["yvar_n2"]),
                }
                for rank, (candidate_key, group) in enumerate(
                    ranked_groups[:3], start=1
                )
            ],
        },
        "best_trial": {
            "epoch": single["epoch"],
            "candidate_token": single["candidate_token"],
            "force": {
                "raw_signal": "lifecycle normal_load_n",
                "filtered_signal": "lifecycle/raw-artifact filtered_normal_n",
                "target_n": 5.0,
                "formal_window_s": [FIGURE8_FORMAL_START_S, FIGURE8_DURATION_S],
                "segments": [
                    {"name": "SEARCH", "source_phase": "CONTACT_SEARCH"},
                    {"name": "handoff", "source_phase": "BASELINE_to_PATH"},
                    {"name": "PATH", "source_phase": "PATH"},
                    {"name": "return/Home", "source_phase": "RETURN_HOME"},
                ],
                "gaps": [
                    {"start_s": None, "end_s": None, "interpolated": False, "count": lifecycle.get("artifact_sample_index_gap_count", 0)}
                ] if lifecycle.get("artifact_sample_index_gap_count", 0) else [],
            },
            "position": {
                "metric": "XY Euclidean tracking error",
                "unit": "m",
                "sample_count": len(position_error),
                "summary": {"max": max(value for _time, value in position_error), "mean": statistics.fmean(value for _time, value in position_error)},
            },
            "orientation": {
                "metric": "fixed-rotvec Euclidean proxy",
                "unit": "rad",
                "sample_count": len(orientation_error),
                "summary": {"max": max(value for _time, value in orientation_error), "mean": statistics.fmean(value for _time, value in orientation_error)},
            },
            "sampling": {
                "nominal_hz": 500.0,
                "observed_hz": len(path_rows) / max(1e-9, float(lifecycle["path_duration_s"])),
                "objective_bins": FIGURE8_FORMAL_BIN_COUNT,
            },
            "return_home": {
                "observed": closeout["release"].get("passed") is True,
                "source_id": "release",
                "source_path": str(closeout["release_path"]),
            },
        },
        "five_second_segments": segments,
        "sources": sources,
    }
    report_dir = root / "reports" / (
        "final" if final else f"checkpoint-{checkpoint_novel:03d}"
    )
    evidence_path = report_dir / "evidence.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    repeated_mean = statistics.fmean(repeat_values)
    repeated_std = statistics.pstdev(repeat_values)
    status_label = "final closeout" if final else f"checkpoint n={checkpoint_novel}"
    source_ids = {source["id"] for source in sources}
    if "scheduler" not in source_ids or "best_lifecycle" not in source_ids or "release" not in source_ids:
        raise FigureEightReportError("canonical report lacks core material sources")
    if any(Path(source["path"]).is_absolute() for source in sources):
        raise FigureEightReportError(
            "canonical report source paths must remain campaign-relative"
        )

    evaluation_plot = []
    best_so_far = []
    running_best = math.inf
    previous_phase = None
    phase_references = []
    for row in evaluations:
        if row["phase"] != previous_phase:
            if previous_phase is not None:
                phase_references.append(
                    {"axis": "x", "value": float(row["epoch"]) - 0.5}
                )
            previous_phase = row["phase"]
        if row.get("censored") is True:
            evidence_kind = "censored prefix (nontrainable)"
        elif row.get("admitted") is True and row.get("kind") == "novel":
            evidence_kind = "Exact novel"
        elif row.get("admitted") is True:
            evidence_kind = "Exact repeat/sentinel"
        else:
            evidence_kind = "strict rejected"
        evaluation_plot.append({
            "epoch": int(row["epoch"]),
            "novel_ordinal": int(row["novel_ordinal"]),
            "phase": str(row["phase"]),
            "candidate_token": str(row["candidate_token"]),
            "evidence_kind": evidence_kind,
            "display_mae_n": row.get("plot_value_n"),
            "sealed_mae_n": row.get("objective_n"),
            "prefix_mean_n": row.get("prefix_mean_n"),
            "admitted": bool(row["admitted"]),
            "exclusion_reason": str(row.get("exclusion_reason") or ""),
        })
        objective = row.get("objective_n")
        if row.get("admitted") is True and objective is not None:
            candidate_best = float(objective)
            if math.isfinite(running_best) and candidate_best < running_best:
                best_so_far.append({
                    "epoch": int(row["epoch"]),
                    "best_so_far_n": running_best,
                })
            running_best = min(running_best, candidate_best)
        if math.isfinite(running_best):
            best_so_far.append({
                "epoch": int(row["epoch"]),
                "best_so_far_n": running_best,
            })

    lifecycle_visual = []
    for row in _bounded_rows(display_rows, 620):
        time_s = float(row["relative_time_s"])
        phase = str(row.get("phase_code", ""))
        for field, series in (
            ("normal_load_n", "raw normal"),
            ("filtered_normal_n", "deployed filtered"),
            ("setpoint_n", "force setpoint"),
        ):
            try:
                value = float(row[field])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                lifecycle_visual.append({
                    "lifecycle_time_s": time_s,
                    "force_n": value,
                    "series": series,
                    "phase_code": phase,
                })

    repeat_visual = []
    per_repeat_limit = max(2, 1900 // max(1, len(robust_receipts)))
    for index, receipt in enumerate(robust_receipts, start=1):
        samples = _read_json(Path(receipt["raw_artifact_path"]))["samples"]
        for row in _bounded_rows(samples, per_repeat_limit):
            repeat_visual.append({
                "path_time_s": float(row["path_time_s"]),
                "force_n": float(row["filtered_normal_n"]),
                "repeat": f"repeat {index}",
                "sealed_mae_n": float(
                    receipt["physical_admission"]["sealed_mae_n"]
                ),
                "candidate_token": _token(robust_key),
            })

    xy_visual = []
    paired_xy = list(zip(actual_xy, desired_xy, strict=True))
    for actual, desired in _bounded_rows(paired_xy, 900):
        xy_visual.extend((
            {"base_x_m": actual[0], "base_y_m": actual[1], "series": "actual"},
            {"base_x_m": desired[0], "base_y_m": desired[1], "series": "desired"},
        ))
    position_visual = [
        {"path_time_s": time_s, "position_error_m": value}
        for time_s, value in _bounded_rows(position_error, 1900)
    ]
    orientation_visual = [
        {"path_time_s": time_s, "orientation_error_rad": value}
        for time_s, value in _bounded_rows(orientation_error, 1900)
    ]

    censored_prefix_visual = []
    visible_censored = censored_rows[-12:]
    per_censor_limit = max(2, 1900 // max(1, len(visible_censored)))
    for row in visible_censored:
        samples = _read_json(Path(row["raw_prefix_artifact_path"]))["samples"]
        normalized = []
        for sample in samples:
            try:
                time_s = float(sample.get("path_time_s", sample.get("time_s")))
                force_n = float(
                    sample.get(
                        "filtered_normal_n",
                        sample.get(
                            "raw_measured_normal_n",
                            sample.get("normal_load_n"),
                        ),
                    )
                )
            except (AttributeError, TypeError, ValueError):
                continue
            if math.isfinite(time_s) and math.isfinite(force_n):
                normalized.append((time_s, force_n))
        for time_s, force_n in _bounded_rows(normalized, per_censor_limit):
            censored_prefix_visual.append({
                "path_time_s": time_s,
                "force_n": force_n,
                "candidate_token": row["candidate_token"],
                "trigger_time_s": row["trigger_time_s"],
                "closed_bins": row["complete_bins"],
            })

    segment_visual = []
    for row in segments:
        window = f"[{row['window_s'][0]},{row['window_s'][1]})"
        segment_visual.extend((
            {"window": window, "metric": "MAE", "value_n": row["mae_n"]},
            {"window": window, "metric": "signed bias", "value_n": row["signed_bias_n"]},
        ))

    parameter = CompleteCandidateV1.from_mapping(robust_group["candidate"])
    opened = dict(scheduler.get("probe_bounds_open", {}))
    controller = parameter.controller_path
    parameter_rows = [
        {
            "row_kind": "incumbent parameter",
            "parameter": "force P/D",
            "value": float(controller["force_p_gain"]) / float(controller["force_damping"]),
            "lower": 1.25e-5,
            "upper": 4.0e-4,
            "unit": "ratio",
            "note": "log2 search coordinate",
        },
        {
            "row_kind": "incumbent parameter",
            "parameter": "force damping",
            "value": float(controller["force_damping"]),
            "lower": 7.0,
            "upper": 224.0,
            "unit": "controller units",
            "note": "log2 search coordinate",
        },
        {
            "row_kind": "incumbent parameter",
            "parameter": "normal filter tau",
            "value": float(controller["normal_filter_tau_s"]),
            "lower": 0.03094 if opened.get("normal_filter_tau_s") else 0.04375,
            "upper": 0.0735784,
            "unit": "s",
            "note": "outward bound opens only after n=3 probe",
        },
        {
            "row_kind": "incumbent parameter",
            "parameter": "orientation Ko",
            "value": float(controller["orientation_ko"]),
            "lower": 0.03536 if opened.get("orientation_ko") else 0.05,
            "upper": 0.8,
            "unit": "controller units",
            "note": "path-specific",
        },
        {
            "row_kind": "incumbent parameter",
            "parameter": "motion Kp",
            "value": float(controller["motion_kp"]),
            "lower": 1.5,
            "upper": 6.0,
            "unit": "controller units",
            "note": "path-specific",
        },
        {
            "row_kind": "incumbent parameter",
            "parameter": "force I/P",
            "value": float(controller["force_i_gain"]) / float(controller["force_p_gain"]),
            "lower": 0.05,
            "upper": 0.7071 if opened.get("force_i_gain") else 0.5,
            "unit": "ratio",
            "note": "log2 search coordinate",
        },
    ]
    for name, value in zip(
        ("bias", "speed", "accel", "curvature", "sin_phase", "cos_phase"),
        parameter.correction_weights,
        strict=True,
    ):
        parameter_rows.append({
            "row_kind": "incumbent parameter",
            "parameter": f"correction.{name}",
            "value": float(value),
            "lower": -0.5,
            "upper": 0.5,
            "unit": "N coefficient",
            "note": "L1<=1.25 N; output clip +/-1.25 N; slew<=0.5 N/s",
        })
    if last_response is not None:
        fit = last_response.get("fit_receipt", {})
        lengthscales = fit.get("lengthscales_unit_fitted", ())
        if isinstance(lengthscales, list):
            block = str(fit.get("block", "unknown"))
            names = (
                ("P/D", "damping", "tau", "Ko", "motion Kp", "I/P")
                if block == "controller_path"
                else ("bias", "speed", "accel", "curvature", "sin phase", "cos phase")
            )
            for name, value in zip(names, lengthscales):
                parameter_rows.append({
                    "row_kind": "latest ARD lengthscale",
                    "parameter": name,
                    "value": float(value),
                    "lower": None,
                    "upper": None,
                    "unit": "normalized domain",
                    "note": f"latest fitted block={block}; smaller means locally more sensitive",
                })

    evaluation_table = [
        {
            "epoch": row["epoch"],
            "novel": row["novel_ordinal"],
            "phase": row["phase"],
            "kind": row["kind"],
            "candidate": row["candidate_token"],
            "sealed_mae_n": row.get("objective_n"),
            "censored_prefix_mean_n": row.get("prefix_mean_n"),
            "trial_admission": row["trial_admission_passed"],
            "physical_qualification": row["physical_qualification"],
            "motion_gate": row["motion_gate"],
            "timing_gate": row["timing_gate"],
            "safety_gate": row["safety_gate"],
            "complete_bins": row["complete_bins"],
            "exclusion": str(row.get("exclusion_reason") or ""),
        }
        for row in evaluations
    ]
    metric_rows = [
        {"contract": "objective signal", "value": "deployed filtered normal scalar from raw measured force"},
        {"contract": "formal window", "value": "[5,60) s"},
        {"contract": "seal", "value": "550 complete 0.1 s bins; no interpolation"},
        {"contract": "aggregation", "value": "mean(abs(mean(force in bin)-5 N))"},
        {"contract": "campaign stop", "value": "no absolute MAE target; exactly 200 Exact novel rows"},
        {"contract": "trial censor", "value": "ordinary novel only; 25-bin guard; kappa=2; nontrainable; refill"},
        {"contract": "trial admission", "value": "trial + motion + timing + safety + 550-bin seal"},
    ]
    handoff = _read_json(root / "matched_handoff_ab.json")
    campaign_closeout_rows = [
        {"gate": "campaign fingerprint", "status": "frozen", "detail": str(scheduler.get("campaign_fingerprint_sha256"))},
        {"gate": "handoff", "status": "passed", "detail": f"{handoff['winner_handoff_policy']}; winner n=5"},
        {"gate": "sentinel drift", "status": "passed", "detail": json.dumps(scheduler.get("drift_state", {}), sort_keys=True)},
        {"gate": "return/Home", "status": "passed", "detail": f"position error={float(closeout['release']['position_error_m']):.6f} m"},
        {"gate": "Safety", "status": "NORMAL", "detail": str(closeout["release"].get("safety_mode"))},
        {"gate": "writer", "status": "released", "detail": str(closeout["release_path"])},
    ]

    headline = [{
        "repeated_mean_n": repeated_mean if repeated_available else None,
        "repeated_std_n": repeated_std if repeated_available else None,
        "repeated_n": len(robust_rows),
        "single_min_n": float(single["objective_n"]),
        "single_min_epoch": int(single["epoch"]),
        "posterior_incumbent_n": None if posterior is None else posterior["posterior_mean_n"],
        "exact_novel_count": int(scheduler["novel_count"]),
        "exact_novel_target": 200,
        "censor_count": len(censored_rows),
        "censor_rate": censor_rate,
        "saved_path_time_s": saved_path_time_s,
    }]

    datasets = {
        "headline": headline,
        "bo_evaluations": evaluation_plot,
        "bo_best_so_far": best_so_far,
        "best_lifecycle": lifecycle_visual,
        "robust_repeats": repeat_visual,
        "xy_tracking": xy_visual,
        "position_error": position_visual,
        "orientation_error": orientation_visual,
        "five_second_segments": segment_visual,
        "evaluation_table": evaluation_table,
        "metric_contract": metric_rows,
        "candidate_optimizer": parameter_rows,
        "campaign_closeout": campaign_closeout_rows,
        "censor_table": [
            {
                "epoch": row["epoch"],
                "novel": row["novel_ordinal"],
                "candidate": row["candidate_token"],
                "closed_bins": row["complete_bins"],
                "prefix_mean_n": row["prefix_mean_n"],
                "causal_lower_bound_n": row["causal_lower_bound_n"],
                "trigger_time_s": row["trigger_time_s"],
                "saved_path_time_s": row["saved_path_time_s"],
            }
            for row in censored_rows
        ],
        "gp_calibration": calibration_rows,
    }
    if censored_prefix_visual:
        datasets["censored_prefixes"] = censored_prefix_visual

    primary_source = "scheduler"
    optimizer_source = (
        "optimizer_response" if "optimizer_response" in source_ids else primary_source
    )
    repeat_source = (
        "robust_raw_01" if "robust_raw_01" in source_ids else "best_raw"
    )
    manifest_sources = [
        {"id": source["id"], "label": source["id"].replace("_", " "), "path": source["path"]}
        for source in sources
    ]
    cards = [
        {
            "id": "repeated_incumbent_card",
            "description": "Same-candidate Exact repeat statistic; unavailable until n>=3.",
            "dataset": "headline",
            "sourceId": primary_source,
            "metrics": [
                {"label": "Repeated incumbent mean [N]", "field": "repeated_mean_n", "format": "number"},
                {"label": "Repeat std [N]", "field": "repeated_std_n", "format": "number"},
                {"label": "n", "field": "repeated_n", "format": "number"},
            ],
        },
        {
            "id": "single_minimum_card",
            "description": "Lowest single trial-admitted 550-bin observation; not a repeated claim.",
            "dataset": "headline",
            "sourceId": primary_source,
            "metrics": [
                {"label": "Single minimum [N]", "field": "single_min_n", "format": "number"},
                {"label": "Evaluation", "field": "single_min_epoch", "format": "number"},
            ],
        },
        {
            "id": "posterior_incumbent_card",
            "description": "Latest GP posterior mean among observed canonical groups.",
            "dataset": "headline",
            "sourceId": optimizer_source,
            "metrics": [
                {"label": "Posterior incumbent [N]", "field": "posterior_incumbent_n", "format": "number"},
            ],
        },
        {
            "id": "exact_budget_card",
            "description": "Censored/rejected/repeat/sentinel rows do not increase this count.",
            "dataset": "headline",
            "sourceId": primary_source,
            "metrics": [
                {"label": "Exact novel", "field": "exact_novel_count", "format": "number"},
                {"label": "Target", "field": "exact_novel_target", "format": "number"},
            ],
        },
        {
            "id": "trial_censor_card",
            "description": "Heuristic trial-level censor; rows remain nontrainable evidence.",
            "dataset": "headline",
            "sourceId": primary_source,
            "metrics": [
                {"label": "Censored trials", "field": "censor_count", "format": "number"},
                {"label": "Censor rate", "field": "censor_rate", "format": "percent"},
                {"label": "PATH time saved [s]", "field": "saved_path_time_s", "format": "number"},
            ],
        },
    ]

    posterior_refs = []
    if repeated_available:
        posterior_refs.append({"axis": "y", "value": repeated_mean})
    if posterior is not None:
        posterior_refs.append({"axis": "y", "value": posterior["posterior_mean_n"]})
    charts = [
        {
            "id": "bo_evaluation_scatter",
            "title": "Physical evaluation order: Exact objectives and censored prefixes",
            "subtitle": "Censored prefix means are displayed for audit only and never update best-so-far.",
            "type": "scatter",
            "dataset": "bo_evaluations",
            "sourceId": primary_source,
            "encodings": {
                "x": {"field": "epoch", "type": "quantitative", "label": "Physical evaluation"},
                "y": {"field": "display_mae_n", "type": "quantitative", "label": "Sealed or prefix MAE", "unit": "N"},
                "color": {"field": "evidence_kind", "type": "nominal", "label": "Evidence type"},
                "tooltip": [
                    {"field": "novel_ordinal", "type": "quantitative", "label": "Novel ordinal"},
                    {"field": "phase", "type": "nominal", "label": "Phase"},
                    {"field": "candidate_token", "type": "nominal", "label": "Candidate"},
                    {"field": "sealed_mae_n", "type": "quantitative", "label": "Sealed MAE", "unit": "N"},
                    {"field": "prefix_mean_n", "type": "quantitative", "label": "Prefix mean", "unit": "N"},
                ],
            },
            "referenceLines": [*phase_references, *posterior_refs],
            "layout": "full",
        },
        {
            "id": "bo_best_step",
            "title": "Admitted-only observed best-so-far",
            "subtitle": "Step changes use only finite trial-admitted 550-bin Exact rows.",
            "type": "line",
            "dataset": "bo_best_so_far",
            "sourceId": primary_source,
            "encodings": {
                "x": {"field": "epoch", "type": "quantitative", "label": "Physical evaluation"},
                "y": {"field": "best_so_far_n", "type": "quantitative", "label": "Observed best", "unit": "N"},
            },
            "referenceLines": phase_references,
            "layout": "full",
        },
        {
            "id": "best_force_lifecycle",
            "title": "Best single trial: complete force lifecycle",
            "subtitle": "SEARCH, handoff, PATH and return/Home; scatter marks prevent any visual bridge across real gaps.",
            "type": "scatter",
            "dataset": "best_lifecycle",
            "sourceId": "best_lifecycle",
            "encodings": {
                "x": {"field": "lifecycle_time_s", "type": "quantitative", "label": "Lifecycle time", "unit": "s"},
                "y": {"field": "force_n", "type": "quantitative", "label": "Normal force", "unit": "N"},
                "color": {"field": "series", "type": "nominal", "label": "Signal"},
                "tooltip": [{"field": "phase_code", "type": "nominal", "label": "Phase code"}],
            },
            "referenceLines": [{"axis": "y", "value": 5.0}],
            "layout": "full",
        },
        {
            "id": "best_xy_tracking",
            "title": "Best single trial: XY Figure-eight tracking",
            "subtitle": "Desired and actual base-frame trajectories use the same frozen frame.",
            "type": "scatter",
            "dataset": "xy_tracking",
            "sourceId": "best_lifecycle",
            "encodings": {
                "x": {"field": "base_x_m", "type": "quantitative", "label": "Base X", "unit": "m"},
                "y": {"field": "base_y_m", "type": "quantitative", "label": "Base Y", "unit": "m"},
                "color": {"field": "series", "type": "nominal", "label": "Trajectory"},
            },
            "layout": "full",
        },
        {
            "id": "best_position_error",
            "title": "Best single trial: position error",
            "subtitle": "XY Euclidean error in the frozen base frame.",
            "type": "line",
            "dataset": "position_error",
            "sourceId": "best_lifecycle",
            "encodings": {
                "x": {"field": "path_time_s", "type": "quantitative", "label": "PATH time", "unit": "s"},
                "y": {"field": "position_error_m", "type": "quantitative", "label": "Position error", "unit": "m"},
            },
            "layout": "full",
        },
        {
            "id": "best_orientation_error",
            "title": "Best single trial: deployed orientation-error proxy",
            "subtitle": "Fixed-rotvec Euclidean proxy is labeled as such; it is not promoted to a roll-invariant metric.",
            "type": "line",
            "dataset": "orientation_error",
            "sourceId": "best_lifecycle",
            "encodings": {
                "x": {"field": "path_time_s", "type": "quantitative", "label": "PATH time", "unit": "s"},
                "y": {"field": "orientation_error_rad", "type": "quantitative", "label": "Orientation error", "unit": "rad"},
            },
            "layout": "full",
        },
        {
            "id": "robust_repeat_force",
            "title": "Robust incumbent: all same-candidate force repeats",
            "subtitle": f"Candidate {_token(robust_key)}, n={len(robust_rows)}; every repeat is shown without an envelope.",
            "type": "line",
            "dataset": "robust_repeats",
            "sourceId": repeat_source,
            "encodings": {
                "x": {"field": "path_time_s", "type": "quantitative", "label": "PATH time", "unit": "s"},
                "y": {"field": "force_n", "type": "quantitative", "label": "Measured normal force", "unit": "N"},
                "color": {"field": "repeat", "type": "nominal", "label": "Repeat"},
                "tooltip": [{"field": "sealed_mae_n", "type": "quantitative", "label": "Sealed MAE", "unit": "N"}],
            },
            "referenceLines": [{"axis": "y", "value": 5.0}],
            "layout": "full",
        },
        {
            "id": "segment_residuals",
            "title": "Robust incumbent: 5 s segment residuals",
            "subtitle": "MAE and signed bias are pooled across the same-candidate repeats; negative bias is undershoot.",
            "type": "bar",
            "dataset": "five_second_segments",
            "sourceId": repeat_source,
            "encodings": {
                "x": {"field": "window", "type": "ordinal", "label": "PATH window [s]"},
                "y": {"field": "value_n", "type": "quantitative", "label": "Residual", "unit": "N"},
                "color": {"field": "metric", "type": "nominal", "label": "Metric"},
            },
            "referenceLines": [{"axis": "y", "value": 0.0}],
            "layout": "full",
        },
    ]
    if censored_prefix_visual:
        charts.insert(2, {
            "id": "censored_prefix_force",
            "title": "Early-stopped novel trials: recorded force prefixes",
            "subtitle": f"Latest {len(visible_censored)} of {len(censored_rows)} censored prefixes; scatter marks preserve real coverage/gaps.",
            "type": "scatter",
            "dataset": "censored_prefixes",
            "sourceId": f"censored_raw_{visible_censored[-1]['epoch']:06d}",
            "encodings": {
                "x": {"field": "path_time_s", "type": "quantitative", "label": "PATH time", "unit": "s"},
                "y": {"field": "force_n", "type": "quantitative", "label": "Measured normal force", "unit": "N"},
                "color": {"field": "candidate_token", "type": "nominal", "label": "Candidate"},
                "tooltip": [
                    {"field": "trigger_time_s", "type": "quantitative", "label": "Trigger time", "unit": "s"},
                    {"field": "closed_bins", "type": "quantitative", "label": "Closed bins"},
                ],
            },
            "referenceLines": [{"axis": "y", "value": 5.0}],
            "layout": "full",
        })
    if calibration_rows:
        charts.append({
            "id": "gp_calibration_scatter",
            "title": "Latest GP posterior mean versus observed group mean",
            "subtitle": "Each point is one canonical candidate group; tooltip includes posterior sigma, empirical Yvar and n.",
            "type": "scatter",
            "dataset": "gp_calibration",
            "sourceId": optimizer_source,
            "encodings": {
                "x": {"field": "posterior_mean_n", "type": "quantitative", "label": "Posterior mean", "unit": "N"},
                "y": {"field": "observed_mean_n", "type": "quantitative", "label": "Observed group mean", "unit": "N"},
                "size": {"field": "group_n", "type": "quantitative", "label": "Group n"},
                "tooltip": [
                    {"field": "candidate_token", "type": "nominal", "label": "Candidate"},
                    {"field": "posterior_sigma_n", "type": "quantitative", "label": "Posterior sigma", "unit": "N"},
                    {"field": "empirical_yvar_n2", "type": "quantitative", "label": "Yvar", "unit": "N2"},
                    {"field": "standardized_residual", "type": "quantitative", "label": "Standardized residual"},
                ],
            },
            "layout": "full",
        })

    tables = [
        {
            "id": "evaluation_detail",
            "title": "Exact, censored and rejected evaluation ledger",
            "subtitle": "Separate trial-admission receipts keep rejected and Exact observations distinct.",
            "dataset": "evaluation_table",
            "sourceId": primary_source,
            "defaultSort": {"field": "epoch", "direction": "asc"},
            "density": "compact",
            "layout": "full",
            "columns": [
                {"field": "epoch", "label": "Eval", "format": "number"},
                {"field": "novel", "label": "Novel", "format": "number"},
                {"field": "phase", "label": "Phase", "type": "text"},
                {"field": "kind", "label": "Kind", "type": "text"},
                {"field": "candidate", "label": "Candidate", "type": "text"},
                {"field": "sealed_mae_n", "label": "Sealed MAE [N]", "format": "number"},
                {"field": "censored_prefix_mean_n", "label": "Prefix mean [N]", "format": "number"},
                {"field": "trial_admission", "label": "Trial admit", "type": "text"},
                {"field": "physical_qualification", "label": "Physical", "type": "text"},
                {"field": "motion_gate", "label": "Motion", "type": "text"},
                {"field": "timing_gate", "label": "Timing", "type": "text"},
                {"field": "safety_gate", "label": "Safety", "type": "text"},
                {"field": "complete_bins", "label": "Bins", "format": "number"},
                {"field": "exclusion", "label": "Exclusion", "type": "text"},
            ],
        },
        {
            "id": "metric_contract_detail",
                "title": "Metric, censor and trial-admission contract",
            "dataset": "metric_contract",
            "sourceId": primary_source,
            "columns": [
                {"field": "contract", "label": "Contract", "type": "text"},
                {"field": "value", "label": "Frozen value", "type": "text"},
            ],
        },
        {
            "id": "candidate_optimizer_detail",
            "title": "Robust incumbent parameters, bounds and latest ARD",
            "dataset": "candidate_optimizer",
            "sourceId": optimizer_source,
            "columns": [
                {"field": "row_kind", "label": "Row", "type": "text"},
                {"field": "parameter", "label": "Parameter", "type": "text"},
                {"field": "value", "label": "Value", "format": "number"},
                {"field": "lower", "label": "Lower", "format": "number"},
                {"field": "upper", "label": "Upper", "format": "number"},
                {"field": "unit", "label": "Unit", "type": "text"},
                {"field": "note", "label": "Interpretation", "type": "text"},
            ],
        },
        {
            "id": "campaign_closeout_detail",
            "title": "Campaign and physical closeout",
            "dataset": "campaign_closeout",
            "sourceId": "release",
            "columns": [
                {"field": "gate", "label": "Gate", "type": "text"},
                {"field": "status", "label": "Status", "type": "text"},
                {"field": "detail", "label": "Evidence", "type": "text"},
            ],
        },
    ]
    if censored_rows:
        tables.append({
            "id": "censor_detail",
            "title": "Typed censored trial receipts",
            "subtitle": "Prefix mean and causal lower bound are not penalty objectives.",
            "dataset": "censor_table",
            "sourceId": f"censored_raw_{censored_rows[-1]['epoch']:06d}",
            "defaultSort": {"field": "epoch", "direction": "asc"},
            "columns": [
                {"field": "epoch", "label": "Eval", "format": "number"},
                {"field": "novel", "label": "Novel", "format": "number"},
                {"field": "candidate", "label": "Candidate", "type": "text"},
                {"field": "closed_bins", "label": "Closed bins", "format": "number"},
                {"field": "prefix_mean_n", "label": "Prefix mean [N]", "format": "number"},
                {"field": "causal_lower_bound_n", "label": "sum(error)/550 [N]", "format": "number"},
                {"field": "trigger_time_s", "label": "Trigger [s]", "format": "number"},
                {"field": "saved_path_time_s", "label": "Saved [s]", "format": "number"},
            ],
        })
    if calibration_rows:
        tables.append({
            "id": "gp_calibration_detail",
            "title": "Noise-aware GP calibration detail",
            "dataset": "gp_calibration",
            "sourceId": optimizer_source,
            "columns": [
                {"field": "candidate_token", "label": "Candidate", "type": "text"},
                {"field": "observed_mean_n", "label": "Observed mean [N]", "format": "number"},
                {"field": "posterior_mean_n", "label": "Posterior mean [N]", "format": "number"},
                {"field": "posterior_sigma_n", "label": "Posterior sigma [N]", "format": "number"},
                {"field": "standardized_residual", "label": "Std residual", "format": "number"},
                {"field": "group_n", "label": "n", "format": "number"},
                {"field": "empirical_yvar_n2", "label": "Yvar [N2]", "format": "number"},
            ],
        })

    blocks = [
        {"id": "title", "type": "markdown", "body": f"# Figure-eight BO Autotuner 技术报告\n\n`r013-figure8-direct-campaign-v1` · {status_label} · 独立 Step6 lineage。"},
            {"id": "technical_summary", "type": "markdown", "sourceId": primary_source, "body": f"## 技术结论\n\n当前取得 **{int(scheduler['novel_count'])}/200** 个 trial-admitted Exact novel candidates；novel dispatch 为 **{dispatch_count}**。最低单次 sealed MAE 为 **{float(single['objective_n']):.4f} N**。{'Repeated incumbent 为 **'+format(repeated_mean,'.4f')+' +/- '+format(repeated_std,'.4f')+' N (n='+str(len(robust_rows))+')**。' if repeated_available else 'Repeated incumbent 尚未满足 n>=3，不能确认。'} Campaign 没有 absolute-MAE stop；trial censor 只缩短明显较差的普通 novel trial。"},
        {"id": "headline_metrics", "type": "metric-strip", "cardIds": [card["id"] for card in cards]},
        {"id": "identity_scope", "type": "markdown", "sourceId": primary_source, "body": f"## Identity 与 evidence scope\n\nFingerprint `{scheduler.get('campaign_fingerprint_sha256')}`；path 60 s，formal window `[5,60)`，550 个 0.1 s bins。Cycloid n=3 只提供 provisional warm-start center，旧 cycloid phase correction 不进入本 GP。图表来自 cold-readable raw/lifecycle/scheduler evidence；有界展示样本不改变 authoritative raw files。"},
            {"id": "bo_progress_finding", "type": "markdown", "sourceId": primary_source, "body": "## BO 进展判断\n\nExact、repeat/sentinel、censored prefix 与 trial rejection 分开显示。只有 finite、550-bin、trial-admitted Exact observations 更新 observed best-so-far。Censored prefix mean 不是 sealed MAE，也不是 GP penalty。竖向 reference lines 是实际 phase boundaries；本 campaign 没有 absolute-MAE acceptance line。"},
        {"id": "bo_progress_figure", "type": "chart", "chartId": "bo_evaluation_scatter"},
        {"id": "bo_best_so_far_figure", "type": "chart", "chartId": "bo_best_step"},
    ]
    if censored_prefix_visual:
        blocks.append({"id": "censored_prefix_figure", "type": "chart", "chartId": "censored_prefix_force"})
    blocks.extend([
        {"id": "best_diagnostics_finding", "type": "markdown", "sourceId": "best_lifecycle", "body": f"## 最优 trial 与 pose diagnostics\n\nBest single lifecycle 保留 SEARCH -> handoff -> PATH -> return/Home。Force 使用散点避免跨越真实 gap；authoritative lifecycle 未降采样，report snapshot 仅做 deterministic bounded display。Nominal sampling 500 Hz，observed PATH {evidence['best_trial']['sampling']['observed_hz']:.1f} Hz。Orientation 显示 deployed fixed-rotvec Euclidean proxy，不冒充 roll-invariant metric。"},
        {"id": "best_diagnostics_figure", "type": "chart", "chartId": "best_force_lifecycle"},
        {"id": "best_xy_tracking_figure", "type": "chart", "chartId": "best_xy_tracking"},
        {"id": "best_position_error_figure", "type": "chart", "chartId": "best_position_error"},
        {"id": "best_orientation_error_figure", "type": "chart", "chartId": "best_orientation_error"},
        {"id": "repeatability_finding", "type": "markdown", "sourceId": repeat_source, "body": f"## Repeatability 与 local residual\n\nRobust candidate `{_token(robust_key)}` 的 Exact repeats 为 {', '.join(format(value,'.4f') for value in repeat_values)} N。Mean={repeated_mean:.4f} N，std={repeated_std:.4f} N，range=[{min(repeat_values):.4f},{max(repeat_values):.4f}] N。所有 repeat 单独显示，不用 envelope 隐藏 outlier。"},
        {"id": "repeatability_figure", "type": "chart", "chartId": "robust_repeat_force"},
        {"id": "segment_residual_figure", "type": "chart", "chartId": "segment_residuals"},
        {"id": "evaluation_table", "type": "table", "tableId": "evaluation_detail"},
    ])
    if censored_rows:
        blocks.append({"id": "censor_receipt_table", "type": "table", "tableId": "censor_detail"})
    blocks.extend([
        {"id": "metric_acceptance_table", "type": "table", "tableId": "metric_contract_detail"},
        {"id": "candidate_optimizer_table", "type": "table", "tableId": "candidate_optimizer_detail"},
    ])
    if calibration_rows:
        blocks.extend([
            {"id": "gp_calibration_figure", "type": "chart", "chartId": "gp_calibration_scatter"},
            {"id": "gp_calibration_table", "type": "table", "tableId": "gp_calibration_detail"},
        ])
    blocks.extend([
        {"id": "qualification_closeout_table", "type": "table", "tableId": "campaign_closeout_detail"},
        {"id": "limitations", "type": "markdown", "body": "## Limitations\n\n这是固定 runtime/fingerprint 与 200-Exact budget 下的 empirical result，不是 mathematical global optimum。Physical execution 保持 single writer、serial q=1；async 只在 safe return 阶段预取一条 `X_pending` proposal，下一 ARM 仍等待当前 Home/admission closure。Censored rows 是 heuristic nontrainable evidence。Cycloid MAE、Figure-eight MAE 与 ext60 不是同一统计总体。"},
        {"id": "next_steps", "type": "markdown", "body": "## Next steps\n\n" + ("冻结 robust Figure-eight incumbent，并把 fingerprint、noise model、完整 ledger 与 report package 作为后续部署输入。" if final else "保持同 fingerprint 继续 campaign，直到 200 个 Exact novel candidates，并把 observed top three 全部补到 n=5。")},
    ])

    datasets, dataset_source_ids, query_sources = (
        _sql_materialize_snapshot_datasets(
            datasets,
            upstream_sources=sources,
            query_dir=report_dir / "queries",
            state_root=root,
        )
    )
    sources.extend(query_sources)
    evidence["sources"] = sources
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    for item in [*cards, *charts, *tables]:
        item["sourceId"] = dataset_source_ids[str(item["dataset"])]
    manifest_sources.extend(
        {
            "id": source["id"],
            "label": source["label"],
            "path": source["path"],
        }
        for source in query_sources
    )

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": f"Figure-eight BO Autotuner — {status_label}",
            "description": "V4-equivalent Figure-eight 200-Exact BO campaign with active trial-level MAE censor.",
            "generatedAt": evidence["report"]["generated_at"],
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": manifest_sources,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": evidence["report"]["generated_at"],
            "status": "ready",
            "datasets": datasets,
        },
        "sources": [
            {
                **source,
                "label": source["id"].replace("_", " "),
            }
            for source in sources
        ],
    }
    artifact_path = report_dir / "artifact.json"
    artifact_path.write_text(
        json.dumps(artifact, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    analytics_plugin_root, portable_delivery_script = (
        resolve_portable_report_builder()
    )
    report_path = report_dir / "report.html"
    delivered = subprocess.run(
        [
            "node",
            str(portable_delivery_script),
            "--input",
            str(artifact_path),
            "--output",
            str(report_path),
        ],
        cwd=analytics_plugin_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180.0,
        check=False,
    )
    if delivered.returncode != 0 or not report_path.is_file():
        raise FigureEightReportError(
            "canonical portable report delivery failed: "
            + (delivered.stdout.strip() or delivered.stderr.strip())
        )
    delivery_receipt_path = report_dir / "portable_delivery_receipt.json"
    delivery_text = delivered.stdout.strip()
    try:
        delivery_receipt = json.loads(delivery_text)
    except json.JSONDecodeError:
        try:
            delivery_receipt = json.loads(delivery_text.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise FigureEightReportError(
                "canonical portable builder did not emit a JSON receipt"
            ) from exc
    delivery_receipt_path.write_text(
        json.dumps(delivery_receipt, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    validation = validate_figure8_report(
        evidence_path=evidence_path,
        html_path=report_path,
        final=final,
    )
    validation_path = report_dir / "figure8_report_validation.json"
    validation_path.write_text(json.dumps(validation, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if not validation["passed"]:
        raise FigureEightReportError(
            "Figure-eight structural report validation failed: "
            + "; ".join(validation["errors"])
        )
    if not CANONICAL_VALIDATOR.is_file():
        raise FigureEightReportError("canonical Autotuner report validator is unavailable")
    canonical_receipt = report_dir / "autotuner_qa_receipt.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(CANONICAL_VALIDATOR),
            "--evidence",
            str(evidence_path),
            "--artifact",
            str(artifact_path),
            "--html",
            str(report_path),
            "--receipt",
            str(canonical_receipt),
            "--strict",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60.0,
        check=False,
    )
    if completed.returncode != 0:
        raise FigureEightReportError(
            "canonical Autotuner report validation failed: "
            + (completed.stdout.strip() or completed.stderr.strip())
        )
    return {
        "schema": "step6.autotune/figure8-report-build-result-v1",
        "passed": True,
        "checkpoint_novel": checkpoint_novel,
        "final": final,
        "evidence": str(evidence_path),
        "artifact": str(artifact_path),
        "html": str(report_path),
        "validation": str(validation_path),
        "canonical_qa": str(canonical_receipt),
        "portable_delivery_receipt": str(delivery_receipt_path),
        "html_sha256": _sha256(report_path),
    }


def validate_figure8_report(*, evidence_path: Path, html_path: Path, final: bool) -> dict[str, Any]:
    evidence = _read_json(evidence_path)
    document = Path(html_path).read_text(encoding="utf-8")
    errors = []
    positions = []
    for block in REQUIRED_BLOCKS:
        marker = f'data-artifact-block-id="{block}"'
        position = document.find(marker)
        if position < 0:
            errors.append(f"missing report block: {block}")
        positions.append(position)
    if positions != sorted(positions):
        errors.append("report blocks are out of fixed order")
    if re.search(r"(?:src|href)\s*=\s*['\"]https?://", document, re.IGNORECASE):
        errors.append("report contains external HTTP(S) dependency")
    sealed = evidence.get("metric_contract", {}).get("sealed", {})
    if sealed.get("window_s") != [FIGURE8_FORMAL_START_S, FIGURE8_DURATION_S] or sealed.get("bin_count") != FIGURE8_FORMAL_BIN_COUNT:
        errors.append("Figure-eight metric window/bin contract differs")
    evaluations = evidence.get("evaluations", [])
    admitted = [row for row in evaluations if row.get("admitted") is True]
    if not admitted:
        errors.append("report has no admitted evaluations")
    else:
        best = min(admitted, key=lambda row: float(row["objective_n"]))
        single = evidence.get("incumbent", {}).get("single_trial", {})
        if best.get("epoch") != single.get("epoch") or not math.isclose(
            float(best["objective_n"]), float(single.get("sealed_mae_n", math.nan))
        ):
            errors.append("single minimum differs from admitted evaluation rows")
    repeated = evidence.get("incumbent", {}).get("repeated", {})
    if final and int(repeated.get("n", 0)) < 5:
        errors.append("final robust incumbent has fewer than five repeats")
    top_three = evidence.get("incumbent", {}).get("top_three", [])
    if final and (
        len(top_three) != 3
        or any(int(row.get("n", 0)) < 5 for row in top_three)
    ):
        errors.append("final observed top three are not all n=5")
    if final and int(evidence.get("optimizer", {}).get("exact_novel_target", -1)) != 200:
        errors.append("final exact novel target differs from 200")
    if evidence.get("report", {}).get("target_achieved_claim") is not False:
        errors.append("Figure-eight report made a target-achievement claim")
    for source in evidence.get("sources", []):
        path = Path(source["path"])
        resolved = path if path.is_absolute() else Path(evidence_path).parents[2] / path
        if not resolved.is_file() or _sha256(resolved) != source.get("sha256"):
            errors.append(f"source binding differs: {source.get('id')}")
    return {
        "schema": "step6.autotune/figure8-report-validation-v1",
        "passed": not errors,
        "errors": errors,
        "evidence_sha256": _sha256(evidence_path),
        "html_sha256": _sha256(html_path),
        "required_block_order": list(REQUIRED_BLOCKS),
        "external_http_dependencies": False,
        "final": bool(final),
    }


def _open_final_report_in_chrome(report_path: Path) -> dict[str, Any]:
    if not CHROME_OPENER.is_file():
        raise FigureEightReportError(
            "verified local Chrome opener is unavailable for final report"
        )
    completed = subprocess.run(
        [sys.executable, str(CHROME_OPENER), "--json", str(Path(report_path).resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120.0,
        check=False,
    )
    try:
        receipt = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise FigureEightReportError(
            "verified Chrome opener did not emit a JSON receipt: "
            + (completed.stdout.strip() or completed.stderr.strip())
        ) from exc
    artifacts = receipt.get("artifacts", ())
    if (
        completed.returncode != 0
        or receipt.get("ok") is not True
        or receipt.get("visible_verified") is not True
        or len(artifacts) != 1
        or artifacts[0].get("visible_verified") is not True
        or Path(str(artifacts[0].get("path", ""))).resolve()
        != Path(report_path).resolve()
    ):
        raise FigureEightReportError(
            "final report was not verified in a visible Chrome window: "
            + json.dumps(receipt, sort_keys=True)
        )
    return receipt


def checkpoint_report_callback(
    state_root: Path,
    novel: int,
    checkpoint: Mapping[str, Any],
) -> None:
    final = bool(checkpoint.get("final"))
    result = build_figure8_report(
        state_root,
        checkpoint_novel=int(novel),
        final=final,
    )
    if final:
        receipt = _open_final_report_in_chrome(Path(result["html"]))
        receipt_path = Path(result["html"]).with_name("chrome_open_receipt.json")
        receipt_path.write_text(
            json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )


__all__ = [
    "FigureEightReportError",
    "PORTABLE_BUILDER_ENV",
    "REPORT_SCHEMA",
    "build_figure8_report",
    "checkpoint_report_callback",
    "resolve_portable_report_builder",
    "validate_figure8_report",
]
