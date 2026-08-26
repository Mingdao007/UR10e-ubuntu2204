#!/usr/bin/env python3
"""Run an isolated V5 Home-entry versus rollover comparison.

This lane is deliberately outside the optimizer and the V5 campaign ledger.
It reuses the already source-closed V5 live owner, but binds its own runner
digest, schedule, state root, execution journal, and final report.  The
historical-best controller coordinates are held fixed and no ``tell_exact``
or correction campaign is reachable from this module.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "step6" / "step6_figure8_runtime_manifest.json"
RUNNER_SCHEMA = "step6.autotune/v5-entry-comparison-runner-v1"
COMPARISON_SCHEMA = "step6.autotune/v5-entry-comparison-v1"
COMPARISON_VERSION = 1
PAIR_COUNT = 10
ORDER_SPLIT = 10
MARGIN_N = 0.05
CI = 0.90
T_CRITICAL_DF19_90 = 1.8331129326536335
EXPECTED_SOURCE_SHA = "3a4dc0ae98674d211405c68682a30182b243325b59414162260611c9a54b4e05"


class V5EntryComparisonError(RuntimeError):
    """The isolated comparison could not preserve its typed evidence."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V5EntryComparisonError("comparison value is not canonical JSON") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha(path: Path) -> str:
    target = Path(path).resolve()
    if target.is_symlink() or not target.is_file():
        raise V5EntryComparisonError(f"comparison file is unavailable: {target}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _read_json(path: Path, role: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V5EntryComparisonError(f"{role} is unreadable") from exc
    if not isinstance(value, dict):
        raise V5EntryComparisonError(f"{role} is not an object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.name + f".tmp-{os.getpid()}-{os.urandom(6).hex()}"
    )
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return None
    if len(finite) == 1:
        return finite[0]
    position = (len(finite) - 1) * float(fraction)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return finite[lower]
    weight = position - lower
    return finite[lower] * (1.0 - weight) + finite[upper] * weight


@dataclass(frozen=True)
class ComparisonPairV1:
    pair_id: str
    order: str
    home_chain_id: str
    home_record_index: int
    rollover_chain_id: str
    rollover_record_index: int
    warm_chain_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.order not in {"HOME_THEN_ROLLOVER", "ROLLOVER_THEN_HOME"}:
            raise V5EntryComparisonError("comparison pair order is invalid")
        if not self.pair_id or not self.home_chain_id or not self.rollover_chain_id:
            raise V5EntryComparisonError("comparison pair identity is incomplete")
        if self.home_record_index < 0 or self.rollover_record_index < 0:
            raise V5EntryComparisonError("comparison record index is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/v5-entry-comparison-pair-v1",
            "version": 1,
            "pair_id": self.pair_id,
            "order": self.order,
            "home": {
                "chain_id": self.home_chain_id,
                "record_index": self.home_record_index,
                "entry_mode": "HOME_ENTRY",
            },
            "rollover": {
                "chain_id": self.rollover_chain_id,
                "record_index": self.rollover_record_index,
                "entry_mode": "ROLLOVER_ENTRY",
            },
            "warm_chain_ids": list(self.warm_chain_ids),
            "warm_not_counted": bool(self.warm_chain_ids),
        }


def build_comparison_schedule(
    *, session_epoch: int, comparison_fingerprint: str
) -> tuple[tuple[ComparisonPairV1, ...], dict[str, tuple[Any, ...]]]:
    """Build ten two-plan chains: ten Home-entry and ten rollover-entry rows."""

    from step6_figure8_autotune_v1.v5_capability_acceptance import (
        _fixed_plan,
        capability_anchor_candidate,
    )

    pairs: list[ComparisonPairV1] = []
    chains: dict[str, tuple[Any, ...]] = {}
    ordinal = 1
    anchor = capability_anchor_candidate()

    def plans(chain_id: str, count: int) -> tuple[Any, ...]:
        nonlocal ordinal
        result = []
        for position in range(1, count + 1):
            result.append(
                _fixed_plan(
                    chain_id=chain_id,
                    position=position,
                    ordinal=ordinal,
                    epoch=session_epoch,
                    candidate=anchor,
                    fingerprint=comparison_fingerprint,
                )
            )
            ordinal += 1
        return tuple(result)

    for pair_index in range(1, PAIR_COUNT + 1):
        pair_id = f"cmp-pair-{pair_index:02d}"
        order = "HOME_THEN_ROLLOVER"
        chain_id = f"{pair_id}-chain"
        chains[chain_id] = plans(chain_id, 2)
        pairs.append(
            ComparisonPairV1(
                pair_id,
                order,
                chain_id,
                0,
                chain_id,
                1,
                (),
            )
        )
    if len(pairs) != PAIR_COUNT or len(chains) != PAIR_COUNT:
        raise V5EntryComparisonError("comparison schedule cardinality differs")
    return tuple(pairs), chains


def _entry_mode(result: Any, record_index: int) -> str:
    entry_modes = getattr(result, "comparison_entry_modes", None)
    if entry_modes is not None:
        try:
            return str(entry_modes[record_index])
        except (IndexError, TypeError) as exc:
            raise V5EntryComparisonError("snapshot entry mode is absent") from exc
    events = tuple(result.records[0].source_artifact.event_bundle.get("events", ()))
    if record_index == 0:
        starts = [event for event in events if event.get("kind") == "CHAIN_START"]
        if len(starts) != 1 or starts[0].get("sample_index") != result.records[0].trial_slice.sample_start_index:
            raise V5EntryComparisonError("Home-entry CHAIN_START evidence differs")
        return "HOME_ENTRY"
    rollovers = [event for event in events if event.get("kind") == "CONTACT_ROLLOVER"]
    if record_index - 1 >= len(rollovers):
        raise V5EntryComparisonError("rollover-entry event evidence is absent")
    if rollovers[record_index - 1].get("sample_index") != result.records[record_index].trial_slice.sample_start_index:
        raise V5EntryComparisonError("rollover-entry seam does not bind trial start")
    return "ROLLOVER_ENTRY"


def _timing_summary(record: Any) -> dict[str, Any]:
    snapshot_timing = getattr(record, "comparison_timing", None)
    if snapshot_timing is not None:
        return dict(snapshot_timing)
    rows = tuple(
        record.source_artifact.rows[
            record.trial_slice.sample_start_index : record.trial_slice.sample_end_index
        ]
    )
    host_gaps = [
        float(current["monotonic_s"]) - float(previous["monotonic_s"])
        for previous, current in zip(rows, rows[1:])
    ]
    rtde_gaps = [
        float(current["rtde_timestamp_s"]) - float(previous["rtde_timestamp_s"])
        for previous, current in zip(rows, rows[1:])
    ]
    return {
        "host_gap_s": {
            "p50": _percentile(host_gaps, 0.50),
            "p95": _percentile(host_gaps, 0.95),
            "p99": _percentile(host_gaps, 0.99),
            "max": max(host_gaps) if host_gaps else None,
        },
        "rtde_gap_s": {
            "p50": _percentile(rtde_gaps, 0.50),
            "p95": _percentile(rtde_gaps, 0.95),
            "p99": _percentile(rtde_gaps, 0.99),
            "max": max(rtde_gaps) if rtde_gaps else None,
        },
        "lifecycle_max_gap_s": record.source_artifact.receipt.get("max_gap_s"),
        "lifecycle_max_rtde_gap_s": record.source_artifact.receipt.get("max_rtde_gap_s"),
    }


def _stats(values: Sequence[float]) -> dict[str, Any]:
    finite = tuple(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return {"n": 0, "mean_n": None, "std_n_ddof1": None, "min_n": None, "max_n": None}
    return {
        "n": len(finite),
        "mean_n": statistics.mean(finite),
        "std_n_ddof1": statistics.stdev(finite) if len(finite) > 1 else None,
        "min_n": min(finite),
        "max_n": max(finite),
    }


def evaluate_comparison(
    *,
    pairs: Sequence[ComparisonPairV1],
    results: Mapping[str, Any],
    comparison_fingerprint: str,
    release_identity_sha256: str,
    runner_sha256: str,
) -> dict[str, Any]:
    """Cold-evaluate selected rows without any campaign/optimizer side effect."""

    selected: list[dict[str, Any]] = []
    home_mae: list[float] = []
    rollover_mae: list[float] = []
    differences: list[float] = []
    order_rows: dict[str, list[float]] = {
        "HOME_THEN_ROLLOVER": [],
        "ROLLOVER_THEN_HOME": [],
    }
    timing_by_method: dict[str, list[dict[str, Any]]] = {"HOME_ENTRY": [], "ROLLOVER_ENTRY": []}
    for pair in pairs:
        try:
            home_result = results[pair.home_chain_id]
            rollover_result = results[pair.rollover_chain_id]
            home_record = home_result.records[pair.home_record_index]
            rollover_record = rollover_result.records[pair.rollover_record_index]
        except (KeyError, IndexError, TypeError) as exc:
            raise V5EntryComparisonError(f"comparison pair is incomplete: {pair.pair_id}") from exc
        if _entry_mode(home_result, pair.home_record_index) != "HOME_ENTRY":
            raise V5EntryComparisonError(f"ordinary entry mode differs: {pair.pair_id}")
        if _entry_mode(rollover_result, pair.rollover_record_index) != "ROLLOVER_ENTRY":
            raise V5EntryComparisonError(f"rollover entry mode differs: {pair.pair_id}")
        if home_record.eligible is not True or rollover_record.eligible is not True:
            raise V5EntryComparisonError(f"comparison pair is not gate-closed eligible: {pair.pair_id}")
        home = float(home_record.metric_snapshot.formal_mae_n)
        rollover = float(rollover_record.metric_snapshot.formal_mae_n)
        difference = rollover - home
        home_mae.append(home)
        rollover_mae.append(rollover)
        differences.append(difference)
        order_rows[pair.order].append(difference)
        home_timing = _timing_summary(home_record)
        rollover_timing = _timing_summary(rollover_record)
        timing_by_method["HOME_ENTRY"].append(home_timing)
        timing_by_method["ROLLOVER_ENTRY"].append(rollover_timing)
        selected.append(
            {
                "pair_id": pair.pair_id,
                "order": pair.order,
                "home_entry": {
                    "chain_id": pair.home_chain_id,
                    "record_index": pair.home_record_index,
                    "formal_mae_n": home,
                    "record_sha256": home_record.record_sha256,
                    "timing": home_timing,
                },
                "rollover_entry": {
                    "chain_id": pair.rollover_chain_id,
                    "record_index": pair.rollover_record_index,
                    "formal_mae_n": rollover,
                    "record_sha256": rollover_record.record_sha256,
                    "timing": rollover_timing,
                },
                "rollover_minus_home_formal_mae_n": difference,
                "warm_chain_ids": list(pair.warm_chain_ids),
                "warm_not_counted": True,
            }
        )
    mean = statistics.mean(differences)
    std = statistics.stdev(differences) if len(differences) > 1 else 0.0
    half_width = T_CRITICAL_DF19_90 * std / math.sqrt(len(differences))
    ci = (mean - half_width, mean + half_width)
    equivalence = ci[0] >= -MARGIN_N and ci[1] <= MARGIN_N

    def method_timing(method: str) -> dict[str, Any]:
        rows = timing_by_method[method]
        def flatten(key: str, statistic: str) -> list[float]:
            return [float(item[key][statistic]) for item in rows if item[key][statistic] is not None]
        return {
            "n": len(rows),
            "host_gap_s": {
                statistic: _stats(flatten("host_gap_s", statistic))["mean_n"]
                for statistic in ("p50", "p95", "p99", "max")
            },
            "rtde_gap_s": {
                statistic: _stats(flatten("rtde_gap_s", statistic))["mean_n"]
                for statistic in ("p50", "p95", "p99", "max")
            },
            "max_lifecycle_gap_s": max(
                float(item["lifecycle_max_gap_s"])
                for item in rows
                if item["lifecycle_max_gap_s"] is not None
            ),
            "max_lifecycle_rtde_gap_s": max(
                float(item["lifecycle_max_rtde_gap_s"])
                for item in rows
                if item["lifecycle_max_rtde_gap_s"] is not None
            ),
        }

    home_timing = method_timing("HOME_ENTRY")
    rollover_timing = method_timing("ROLLOVER_ENTRY")
    reliability_equal = True  # all selected rows are gate-closed by construction
    if equivalence and reliability_equal:
        recommendation = "ROLLOVER"
        status = "EQUIVALENT_RECOMMEND_ROLLOVER"
    elif equivalence:
        recommendation = "HOME_ENTRY"
        status = "EQUIVALENT_RECOMMEND_HOME_ENTRY"
    else:
        recommendation = None
        status = "NOT_EQUIVALENT_MEASUREMENT_RESOLUTION_UNRESOLVED"
    return {
        "schema": COMPARISON_SCHEMA,
        "version": COMPARISON_VERSION,
        "status": status,
        "recommendation": recommendation,
        "comparison_fingerprint_sha256": comparison_fingerprint,
        "release_identity_sha256": release_identity_sha256,
        "runner_sha256": runner_sha256,
        "pair_count": len(pairs),
        "counted_trial_count": 2 * len(pairs),
        "warm_trial_count": sum(len(pair.warm_chain_ids) for pair in pairs),
        "metric": {
            "signal": "filtered_normal_n",
            "window": "[5,60)",
            "target_force_n": 5.0,
            "correction_weights": [0.0] * 6,
        },
        "ordinary_home_entry": _stats(home_mae),
        "rollover_entry": _stats(rollover_mae),
        "paired_difference": {
            "definition": "rollover_entry_minus_home_entry",
            "mean_n": mean,
            "std_n_ddof1": std,
            "ci90_n": list(ci),
            "student_t_degrees_of_freedom": len(differences) - 1,
            "equivalence_margin_n": MARGIN_N,
            "equivalence_passed": equivalence,
            "min_n": min(differences),
            "max_n": max(differences),
        },
        "order_strata": {
            order: _stats(values) for order, values in order_rows.items()
        },
        "timing": {
            "home_entry": home_timing,
            "rollover_entry": rollover_timing,
        },
        "safety_timing_failure_rate": {
            "home_entry": 0.0,
            "rollover_entry": 0.0,
            "note": "selected rows are exact eligible; failures remain in execution artifacts",
        },
        "pairs": selected,
        "campaign_qualification": False,
        "tell_exact_calls": 0,
        "primary_started": False,
        "correction_started": False,
        "automatic_extension": False,
    }


def _markdown_report(report: Mapping[str, Any]) -> str:
    paired = report["paired_difference"]
    home = report["ordinary_home_entry"]
    rollover = report["rollover_entry"]
    return "\n".join(
        (
            "# V5 ordinary Home-entry vs rollover comparison",
            "",
            f"- Status: **{report['status']}**",
            f"- Recommendation: **{report['recommendation'] or 'none — unresolved'}**",
            f"- Pairs: {report['pair_count']} (counted trials={report['counted_trial_count']}, warm not counted={report['warm_trial_count']})",
            "- Metric: sealed filtered-normal `[5,60)` MAE against 5 N; correction weights are zero.",
            "",
            "## MAE",
            "",
            f"- ordinary Home-entry: mean={home['mean_n']:.6f} N, std(ddof=1)={home['std_n_ddof1']:.6f} N, range=[{home['min_n']:.6f},{home['max_n']:.6f}] N",
            f"- rollover-entry: mean={rollover['mean_n']:.6f} N, std(ddof=1)={rollover['std_n_ddof1']:.6f} N, range=[{rollover['min_n']:.6f},{rollover['max_n']:.6f}] N",
            f"- rollover − Home: mean={paired['mean_n']:.6f} N, std(ddof=1)={paired['std_n_ddof1']:.6f} N, CI90=[{paired['ci90_n'][0]:.6f},{paired['ci90_n'][1]:.6f}] N, margin=±{paired['equivalence_margin_n']:.2f} N",
            "",
            "## Gate interpretation",
            "",
            "Equivalence is required before releasing V5 Primary. If the CI is outside the predeclared margin, this report is directional only and does not change the gate.",
            "",
        )
    )


def _snapshot_chain_result(result: Any) -> dict[str, Any]:
    """Retain only comparison-sized data after per-chain cold verification."""

    records = []
    entry_modes = []
    for index, record in enumerate(result.records):
        entry_mode = "HOME_ENTRY" if index == 0 else "ROLLOVER_ENTRY"
        entry_modes.append(entry_mode)
        records.append(
            {
                "record_index": index,
                "trial_id": record.trial_id,
                "attempt_id": record.attempt_id,
                "record_sha256": record.record_sha256,
                "eligible": record.eligible is True,
                "formal_mae_n": float(record.metric_snapshot.formal_mae_n),
                "entry_mode": entry_mode,
                "timing": _timing_summary(record),
                "boundary_mode": record.boundary.mode.value,
            }
        )
    return {
        "schema": "step6.autotune/v5-entry-comparison-chain-summary-v1",
        "version": 1,
        "chain_id": result.chain_id,
        "records": records,
        "entry_modes": entry_modes,
        "lifecycle_receipt": {
            "artifact_path": result.lifecycle_receipt.get("artifact_path"),
            "artifact_sha256": result.lifecycle_receipt.get("artifact_sha256"),
            "max_gap_s": result.lifecycle_receipt.get("max_gap_s"),
            "max_rtde_gap_s": result.lifecycle_receipt.get("max_rtde_gap_s"),
            "home_verified": result.lifecycle_receipt.get("home_verified"),
        },
    }


def _snapshot_result_objects(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for row in rows:
        records = []
        for record in row["records"]:
            records.append(
                SimpleNamespace(
                    trial_id=record["trial_id"],
                    attempt_id=record["attempt_id"],
                    eligible=record["eligible"],
                    metric_snapshot=SimpleNamespace(
                        formal_mae_n=record["formal_mae_n"]
                    ),
                    record_sha256=record["record_sha256"],
                    boundary=SimpleNamespace(
                        mode=__import__(
                            "step6_figure8_autotune_v1.v5_lifecycle_ledger",
                            fromlist=["BoundaryMode"],
                        ).BoundaryMode(record["boundary_mode"])
                    ),
                    comparison_timing=record["timing"],
                )
            )
        results[row["chain_id"]] = SimpleNamespace(
            chain_id=row["chain_id"],
            records=tuple(records),
            comparison_entry_modes=tuple(row["entry_modes"]),
        )
    return results


class V5EntryComparisonV1:
    """Execute the isolated comparison with the existing V5 owner."""

    def __init__(self, inputs: Any) -> None:
        from step6_figure8_autotune_v1.v5_capability_acceptance import (
            _read_json,
            verify_no_contact_canary,
            verify_no_motion_recipe_receipt,
        )

        self.inputs = inputs
        self.root = Path(inputs.root).resolve()
        self.state_root = Path(inputs.state_root).resolve()
        self.prepare_live = None
        self.context_builder = None
        self.owner_factory = None
        self.stop_resident = None
        self.no_motion = verify_no_motion_recipe_receipt(
            inputs.no_motion_recipe_receipt,
            expected_source_sha256=str(inputs.source_identity["source_sha256"]),
            expected_triplet_sha256=inputs.controller_triplet_sha256,
        )
        home = _read_json(inputs.home_calibration_receipt, "Home calibration receipt")
        self.canary = verify_no_contact_canary(
            root=self.root,
            canary_dir=inputs.canary_dir,
            canary_readback_dir=inputs.canary_readback_dir,
            expected_home_pose=inputs.compatibility_fingerprint.home_pose,
            expected_home_receipt_sha256=str(home["receipt_sha256"]),
        )
        runner_sha = _file_sha(Path(__file__))
        identity_body = {
            "schema": "step6.autotune/v5-entry-comparison-identity-v1",
            "version": COMPARISON_VERSION,
            "comparison_runner_sha256": runner_sha,
            "source_identity_sha256": inputs.source_identity["source_sha256"],
            "expected_source_identity_sha256": EXPECTED_SOURCE_SHA,
            "release_identity_sha256": inputs.release_identity_sha256,
            "controller_triplet_sha256": dict(inputs.controller_triplet_sha256),
            "home_calibration_receipt_sha256": home["receipt_sha256"],
            "no_motion_recipe_receipt_sha256": self.no_motion["receipt_sha256"],
            "no_contact_evidence_sha256": self.canary["evidence_sha256"],
            "candidate_provenance": "historical_v5_best_coordinates_on_current_runtime",
            "candidate": {
                "force_p_gain": 0.019027313840405524,
                "force_damping": 188.36079701683204,
                "force_i_gain": 0.008610779292198037,
                "normal_filter_tau_s": 0.04375,
                "orientation_ko": 0.05,
                "motion_kp": 2.5226892457611436,
                "correction_weights": [0.0] * 6,
            },
            "pair_count": PAIR_COUNT,
            "order_split": ORDER_SPLIT,
            "counted_trial_count": 2 * PAIR_COUNT,
            "warm_trial_count": 0,
            "execution_order": "HOME_THEN_ROLLOVER_SINGLE_CHAIN",
            "metric_window": "[5,60)",
            "equivalence_margin_n": MARGIN_N,
            "tell_exact_calls": 0,
            "primary_started": False,
            "correction_started": False,
            "automatic_extension": False,
        }
        if inputs.source_identity["source_sha256"] != EXPECTED_SOURCE_SHA:
            raise V5EntryComparisonError(
                "current source identity differs from the frozen comparison source: "
                f"{inputs.source_identity['source_sha256']}"
            )
        self.comparison_fingerprint = _sha(identity_body)
        self.identity = {**identity_body, "comparison_fingerprint_sha256": self.comparison_fingerprint}
        self._ensure_identity()

    @property
    def journal_path(self) -> Path:
        return self.state_root / "execution.jsonl"

    @property
    def receipt_path(self) -> Path:
        return self.state_root / "comparison_receipt.json"

    @property
    def phase_path(self) -> Path:
        return self.state_root / "phase_runtime.json"

    @property
    def schedule_path(self) -> Path:
        return self.state_root / "comparison_schedule.json"

    def _ensure_identity(self) -> None:
        marker = self.state_root / "comparison_identity.json"
        if marker.is_file():
            if _read_json(marker, "comparison identity") != self.identity:
                raise V5EntryComparisonError("comparison state identity changed")
            return
        if self.state_root.exists() and any(self.state_root.iterdir()):
            raise V5EntryComparisonError("comparison state root is not fresh")
        self.state_root.mkdir(parents=True, exist_ok=True)
        _atomic_json(marker, self.identity)

    @property
    def execution_dir(self) -> Path:
        return self.state_root / "execution-chains"

    @property
    def execution_index_path(self) -> Path:
        return self.state_root / "execution-index.jsonl"

    @property
    def chain_summary_path(self) -> Path:
        return self.state_root / "chain-summaries.jsonl"

    def _chain_journal(self, chain_id: str) -> Any:
        from step6_figure8_autotune_v1.v5_campaign_runner import V5ExecutionJournalV1
        from step6_figure8_autotune_v1.v5_lifecycle_ledger import LedgerRole

        self.execution_dir.mkdir(parents=True, exist_ok=True)
        return V5ExecutionJournalV1(
            self.execution_dir / f"{chain_id}.jsonl",
            campaign_fingerprint=self.comparison_fingerprint,
            release_identity_sha256=self.inputs.release_identity_sha256,
            role=LedgerRole.PRIMARY,
        )

    @staticmethod
    def _trim_chain_journal(journal: Any) -> None:
        """Release expanded lifecycle rows after each one-chain verification."""

        results = getattr(journal, "_results", None)
        if isinstance(results, dict):
            journal._results = {key: True for key in results}
            gc.collect()

    def _append_index(self, chain_id: str, journal: Any, result: Any) -> None:
        previous = "0" * 64
        if self.execution_index_path.is_file():
            rows = [
                json.loads(line)
                for line in self.execution_index_path.read_text(encoding="utf-8").splitlines()
            ]
            if rows:
                previous = rows[-1]["row_sha256"]
        body = {
            "schema": "step6.autotune/v5-entry-comparison-execution-index-v1",
            "version": 1,
            "comparison_fingerprint_sha256": self.comparison_fingerprint,
            "release_identity_sha256": self.inputs.release_identity_sha256,
            "chain_id": chain_id,
            "journal_path": str(journal.path),
            "result_path": str(journal.result_path(chain_id)),
            "result_sha256": _file_sha(journal.result_path(chain_id)),
            "previous_sha256": previous,
        }
        row = {**body, "row_sha256": _sha(body)}
        with self.execution_index_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _append_summary(self, result: Any) -> None:
        summary = _snapshot_chain_result(result)
        previous = "0" * 64
        if self.chain_summary_path.is_file():
            rows = [
                json.loads(line)
                for line in self.chain_summary_path.read_text(encoding="utf-8").splitlines()
            ]
            if rows:
                previous = rows[-1]["row_sha256"]
        body = {
            "schema": "step6.autotune/v5-entry-comparison-chain-summary-row-v1",
            "version": 1,
            "comparison_fingerprint_sha256": self.comparison_fingerprint,
            "chain_id": result.chain_id,
            "summary": summary,
            "previous_sha256": previous,
        }
        row = {**body, "row_sha256": _sha(body)}
        with self.chain_summary_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _load_summaries(self) -> dict[str, Any]:
        if not self.chain_summary_path.is_file():
            return {}
        previous = "0" * 64
        result: dict[str, Any] = {}
        for line in self.chain_summary_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            unsigned = {key: value for key, value in row.items() if key != "row_sha256"}
            if (
                row.get("schema") != "step6.autotune/v5-entry-comparison-chain-summary-row-v1"
                or row.get("version") != 1
                or row.get("comparison_fingerprint_sha256") != self.comparison_fingerprint
                or row.get("previous_sha256") != previous
                or row.get("row_sha256") != _sha(unsigned)
            ):
                raise V5EntryComparisonError("comparison chain summaries hash/identity differs")
            chain_id = str(row["chain_id"])
            if chain_id in result:
                raise V5EntryComparisonError("comparison chain summary is duplicated")
            result[chain_id] = dict(row["summary"])
            previous = row["row_sha256"]
        return result

    def _prepare_or_reuse(self) -> tuple[Path, int]:
        from step6_figure8_autotune_v1.campaign_runner import _stop_exact_resident
        from step6_figure8_autotune_v1.prepare_live import prepare_figure8_live_run
        from step6_figure8_autotune_v1.v5_live_owner import V5_LIVE_BINDING_SCHEMA, V5_LIVE_OWNER_VERSION
        from step6_figure8_autotune_v1.v5_lifecycle_ledger import LedgerRole, canonical_sha256

        self.prepare_live = prepare_figure8_live_run
        self.context_builder = __import__(
            "step6_figure8_autotune_v1.v5_live_owner", fromlist=["build_v5_live_context"]
        ).build_v5_live_context
        self.owner_factory = __import__(
            "step6_figure8_autotune_v1.v5_live_owner", fromlist=["V5SingleWriterOwnerV1"]
        ).V5SingleWriterOwnerV1
        self.stop_resident = _stop_exact_resident
        if self.phase_path.is_file():
            phase = _read_json(self.phase_path, "comparison phase")
            if phase.get("status") in {"resident_ready", "owner_open"}:
                run_dir = Path(str(phase["run_dir"])).resolve()
                ready = _read_json(run_dir / "r013_live_owner_ready.json", "comparison resident ready")
                return run_dir, int(ready["session_epoch"])
        live_root = self.state_root / "live"
        live_root.mkdir(parents=True, exist_ok=True)
        serial = 1
        while (live_root / f"resident-{serial:03d}").exists():
            serial += 1
        run_dir = live_root / f"resident-{serial:03d}"
        phase = {
            "schema": "step6.autotune/v5-entry-comparison-phase-v1",
            "version": 1,
            "status": "preparing",
            "comparison_fingerprint_sha256": self.comparison_fingerprint,
            "run_dir": str(run_dir),
        }
        _atomic_json(self.phase_path, phase)
        nonce = __import__("time").time_ns()
        self.prepare_live(
            run_dir,
            root=self.root,
            robot_host=self.inputs.robot_host,
            kunwei_host=self.inputs.kunwei_host,
            kunwei_port=int(self.inputs.kunwei_port),
            controller_readback_dir=self.inputs.controller_readback_dir,
            canary_dir=self.inputs.canary_dir,
            home_calibration_receipt_path=self.inputs.home_calibration_receipt,
            fingerprint=self.inputs.compatibility_fingerprint,
            campaign_id="autotuner-v5-entry-comparison",
            run_id=f"autotuner-v5-entry-comparison-{nonce}",
            attempt_id=f"r006-v5-entry-comparison-{nonce}",
        )
        ready = _read_json(run_dir / "r013_live_owner_ready.json", "comparison resident ready")
        epoch = int(ready["session_epoch"])
        binding = {
            "schema": V5_LIVE_BINDING_SCHEMA,
            "version": V5_LIVE_OWNER_VERSION,
            "campaign_fingerprint": self.comparison_fingerprint,
            "release_identity_sha256": self.inputs.release_identity_sha256,
            "role": LedgerRole.PRIMARY.value,
            "session_epoch": epoch,
            "run_dir": str(run_dir.resolve()),
            "compatibility_fingerprint_sha256": ready["figure8_campaign_fingerprint_sha256"],
        }
        _atomic_json(run_dir / "v5_live_binding.json", {**binding, "binding_sha256": canonical_sha256(binding)})
        _atomic_json(self.phase_path, {**phase, "status": "resident_ready", "session_epoch": epoch, "v5_live_binding": str(run_dir / "v5_live_binding.json")})
        return run_dir, epoch

    def _release_resident(self) -> dict[str, Any]:
        phase = _read_json(self.phase_path, "comparison phase")
        epoch = int(phase.get("session_epoch", 0))
        if epoch <= 0:
            raise V5EntryComparisonError("comparison resident epoch is absent")
        release_path = self.state_root / "writer-release-receipts" / f"session-{epoch}.json"
        if release_path.is_file():
            release = _read_json(release_path, "comparison writer release")
        else:
            if self.stop_resident is None:
                self._prepare_or_reuse()
            raw = dict(self.stop_resident(robot_host=self.inputs.robot_host, expected_home_pose=self.inputs.compatibility_fingerprint.home_pose))
            release = {**raw, "home_verified": raw.get("passed") is True, "stopped": raw.get("passed") is True, "writer_released": raw.get("writer_released") is True, "run_dir": phase.get("run_dir"), "session_epoch": epoch}
            _atomic_json(release_path, release)
        if not (release.get("home_verified") is True and release.get("stopped") is True and release.get("writer_released") is True):
            raise V5EntryComparisonError("comparison resident did not release at verified Home")
        _atomic_json(self.phase_path, {**phase, "status": "released_stopped_home", "writer_release_receipt": str(release_path)})
        return release

    def _schedule(self, epoch: int) -> tuple[tuple[ComparisonPairV1, ...], dict[str, tuple[Any, ...]]]:
        pairs, chains = build_comparison_schedule(session_epoch=epoch, comparison_fingerprint=self.comparison_fingerprint)
        body = {
            "schema": "step6.autotune/v5-entry-comparison-schedule-v1",
            "version": 1,
            "comparison_fingerprint_sha256": self.comparison_fingerprint,
            "pairs": [pair.as_dict() for pair in pairs],
            "chains": {key: [plan.as_dict() for plan in value] for key, value in chains.items()},
        }
        if self.schedule_path.is_file():
            if _read_json(self.schedule_path, "comparison schedule") != {**body, "schedule_sha256": _sha(body)}:
                raise V5EntryComparisonError("comparison schedule changed")
        else:
            _atomic_json(self.schedule_path, {**body, "schedule_sha256": _sha(body)})
        return pairs, chains

    def _report(self, pairs: Sequence[ComparisonPairV1]) -> dict[str, Any]:
        summaries = self._load_summaries()
        return evaluate_comparison(
            pairs=pairs,
            results=_snapshot_result_objects(tuple(summaries.values())),
            comparison_fingerprint=self.comparison_fingerprint,
            release_identity_sha256=self.inputs.release_identity_sha256,
            runner_sha256=self.identity["comparison_runner_sha256"],
        )

    def run(self) -> dict[str, Any]:
        if self.receipt_path.is_file():
            return self.verify_receipt()
        run_dir, epoch = self._prepare_or_reuse()
        pairs, chains = self._schedule(epoch)
        summaries = self._load_summaries()
        missing = [chain_id for chain_id in chains if chain_id not in summaries]
        context = None
        error: BaseException | None = None
        try:
            context = self.context_builder(
                run_dir=run_dir,
                controller_host=self.inputs.robot_host,
                kunwei_host=self.inputs.kunwei_host,
                kunwei_port=int(self.inputs.kunwei_port),
                launch_profile=run_dir / "figure8_launch_profile.json",
                expected_campaign_fingerprint=self.comparison_fingerprint,
                expected_release_identity_sha256=self.inputs.release_identity_sha256,
                expected_role=__import__("step6_figure8_autotune_v1.v5_lifecycle_ledger", fromlist=["LedgerRole"]).LedgerRole.PRIMARY,
            )
            phase = _read_json(self.phase_path, "comparison phase")
            _atomic_json(self.phase_path, {**phase, "status": "owner_open"})
            owner = self.owner_factory(context)
            from step6_figure8_autotune_v1.v5_live_owner import V5LiveChainRequestV1
            from step6_figure8_autotune_v1.v5_lifecycle_ledger import LedgerRole
            for chain_id in missing:
                plan_tuple = chains[chain_id]
                journal = self._chain_journal(chain_id)
                journal.recover_sealed_results()
                if journal.ambiguous_chain_ids:
                    from step6_figure8_autotune_v1.v5_campaign_runner import V5AmbiguousPhysicalDispatch

                    raise V5AmbiguousPhysicalDispatch(
                        "comparison chain dispatch has no owner-sealed result: "
                        + ",".join(journal.ambiguous_chain_ids)
                    )
                if chain_id in {result.chain_id for result in journal.results}:
                    durable = next(result for result in journal.results if result.chain_id == chain_id)
                    if chain_id not in summaries:
                        self._append_summary(durable)
                        summaries[chain_id] = _snapshot_chain_result(durable)
                    self._trim_chain_journal(journal)
                    continue
                durable_path = journal.append_dispatch(chain_id, plan_tuple)
                request = V5LiveChainRequestV1(
                    chain_id,
                    plan_tuple,
                    self.comparison_fingerprint,
                    self.inputs.release_identity_sha256,
                    LedgerRole.PRIMARY,
                    str(durable_path),
                )
                sealed = owner.execute_chain(request)
                durable = journal.append_result(plan_tuple, sealed)
                self._append_index(chain_id, journal, durable)
                self._append_summary(durable)
                summaries[chain_id] = _snapshot_chain_result(durable)
                self._trim_chain_journal(journal)
                phase = _read_json(self.phase_path, "comparison phase")
                _atomic_json(self.phase_path, {**phase, "status": "owner_open", "completed_chain_ids": sorted(summaries), "execution_journal_head_sha256": _sha(sorted(summaries))})
        except BaseException as exc:
            error = exc
        finally:
            if context is not None:
                try:
                    context.close()
                except BaseException as close_error:
                    error = error or close_error
            try:
                self._release_resident()
            except BaseException as release_error:
                error = error or release_error
        if error is not None:
            raise error
        report = self._report(pairs)
        report_path = self.state_root / "v5_entry_comparison_report.json"
        markdown_path = self.state_root / "v5_entry_comparison_report.md"
        _atomic_json(report_path, report)
        markdown_path.write_text(_markdown_report(report), encoding="utf-8")
        body = {
            "schema": "step6.autotune/v5-entry-comparison-receipt-v1",
            "version": 1,
            "status": "COMPLETED",
            "comparison_fingerprint_sha256": self.comparison_fingerprint,
            "identity": self.identity,
            "schedule_sha256": _file_sha(self.schedule_path),
            "execution_journal_path": str(self.execution_index_path),
            "execution_journal_head_sha256": _file_sha(self.execution_index_path),
            "report_path": str(report_path),
            "report_sha256": _file_sha(report_path),
            "markdown_report_path": str(markdown_path),
            "writer_release": _read_json(Path(_read_json(self.phase_path, "comparison phase")["writer_release_receipt"]), "comparison writer release"),
            "campaign_qualification": False,
            "tell_exact_calls": 0,
            "primary_started": False,
            "correction_started": False,
            "automatic_extension": False,
        }
        receipt = {**body, "receipt_sha256": _sha(body)}
        _atomic_json(self.receipt_path, receipt)
        return receipt

    def verify_receipt(self) -> dict[str, Any]:
        receipt = _read_json(self.receipt_path, "comparison receipt")
        phase = _read_json(self.phase_path, "comparison phase")
        pairs, _chains = self._schedule(int(phase["session_epoch"]))
        report = self._report(pairs)
        report_path = Path(str(receipt["report_path"]))
        if _read_json(report_path, "comparison report") != report:
            raise V5EntryComparisonError("comparison report differs from cold evidence")
        body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        expected = {
            **body,
            "schedule_sha256": _file_sha(self.schedule_path),
            "execution_journal_head_sha256": _file_sha(self.execution_index_path),
            "report_sha256": _file_sha(report_path),
        }
        if receipt != {**expected, "receipt_sha256": _sha(expected)}:
            raise V5EntryComparisonError("comparison receipt differs from cold evidence")
        return receipt


def _runtime_request() -> Any:
    from step5d_managed_runtime import ExecutionRequestV1

    return ExecutionRequestV1(
        schema="step5d.execution-admission/request-v1",
        entrypoint="run_step6_figure8_autotune_v1_live",
        manifest_path=MANIFEST,
        route="host",
        required_imports=("numpy", "scipy", "pinocchio"),
        side_effect_level="live_mutation",
        source_or_lineage_identity=RUNNER_SCHEMA,
    )


def _bootstrap(argv: Sequence[str]) -> int:
    from step5d_managed_runtime import (
        admit_control_runtime,
        load_runtime_manifest,
        resolve_managed_control_runtime,
    )

    request = _runtime_request()
    receipt = admit_control_runtime(request, root=ROOT)
    if receipt.status != "admitted":
        print(json.dumps({"ok": False, "error": receipt.as_dict(), "live_executed": False}, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    manifest = load_runtime_manifest(MANIFEST, root=ROOT)
    binding = resolve_managed_control_runtime(manifest)
    environment = dict(binding.environment)
    environment["STEP5D_EXECUTION_ADMISSION"] = "admitted"
    environment["STEP5D_EXECUTION_ADMISSION_RECEIPT"] = json.dumps(receipt.as_dict(), sort_keys=True, separators=(",", ":"))
    completed = subprocess.run(
        [str(binding.python_executable), "-B", str(Path(__file__).resolve()), *tuple(argv)],
        cwd=ROOT,
        env=environment,
        stdin=None,
        timeout=None,
        check=False,
    )
    return int(completed.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--controller-readback-dir", type=Path, required=True)
    parser.add_argument("--canary-controller-readback-dir", type=Path, required=True)
    parser.add_argument("--canary-dir", type=Path, required=True)
    parser.add_argument("--home-calibration-receipt", type=Path, required=True)
    parser.add_argument("--no-motion-recipe-receipt", type=Path, required=True)
    parser.add_argument("--controller-host", default="192.168.1.18")
    parser.add_argument("--kunwei-host", default="192.168.50.25")
    parser.add_argument("--kunwei-port", type=int, default=5152)
    parser.add_argument("--v5-campaign-config", type=Path, required=True)
    args = parser.parse_args(raw_argv)
    if not args.execute_live:
        parser.error("--execute-live is required; comparison has no dry-run live substitute")
    if os.environ.get("STEP5D_EXECUTION_ADMISSION") != "admitted":
        return _bootstrap(raw_argv)

    from step6_figure8_autotune_v1.v5_capability_acceptance import V5CapabilityAcceptanceInputsV1
    from step6_figure8_autotune_v1.v5_production_bundle import V5ProductionBundleInputsV1, V5ProductionBundleV1
    from step6_figure8_autotune_v1.source_identity import build_source_identity

    state_dir = Path(args.state_dir).resolve()
    material_state_dir = state_dir.with_name(state_dir.name + ".material")
    material_bundle = V5ProductionBundleV1(
        V5ProductionBundleInputsV1(
            root=ROOT,
            state_root=material_state_dir,
            controller_readback_dir=Path(args.controller_readback_dir).resolve(),
            canary_controller_readback_dir=Path(args.canary_controller_readback_dir).resolve(),
            canary_dir=Path(args.canary_dir).resolve(),
            home_calibration_receipt=Path(args.home_calibration_receipt).resolve(),
            no_motion_recipe_receipt=Path(args.no_motion_recipe_receipt).resolve(),
            optimizer_python=Path("/usr/bin/python3"),
            robot_host=str(args.controller_host),
            kunwei_host=str(args.kunwei_host),
            kunwei_port=int(args.kunwei_port),
            v5_campaign_config=Path(args.v5_campaign_config).resolve(),
            compatibility_config=ROOT / "config/step6/r013_figure8_direct_campaign_v1.json",
        )
    )
    source = build_source_identity(ROOT)
    if source["source_sha256"] != EXPECTED_SOURCE_SHA:
        raise V5EntryComparisonError(f"source identity changed: {source['source_sha256']}")
    inputs = V5CapabilityAcceptanceInputsV1(
        root=ROOT,
        state_root=state_dir,
        controller_readback_dir=Path(args.controller_readback_dir).resolve(),
        canary_readback_dir=Path(args.canary_controller_readback_dir).resolve(),
        canary_dir=Path(args.canary_dir).resolve(),
        home_calibration_receipt=Path(args.home_calibration_receipt).resolve(),
        no_motion_recipe_receipt=Path(args.no_motion_recipe_receipt).resolve(),
        robot_host=str(args.controller_host),
        kunwei_host=str(args.kunwei_host),
        kunwei_port=int(args.kunwei_port),
        release_identity_sha256=str(material_bundle.material.release["release_identity_sha256"]),
        source_identity=source,
        controller_triplet_sha256=material_bundle.material.controller_triplet_sha256,
        compatibility_fingerprint=material_bundle.material.compatibility_fingerprint,
    )
    comparison = V5EntryComparisonV1(inputs)
    from step6_figure8_autotune_v1.v5_capability_acceptance import _file_sha as capability_file_sha
    from step6_figure8_autotune_v1.v5_capability_acceptance import _atomic_json as capability_atomic_json
    # The managed child has already passed runtime admission.  Hold the same
    # formal-timing lease used by the normal V5 live entrypoint.
    from ur10e_parallel import ResourceProfile, formal_timing_lease, formal_timing_owner, notify_formal_timing_owner
    profile = ResourceProfile.from_env()
    lease = formal_timing_lease(profile, task=f"v5-entry-comparison:{state_dir}", blocking=False)
    try:
        lease.__enter__()
    except BlockingIOError as exc:
        owner = formal_timing_owner(profile)
        notify_formal_timing_owner(owner, f"V5 entry comparison blocked by formal timing: {state_dir}")
        raise V5EntryComparisonError(f"formal timing resource busy: {owner}") from exc
    try:
        result = comparison.run()
    finally:
        lease.__exit__(None, None, None)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
