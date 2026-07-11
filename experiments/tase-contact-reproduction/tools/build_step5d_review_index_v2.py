#!/usr/bin/env python3
"""Build the immutable-history-aware UR10e Review v2 index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from step5d_review_v2 import (
    SCHEMA_INDEX,
    SCHEMA_MANIFEST,
    file_sha256,
    load_json,
    relative_path,
    review_findings,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "step5d_review_index_v2.json"
HISTORICAL_ARTIFACTS = (
    {
        "id": "v30_milestone_reviews_v1",
        "path": "config/step5d_v30_milestone_reviews.json",
        "workflow": "v30",
        "next_review_class": "v30_contact_pre_live",
    },
    {
        "id": "v29_imported_evidence_manifest_v1",
        "path": "config/step5d_v29_imported_evidence_manifest.json",
        "workflow": "v29",
        "next_review_class": "v29_baseline_re_review",
    },
    {
        "id": "v29_remote_evidence_sha256_v1",
        "path": "config/step5d_v29_remote_evidence_sha256.json",
        "workflow": "v29",
        "next_review_class": "v29_baseline_re_review",
    },
)


def _historical_entry(root: Path, descriptor: dict[str, str]) -> dict[str, Any]:
    path, relative = relative_path(root, descriptor["path"])
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = load_json(path)
    return {
        "id": descriptor["id"],
        "path": relative,
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "original_schema_version": payload.get("schema_version"),
        "workflow": descriptor["workflow"],
        "status": "historical_superseded_by_review_policy_v2",
        "immutable": True,
        "counts_as_review_v2": False,
        "next_review_class": descriptor["next_review_class"],
    }


def _review_entry(root: Path, value: str | Path) -> dict[str, Any]:
    path, relative = relative_path(root, value)
    payload = load_json(path)
    if payload.get("schema_version") != SCHEMA_MANIFEST:
        raise ValueError(f"not a Review v2 manifest: {relative}")
    severities = {"P0": 0, "P1": 0, "P2": 0}
    for finding in review_findings(payload):
        severity = finding.get("severity")
        if severity in severities:
            severities[str(severity)] += 1
    return {
        "path": relative,
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "review_mode": payload.get("review_mode"),
        "workflow": payload.get("workflow"),
        "milestone": payload.get("milestone"),
        "required_stack": payload.get("required_stack"),
        "composite_fingerprint": payload.get("composite_fingerprint"),
        "finding_counts": severities,
        "invalidation_reason": payload.get("invalidation_reason"),
    }


def build(
    *, root: Path = ROOT, review_manifests: Iterable[str | Path] = ()
) -> dict[str, Any]:
    historical = [_historical_entry(root, item) for item in HISTORICAL_ARTIFACTS]
    reviews = sorted(
        (_review_entry(root, value) for value in review_manifests),
        key=lambda item: item["path"],
    )
    full_by_fingerprint: dict[str, list[str]] = {}
    for review in reviews:
        if review["review_mode"] == "full":
            full_by_fingerprint.setdefault(
                str(review["composite_fingerprint"]), []
            ).append(str(review["path"]))
    duplicates = {
        fingerprint: paths
        for fingerprint, paths in sorted(full_by_fingerprint.items())
        if len(paths) > 1
    }
    return {
        "schema_version": SCHEMA_INDEX,
        "policy_id": "ur10e_review_policy_v2",
        "historical_artifacts": historical,
        "v2_reviews": reviews,
        "full_review_count_by_composite_fingerprint": {
            fingerprint: len(paths)
            for fingerprint, paths in sorted(full_by_fingerprint.items())
        },
        "duplicate_full_review_fingerprints": duplicates,
        "blockers": (
            ["duplicate_full_review_for_composite_fingerprint"] if duplicates else []
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = build(root=args.experiment_root, review_manifests=args.manifest)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"review index drift: {args.output}")
            return 1
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if payload["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
