#!/usr/bin/env python3
"""Single canonical evaluator for Step5d v30 formal timing evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from step5d_v30_timing import SOURCE_BINDING_FILES, summarize_preaggregated


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_timing_raw(root: Path, raw_path: Path) -> dict[str, Any]:
    """Evaluate one raw capture against current canonical source bindings."""

    root = root.resolve()
    raw_path = raw_path.resolve()
    raw = _load(raw_path)
    remote = _load(root / "config/step5d_v29_remote_evidence_sha256.json")
    evaluation = summarize_preaggregated(
        raw,
        expected_source_binding={
            field: _sha256(root / relative)
            for field, relative in SOURCE_BINDING_FILES.items()
        },
        expected_replay_sha256=remote["sha256"]["bridge_rtde_500hz.csv"],
        expected_paper_truth_sha256=_sha256(
            root / "config/step5d_liveprep_solver_gate.json"
        ),
    )
    accepted = evaluation.get("acceptance_eligible") is True
    return {
        "schema_version": "step5d_timing_acceptance_evaluation_v1",
        "evaluator": "tools/step5d_timing_acceptance.py:evaluate_timing_raw",
        "raw_path": str(raw_path.relative_to(root)),
        "raw_sha256": _sha256(raw_path),
        "input_claim_class": "formal_raw_capture",
        "output_claim_class": "formal_acceptance" if accepted else "diagnostic_only",
        "accepted": accepted,
        "classification": evaluation.get("classification"),
        "blockers": evaluation.get("blockers", []),
        "evaluation": evaluation,
    }
