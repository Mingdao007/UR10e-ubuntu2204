#!/usr/bin/env python3
"""Build an offline SFC tangent-force phase template from five sealed A runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from analyze_tase_resident_evidence import reduce_attempt
from step5d_autotune_v4_r004.contracts import HOME_Q_TOLERANCE_RAD
from step5d_paper_outer_loop import rotvec_to_matrix
from tase_sfc_tangent_augmentation import PHASE_TEMPLATE_SCHEMA


PROTOCOL_ID = "figure8_window60_r013_rate460_v1"
BIN_WIDTH_S = 0.1
BIN_COUNT = 600
SOURCE_ATTEMPT_COUNTS = {4, 5}


class TemplateBuildError(RuntimeError):
    """Selected sealed A traces cannot build a consistent phase template."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_vector(value: Any, width: int, name: str) -> np.ndarray:
    vector = np.asarray(value, dtype=float)
    if vector.shape != (width,) or not np.isfinite(vector).all():
        raise TemplateBuildError(f"{name} must be a finite vector of width {width}")
    return vector


def build_template(attempt_dirs: list[Path]) -> dict[str, Any]:
    if len(attempt_dirs) not in SOURCE_ATTEMPT_COUNTS:
        raise TemplateBuildError("four held-out-fold or five frozen A attempts are required")
    resolved = [Path(path).expanduser().resolve(strict=True) for path in attempt_dirs]
    if len(set(resolved)) != len(attempt_dirs):
        raise TemplateBuildError("source attempts must be distinct")

    attempt_tables: list[np.ndarray] = []
    sources: list[dict[str, Any]] = []
    common_parameters: dict[str, Any] | None = None
    common_physical_binding: dict[str, Any] | None = None
    home_config = json.loads(
        (Path(__file__).resolve().parents[1] / "config/figure8_home_v1.json").read_text(
            encoding="utf-8"
        )
    )
    canonical_home_q = _finite_vector(home_config.get("joint_positions_rad"), 6, "canonical Home q")
    for attempt_dir in resolved:
        reduced = reduce_attempt(attempt_dir, verify_seal=True)
        if (
            reduced.get("protocol_id") != PROTOCOL_ID
            or reduced.get("evidence_eligible") is not True
            or reduced.get("path_complete") is not True
            or reduced.get("home_verified") is not True
            or reduced.get("formal_bins") != 550
        ):
            raise TemplateBuildError(f"source is not a complete eligible rate460 A PATH: {attempt_dir}")
        receipt_path = attempt_dir / "attempt-result.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        session_dir = attempt_dir.parent.parent
        dispatch = json.loads((session_dir / "dispatch_receipt.json").read_text(encoding="utf-8"))
        home_receipt = json.loads((session_dir / "home_start_receipt.json").read_text(encoding="utf-8"))
        readback = json.loads((session_dir / "readback-results.json").read_text(encoding="utf-8"))
        actual_home_q = _finite_vector(home_receipt.get("final_q"), 6, "session Home actual_q")
        if (
            home_receipt.get("stationary") is not True
            or home_receipt.get("safety_mode") != "NORMAL"
            or not isinstance(home_receipt.get("eoat_identity_sha256"), str)
            or readback.get("pass") is not True
            or dispatch.get("method") != "TASE_RNN_MATURE"
            or dispatch.get("program") != "step5d_contact_six_qp_v1"
            or dispatch.get("runtime_protocol") != 606006
            or (dispatch.get("live_path") or {}).get("protocol_id") != PROTOCOL_ID
            or not isinstance(dispatch.get("controller_identity"), dict)
            or not isinstance(dispatch.get("controller_readback_triplet"), dict)
        ):
            raise TemplateBuildError(f"source physical binding or Home receipt differs: {attempt_dir}")
        if max(abs(float(actual) - float(target)) for actual, target in zip(
            actual_home_q, canonical_home_q, strict=True,
        )) > HOME_Q_TOLERANCE_RAD:
            raise TemplateBuildError(f"source session Home is outside the canonical joint Home: {attempt_dir}")
        physical_binding = {
            "home_profile_id": home_config.get("home_profile_id"),
            "canonical_home_q": canonical_home_q.tolist(),
            "eoat_identity_sha256": home_receipt["eoat_identity_sha256"],
            "controller_identity": dispatch["controller_identity"],
            "controller_readback_triplet": dispatch["controller_readback_triplet"],
            "readable_runtime_identity": dispatch.get("readable_runtime_identity"),
            "motion_profile": dispatch.get("motion_profile"),
            "protocol_id": (dispatch.get("live_path") or {}).get("protocol_id"),
            "program": dispatch.get("program"),
        }
        if common_physical_binding is None:
            common_physical_binding = physical_binding
        elif physical_binding != common_physical_binding:
            raise TemplateBuildError("source A attempts do not share Home, EOAT, program, or controller identity")
        binding = receipt.get("parameter_binding")
        if not isinstance(binding, dict):
            raise TemplateBuildError(f"source parameter binding is missing: {attempt_dir}")
        candidate_id = binding.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.endswith("-A"):
            raise TemplateBuildError(f"source is not an A arm: {attempt_dir}")
        if binding.get("protocol_id") != PROTOCOL_ID:
            raise TemplateBuildError(f"source parameter protocol differs: {attempt_dir}")
        current_parameters = {
            key: binding.get(key)
            for key in (
                "Md_scalar", "Bd_scalar", "force_integral_limit_n_s",
                "force_integral_policy", "normal_filter_tau_s",
            )
        }
        if common_parameters is None:
            common_parameters = current_parameters
        elif current_parameters != common_parameters:
            raise TemplateBuildError("A source attempts do not share the same TASE parameters")

        timeline = attempt_dir / "command_timeline.jsonl"
        timeline_sha = _sha256_file(timeline)
        sums = np.zeros((BIN_COUNT, 3), dtype=float)
        counts = np.zeros(BIN_COUNT, dtype=np.int64)
        path_rows = 0
        with timeline.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("reference_phase") != "path":
                    continue
                if "actual_TCP_force" in row:
                    raise TemplateBuildError("source trace contains UR internal force; refusing mixed-source template")
                time_s = float(row.get("reference_time_s"))
                if not math.isfinite(time_s) or not 0.0 <= time_s < 60.0:
                    continue
                index = int(math.floor(time_s / BIN_WIDTH_S + 1e-9))
                if not 0 <= index < BIN_COUNT:
                    raise TemplateBuildError(f"phase bin out of range at {timeline}:{line_number}")
                inputs = row.get("sfc_replay_inputs")
                provenance = row.get("sfc_replay_input_provenance")
                if not isinstance(inputs, dict) or not isinstance(provenance, dict):
                    raise TemplateBuildError(f"synchronized KWR75 inputs missing at {timeline}:{line_number}")
                if provenance.get("control_wrench_tcp_n_nm") != "kunwei_baseline_corrected":
                    raise TemplateBuildError(f"force source is not baseline-corrected Kunwei at {timeline}:{line_number}")
                pose = _finite_vector(inputs.get("actual_tcp_pose_m_rad"), 6, "actual_tcp_pose_m_rad")
                wrench = _finite_vector(inputs.get("control_wrench_tcp_n_nm"), 6, "control_wrench_tcp_n_nm")
                normal = _finite_vector(inputs.get("control_reaction_normal_base"), 3, "control_reaction_normal_base")
                normal_norm = float(np.linalg.norm(normal))
                if normal_norm <= 1e-12:
                    raise TemplateBuildError(f"normal is degenerate at {timeline}:{line_number}")
                normal = normal / normal_norm
                tangent_projector = np.eye(3) - np.outer(normal, normal)
                force_base = rotvec_to_matrix(pose[3:]) @ wrench[:3]
                sums[index] += tangent_projector @ force_base
                counts[index] += 1
                path_rows += 1
        if np.any(counts == 0):
            raise TemplateBuildError(
                f"complete diagnostic coverage required; got {int(np.count_nonzero(counts))}/{BIN_COUNT} bins"
            )
        attempt_tables.append(sums / counts[:, None])
        sources.append({
            "attempt_id": attempt_dir.name,
            "candidate_id": candidate_id,
            "source_path": str(attempt_dir),
            "timeline_sha256": timeline_sha,
            "session_home_actual_q": actual_home_q.tolist(),
            "path_rows_0_60": path_rows,
            "formal_bins": 550,
            "diagnostic_bins": 600,
            "receipt_formal_mae_n": reduced.get("formal_bin_mean_mae_n"),
        })

    template = np.mean(np.stack(attempt_tables), axis=0)
    training_scope = (
        "five-attempt-frozen-template"
        if len(sources) == 5 else "four-attempt-leave-one-out-template"
    )
    return {
        "schema": PHASE_TEMPLATE_SCHEMA,
        "template_id": f"tase-r013-rate460-a-{len(sources)}-attempt-phase-tangent-v1",
        "status": "offline learned input template; not live qualified",
        "training_scope": training_scope,
        "protocol_id": PROTOCOL_ID,
        "window_s": [0.0, 60.0],
        "bin_width_s": BIN_WIDTH_S,
        "bin_count": BIN_COUNT,
        "frame": "base",
        "force_source": "baseline-corrected Kunwei KWR75",
        "method": "equal-weight mean of each sealed A attempt's per-100ms tangent-force mean",
        "training_parameters": common_parameters,
        "physical_binding": common_physical_binding,
        "source_attempts": sources,
        "phase_mean_tangent_force_base_n": template.tolist(),
        "claim_limit": (
            "The template models repeatable path-phase-associated KWR75 force. "
            "It does not identify friction, independent external disturbance, or task-force truth."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error(f"refusing to overwrite existing template: {output}")
    document = build_template(args.attempt_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "template_id": document["template_id"],
        "training_attempts": len(document["source_attempts"]),
        "bins": document["bin_count"],
        "force_source": document["force_source"],
        "live_qualified": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
