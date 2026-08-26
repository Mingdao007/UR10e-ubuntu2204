"""Cold, self-contained evidence report for the Autotuner V5 bundle."""

from __future__ import annotations

from collections import Counter
import hashlib
import html
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterable, Mapping, Sequence

from .v5_campaign import CampaignReportV2, CampaignRoleV2
from .v5_lifecycle_ledger import (
    BoundaryMode,
    FigureEightPhysicalRecordV2,
    FORMAL_BIN_COUNT,
    LedgerRole,
    PATH_END_S,
    TAIL_END_S,
    V5PhysicalAdmissionLedgerV2,
    canonical_sha256,
)


V5_EVIDENCE_REPORT_SCHEMA = "step6.autotune/autotuner-v5-evidence-report-v1"
V5_EVIDENCE_REPORT_VERSION = 1
V5_REPORT_BUILD_SCHEMA = "step6.autotune/autotuner-v5-report-build-receipt-v1"
_GAP_FIELDS = (
    "artifact_sample_index_gap_count",
    "packet_sequence_gap_count",
    "packet_sequence_regression_count",
    "dropped_chunks",
    "write_errors",
    "invalid_rows",
    "observer_errors",
    "missing_force_rows",
    "missing_pose_rows",
)


class V5EvidenceReportError(RuntimeError):
    """The final V5 report could not be cold-derived from sealed evidence."""


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
        raise V5EvidenceReportError("V5 report value is not canonical JSON") from exc


def _file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require_sha(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V5EvidenceReportError(f"{role} SHA-256 is invalid")
    return value


def _atomic_bytes(path: Path, value: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.name + f".tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, _canonical(dict(value)) + b"\n")


def _finite_values(values: Iterable[Any], role: str) -> tuple[float, ...]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise V5EvidenceReportError(f"{role} contains a non-numeric value") from exc
        if not math.isfinite(number):
            raise V5EvidenceReportError(f"{role} contains a non-finite value")
        result.append(number)
    return tuple(result)


def _signal_summary(values: Iterable[Any], role: str) -> dict[str, Any]:
    numbers = _finite_values(values, role)
    if not numbers:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "rms": None,
        }
    return {
        "count": len(numbers),
        "min": min(numbers),
        "max": max(numbers),
        "mean": math.fsum(numbers) / len(numbers),
        "rms": math.sqrt(math.fsum(value * value for value in numbers) / len(numbers)),
    }


def _state_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                f"tp={int(row.get('tp_state', -1))}/phase={int(row.get('phase_code', -1))}"
                for row in rows
            ).items()
        )
    )


def build_v5_trial_evidence(record: FigureEightPhysicalRecordV2) -> dict[str, Any]:
    """Cold-derive one bounded trial summary without interpolation."""

    if not isinstance(record, FigureEightPhysicalRecordV2):
        raise TypeError("V5 report trial evidence requires a physical record")
    artifact = record.artifact_binding.cold_index()
    if (
        artifact.artifact_sha256 != record.source_artifact.artifact_sha256
        or artifact.artifact_size != record.source_artifact.artifact_size
    ):
        raise V5EvidenceReportError("trial artifact differs from the cold binding")
    start = record.trial_slice.sample_start_index
    end = record.trial_slice.sample_end_index
    rows = tuple(artifact.rows[start:end])
    if not rows:
        raise V5EvidenceReportError("trial artifact slice is empty")
    path_start = record.trial_slice.clock_start_s
    tail_rows = tuple(
        row
        for row in rows
        if PATH_END_S
        <= float(row["monotonic_s"]) - path_start
        <= TAIL_END_S + 1e-9
    )
    metric = record.metric_snapshot
    evidence_bins = metric.evidence_bins
    formal_bins = metric.formal_bins
    if len(evidence_bins) != 600 or len(formal_bins) != FORMAL_BIN_COUNT:
        raise V5EvidenceReportError("trial metric bin cardinality differs")
    receipt = artifact.receipt
    gaps = {name: int(receipt.get(name, -1)) for name in _GAP_FIELDS}
    if any(value != 0 for value in gaps.values()):
        raise V5EvidenceReportError("admitted V5 artifact contains a reported gap/error")
    gate_evidence = record.closure.evidence
    boundary = record.boundary.boundary
    return {
        "trial_id": record.trial_id,
        "attempt_id": record.attempt_id,
        "chain_id": record.chain_id,
        "record_sha256": record.record_sha256,
        "candidate_identity": record.candidate_identity.as_dict(),
        "eligible": record.eligible,
        "admission_reasons": list(record.admission_reasons),
        "censored": record.censored,
        "boundary": {
            "mode": record.boundary.mode.value,
            "receipt_sha256": record.boundary.boundary_sha256,
            "tail_endpoint_s": (
                boundary.tail_endpoint_s
                if record.boundary.mode is BoundaryMode.CONTACT_ROLLOVER
                else TAIL_END_S
            ),
            "terminal_home": record.boundary.mode is BoundaryMode.HOME,
        },
        "source_artifact": {
            "path": artifact.artifact_path,
            "sha256": artifact.artifact_sha256,
            "size": artifact.artifact_size,
            "row_count": len(artifact.rows),
            "slice_start_index": start,
            "slice_end_index": end,
            "slice_row_count": len(rows),
            "coverage_complete": receipt.get("coverage_complete") is True,
            "home_verified": receipt.get("home_verified") is True,
            "capture_started_before_motion": receipt.get("capture_started_before_motion") is True,
            "terminal_sensor_present": receipt.get("terminal_sensor_present") is True,
            "gaps_and_errors": gaps,
            "interpolation_used": False,
            "host_monotonic_start_s": float(rows[0]["monotonic_s"]),
            "host_monotonic_end_s": float(rows[-1]["monotonic_s"]),
            "rtde_start_s": float(rows[0]["rtde_timestamp_s"]),
            "rtde_end_s": float(rows[-1]["rtde_timestamp_s"]),
            "state_counts": _state_counts(rows),
            "raw_signed_normal_n": _signal_summary(
                (row["normal_load_n"] for row in rows), "raw signed normal"
            ),
            "raw_force_norm_n": _signal_summary(
                (row["force_norm_n"] for row in rows), "raw force norm"
            ),
            "raw_torque_norm_nm": _signal_summary(
                (row["torque_norm_nm"] for row in rows), "raw torque norm"
            ),
            "deployed_filtered_normal_n": _signal_summary(
                (row["filtered_normal_n"] for row in rows),
                "deployed filtered normal",
            ),
        },
        "evidence_0_60": {
            "start_s": 0.0,
            "end_s": 60.0,
            "bin_width_s": 0.1,
            "bin_count": len(evidence_bins),
            "sealed": metric.closure.sealed,
            "filtered_normal_n": _signal_summary(
                (item.filtered_normal_n for item in evidence_bins),
                "evidence filtered bins",
            ),
            "raw_signed_normal_n": _signal_summary(
                (item.raw_signed_normal_n for item in evidence_bins),
                "evidence raw bins",
            ),
        },
        "formal_5_60": {
            "start_s": 5.0,
            "end_s": 60.0,
            "bin_width_s": 0.1,
            "bin_count": len(formal_bins),
            "signal_name": metric.signal_name,
            "formal_mae_n": metric.formal_mae_n,
            "metric_rows_sha256": metric.metric_rows_sha256,
            "snapshot_sha256": metric.content_hash,
            "sealed": metric.sealed and metric.closure.sealed,
            "raw_measured_force_is_metric_input": False,
        },
        "primary_0_60": {
            "start_s": 0.0,
            "end_s": 60.0,
            "bin_width_s": 0.1,
            "bin_count": len(evidence_bins),
            "signal_name": metric.signal_name,
            "primary_mae_n": metric.primary_mae_n,
            "metric_rows_sha256": metric.metric_rows_sha256,
            "snapshot_sha256": metric.content_hash,
            "sealed": metric.sealed and metric.closure.sealed,
            "raw_measured_force_is_metric_input": False,
        },
        "ext60_tail": {
            "start_s": 60.0,
            "end_s": TAIL_END_S,
            "row_count": len(tail_rows),
            "state_counts": _state_counts(tail_rows),
            "raw_signed_normal_n": _signal_summary(
                (row["normal_load_n"] for row in tail_rows), "tail raw normal"
            ),
            "deployed_filtered_normal_n": _signal_summary(
                (row["filtered_normal_n"] for row in tail_rows),
                "tail filtered normal",
            ),
            "gate_observed_count": int(
                gate_evidence.tail_summary["observed_count"]
            ) if gate_evidence is not None else None,
            "gate_failure_counts": (
                dict(gate_evidence.tail_summary["failure_counts"])
                if gate_evidence is not None
                else None
            ),
            "included_in_mae": False,
            "included_in_censor": False,
            "included_in_gp_or_budget": False,
            "interpolation_used": False,
        },
        "narrow_force_window_telemetry": {
            "values": dict(record.optional_telemetry),
            "blocking": False,
            "admission_authority": False,
        },
    }


def _role_evidence(
    *,
    role: CampaignRoleV2,
    report: CampaignReportV2,
    ledger: V5PhysicalAdmissionLedgerV2,
) -> dict[str, Any]:
    expected_ledger_role = (
        LedgerRole.PRIMARY if role is CampaignRoleV2.PRIMARY else LedgerRole.CORRECTION
    )
    if (
        report.role is not role
        or ledger.role is not expected_ledger_role
        or report.campaign_fingerprint != ledger.campaign_fingerprint
    ):
        raise V5EvidenceReportError("role report and physical ledger differ")
    ledger.fresh_process_verify()
    trials = [build_v5_trial_evidence(record) for record in ledger.records]
    return {
        "role": role.value,
        "campaign_fingerprint": report.campaign_fingerprint,
        "physical_ledger_path": str(ledger.path.resolve()),
        "physical_ledger_head_sha256": ledger.head_sha256,
        "campaign_closeout": report.as_dict(),
        "record_count": len(trials),
        "eligible_count": sum(bool(row["eligible"]) for row in trials),
        "home_boundary_count": sum(
            row["boundary"]["mode"] == BoundaryMode.HOME.value for row in trials
        ),
        "rollover_boundary_count": sum(
            row["boundary"]["mode"] == BoundaryMode.CONTACT_ROLLOVER.value
            for row in trials
        ),
        "trials": trials,
    }


def _require_release(value: Mapping[str, Any], role: str) -> dict[str, Any]:
    release = dict(value)
    if (
        release.get("home_verified") is not True
        or release.get("stopped") is not True
        or release.get("writer_released") is not True
    ):
        raise V5EvidenceReportError(f"{role} writer release is not Home/STOPPED")
    return release


def _selection_summary(
    primary: CampaignReportV2,
    correction: CampaignReportV2,
) -> dict[str, Any]:
    repeated = next(
        (
            dict(row)
            for row in primary.top_candidates
            if row["candidate_key"] == primary.winner_candidate_key
        ),
        None,
    )
    if repeated is None or repeated.get("total_n") != 5:
        raise V5EvidenceReportError("PRIMARY physically repeated incumbent is absent")
    return {
        "primary_single_minimum_basis": {
            "exact_novel_count": primary.exact_novel_count,
            "ranking_basis": "single sealed primary_0_60_mae_n over exact novel trials",
            "top3_original": [
                {
                    "candidate_key": row["candidate_key"],
                    "original_mae_n": row["original_mae_n"],
                }
                for row in primary.top_candidates
            ],
        },
        "physically_repeated_incumbent": {
            "candidate_key": primary.winner_candidate_key,
            "candidate": dict(primary.winner_candidate or {}),
            **repeated,
            "selection_basis": "lowest physically eligible n=5 repeated mean among original top3",
        },
        "correction_single_minimum": {
            "exact_novel_count": correction.exact_novel_count,
            "candidate_key": correction.corrected_best_candidate_key,
            "candidate": dict(correction.corrected_best_candidate or {}),
        },
        "matched_zero_vs_corrected": {
            "order": ["Z", "C", "C", "Z", "Z", "C", "C", "Z", "Z", "C"],
            "zero_maes_n": list(correction.matched_zero_maes_n),
            "corrected_maes_n": list(correction.matched_corrected_maes_n),
            "paired_differences_corrected_minus_zero_n": list(
                correction.paired_differences
            ),
            "paired_mean_difference_n": correction.paired_mean_difference_n,
            "paired_ci90_n": list(correction.paired_ci90_n or ()),
            "paired_df": correction.paired_df,
            "decision": correction.decision,
            "automatic_promotion": False,
        },
    }


def build_v5_evidence_report(
    *,
    output_root: Path | str,
    release_identity: Mapping[str, Any],
    primary_report: CampaignReportV2,
    correction_report: CampaignReportV2,
    primary_physical: V5PhysicalAdmissionLedgerV2,
    correction_physical: V5PhysicalAdmissionLedgerV2,
    primary_writer_release: Mapping[str, Any],
    correction_writer_release: Mapping[str, Any],
    capability_acceptance: Mapping[str, Any],
    sidecars: Mapping[str, Any],
) -> dict[str, Any]:
    """Build and cold-verify authoritative JSON plus dependency-free HTML."""

    release = dict(release_identity)
    release_sha = _require_sha(
        release.get("release_identity_sha256"), "V5 release identity"
    )
    for ledger in (primary_physical, correction_physical):
        if ledger.release_identity_sha256 != release_sha:
            raise V5EvidenceReportError("report physical ledger crosses release identity")
    primary_release = _require_release(primary_writer_release, "PRIMARY")
    correction_release = _require_release(correction_writer_release, "CORRECTION")
    capability = dict(capability_acceptance)
    capability_unsigned = {
        key: value for key, value in capability.items() if key != "receipt_sha256"
    }
    if (
        capability.get("schema")
        != "step6.autotune/autotuner-v5-capability-acceptance-v1"
        or capability.get("status") != "PASSED"
        or capability.get("release_identity_sha256") != release_sha
        or capability.get("receipt_sha256") != canonical_sha256(capability_unsigned)
        or capability.get("verified_home") is not True
        or capability.get("stopped") is not True
        or capability.get("writer_released") is not True
        or capability.get("campaign_qualification") is not False
        or capability.get("narrow_force_windows_blocking") is not False
    ):
        raise V5EvidenceReportError(
            "one-shot capability acceptance is absent or crosses release identity"
        )
    primary = _role_evidence(
        role=CampaignRoleV2.PRIMARY,
        report=primary_report,
        ledger=primary_physical,
    )
    correction = _role_evidence(
        role=CampaignRoleV2.CORRECTION,
        report=correction_report,
        ledger=correction_physical,
    )
    body = {
        "schema": V5_EVIDENCE_REPORT_SCHEMA,
        "version": V5_EVIDENCE_REPORT_VERSION,
        "release_identity": release,
        "scope": {
            "v5_only": True,
            "v4_layout_606_mutated": False,
            "campaign_or_epoch_qualification": False,
            "metric_signal": "filtered_normal_n",
            "raw_force_authority": ["safety", "evidence"],
            "formal_window_s": [5.0, 60.0],
            "evidence_window_s": [0.0, 60.0],
            "primary_metric_window_s": [0.0, 60.0],
            "secondary_metric_window_s": [5.0, 60.0],
            "ext60_tail_s": [60.0, TAIL_END_S],
            "tail_excluded_from_mae_censor_gp_budget": True,
            "narrow_force_windows_blocking": False,
            "interpolation_or_gap_filling": False,
            "automatic_promotion": False,
        },
        "primary": primary,
        "correction": correction,
        "selection": _selection_summary(primary_report, correction_report),
        "capability_acceptance": capability,
        "sidecars": dict(sidecars),
        "writer_release": {
            "primary": primary_release,
            "correction": correction_release,
            "final_home": True,
            "final_stopped": True,
            "writer_released": True,
        },
    }
    evidence = {**body, "report_sha256": canonical_sha256(body)}
    destination = Path(output_root).resolve()
    evidence_path = destination / "evidence.json"
    html_path = destination / "report.html"
    receipt_path = destination / "build_receipt.json"
    _atomic_json(evidence_path, evidence)
    _atomic_bytes(html_path, _render_html(evidence).encode("utf-8"))
    receipt_body = {
        "schema": V5_REPORT_BUILD_SCHEMA,
        "version": 1,
        "status": "COMPLETE",
        "release_identity_sha256": release_sha,
        "evidence_path": str(evidence_path),
        "evidence_sha256": _file_sha(evidence_path),
        "html_path": str(html_path),
        "html_sha256": _file_sha(html_path),
        "report_sha256": evidence["report_sha256"],
        "final_home": True,
        "final_stopped": True,
        "writer_released": True,
        "automatic_promotion": False,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": canonical_sha256(receipt_body),
    }
    _atomic_json(receipt_path, receipt)
    cold_verify_v5_evidence_report(receipt_path)
    return receipt


def cold_verify_v5_evidence_report(receipt_path: Path | str) -> Mapping[str, Any]:
    receipt_source = Path(receipt_path).resolve()
    try:
        receipt = json.loads(receipt_source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V5EvidenceReportError("V5 report build receipt is unreadable") from exc
    if not isinstance(receipt, dict):
        raise V5EvidenceReportError("V5 report build receipt is not an object")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        receipt.get("schema") != V5_REPORT_BUILD_SCHEMA
        or receipt.get("version") != 1
        or receipt.get("status") != "COMPLETE"
        or receipt.get("receipt_sha256") != canonical_sha256(unsigned)
        or any(
            receipt.get(name) is not True
            for name in ("final_home", "final_stopped", "writer_released")
        )
        or receipt.get("automatic_promotion") is not False
    ):
        raise V5EvidenceReportError("V5 report build receipt state/hash differs")
    evidence_path = Path(str(receipt.get("evidence_path"))).resolve()
    html_path = Path(str(receipt.get("html_path"))).resolve()
    if (
        evidence_path.is_symlink()
        or html_path.is_symlink()
        or not evidence_path.is_file()
        or not html_path.is_file()
        or _file_sha(evidence_path) != receipt.get("evidence_sha256")
        or _file_sha(html_path) != receipt.get("html_sha256")
    ):
        raise V5EvidenceReportError("V5 report output bytes differ")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V5EvidenceReportError("V5 report evidence is unreadable") from exc
    if not isinstance(evidence, dict):
        raise V5EvidenceReportError("V5 report evidence is not an object")
    report_hash = evidence.pop("report_sha256", None)
    if (
        evidence.get("schema") != V5_EVIDENCE_REPORT_SCHEMA
        or evidence.get("version") != V5_EVIDENCE_REPORT_VERSION
        or report_hash != canonical_sha256(evidence)
        or report_hash != receipt.get("report_sha256")
        or evidence.get("scope", {}).get("interpolation_or_gap_filling") is not False
        or evidence.get("scope", {}).get("automatic_promotion") is not False
        or evidence.get("capability_acceptance", {}).get("status") != "PASSED"
        or evidence.get("writer_release", {}).get("final_home") is not True
        or evidence.get("writer_release", {}).get("final_stopped") is not True
    ):
        raise V5EvidenceReportError("V5 report evidence content/hash differs")
    evidence["report_sha256"] = report_hash
    return evidence


def _render_html(evidence: Mapping[str, Any]) -> str:
    selection = evidence["selection"]
    repeated = selection["physically_repeated_incumbent"]
    paired = selection["matched_zero_vs_corrected"]
    role_rows: list[str] = []
    for role_name in ("primary", "correction"):
        role = evidence[role_name]
        for trial in role["trials"]:
            role_rows.append(
                "<tr>"
                f"<td>{html.escape(role_name.upper())}</td>"
                f"<td>{html.escape(str(trial['trial_id']))}</td>"
                f"<td>{html.escape(str(trial['boundary']['mode']))}</td>"
                f"<td>{trial['primary_0_60']['primary_mae_n']:.6f}</td>"
                f"<td>{trial['formal_5_60']['bin_count']}</td>"
                f"<td>{trial['evidence_0_60']['bin_count']}</td>"
                f"<td>{trial['ext60_tail']['row_count']}</td>"
                f"<td>{html.escape(str(trial['source_artifact']['sha256']))}</td>"
                "</tr>"
            )
    ci = paired["paired_ci90_n"]
    sidecars = html.escape(
        json.dumps(evidence["sidecars"], sort_keys=True, ensure_ascii=False)
    )
    capability = evidence["capability_acceptance"]
    capability_contact = capability["contact_capabilities"]
    entry_ci = capability_contact["home_vs_rollover_five_paired_entries"]["ci90_n"]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autotuner V5 Evidence Report</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:2rem;line-height:1.5;color:#18202a;background:#f7f8fa}}
main{{max-width:1500px;margin:auto;background:white;padding:2rem;border-radius:12px;box-shadow:0 2px 14px #0001}}
table{{border-collapse:collapse;width:100%;font-size:.84rem}}th,td{{border:1px solid #d9dee5;padding:.4rem;text-align:left;vertical-align:top}}
th{{background:#eef2f6}}code,pre{{background:#f1f3f5;padding:.2rem .35rem;border-radius:4px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
.ok{{color:#087f5b;font-weight:700}}.note{{color:#495057}}
</style></head><body><main>
<h1>Autotuner V5 evidence report</h1>
<p class="ok">V5 only · final Home/STOPPED · writer released · no automatic promotion</p>
<p>Release <code>{html.escape(str(evidence['release_identity']['release_identity_sha256']))}</code></p>
<h2>Selection</h2>
<p>Physically repeated incumbent: <code>{html.escape(str(repeated['candidate_key']))}</code>, n={repeated['total_n']}, repeated mean={repeated['repeated_mean_n']:.6f} N.</p>
<p>Correction paired mean (corrected − zero): {paired['paired_mean_difference_n']:.6f} N; 90% CI [{ci[0]:.6f}, {ci[1]:.6f}] N; decision={html.escape(str(paired['decision']))}.</p>
<h2>One-shot capability acceptance</h2>
<p class="ok">Recipe/no-contact/same-candidate/bounded-neighbor/four-rollover acceptance passed; terminal Home/STOPPED verified.</p>
<p>Home-entry versus rollover-entry formal-MAE 90% CI: [{entry_ci[0]:.6f}, {entry_ci[1]:.6f}] N within ±0.05 N. Capability trials called tell_exact 0 times and consumed campaign budget 0.</p>
<h2>Evidence contract</h2>
<p class="note">Primary metric is sealed filtered_normal_n over [0,60) with 600 bins; [5,60) with 550 bins remains the secondary comparability metric. ext60 [60,20π) is tail evidence only and is excluded from MAE, censor, GP, and budget. Raw force is retained for safety/evidence. No interpolation or gap filling is used. Narrow force windows are telemetry only.</p>
<h2>Physical trials</h2>
<table><thead><tr><th>Role</th><th>Trial</th><th>Boundary</th><th>Primary MAE N</th><th>Secondary bins</th><th>Primary bins</th><th>Tail rows</th><th>Artifact SHA-256</th></tr></thead>
<tbody>{''.join(role_rows)}</tbody></table>
<h2>Sidecars</h2><pre>{sidecars}</pre>
<h2>Cold identity</h2><p>Report content SHA-256: <code>{html.escape(str(evidence['report_sha256']))}</code></p>
</main></body></html>"""


__all__ = [
    "V5_EVIDENCE_REPORT_SCHEMA",
    "V5EvidenceReportError",
    "build_v5_evidence_report",
    "build_v5_trial_evidence",
    "cold_verify_v5_evidence_report",
]
