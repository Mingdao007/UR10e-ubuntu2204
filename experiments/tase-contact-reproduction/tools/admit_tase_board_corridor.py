"""Admit a predeclared board-mark corridor from ten independent normal runs.

This is image evidence preparation only. It neither acquires camera frames nor
starts the robot; a visual failure withholds workpiece-damage conclusions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping

from measure_contact_board_marks import measure_contact_board_marks


INPUT_SCHEMA = "tase.board-corridor-input-v1"
OUTPUT_SCHEMA = "tase.board-corridor-admission-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def admit_corridor(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if manifest.get("schema") != INPUT_SCHEMA:
        raise ValueError("board corridor input schema differs")
    mask = Path(manifest["corridor_mask"])
    if not mask.is_file() or mask.is_symlink():
        raise ValueError("corridor mask must be an existing regular file")
    mask_digest = _sha256(mask)
    pairs = manifest.get("unperturbed_capture_pairs")
    if not isinstance(pairs, list) or len(pairs) < 10:
        raise ValueError("at least ten unperturbed capture pairs required")
    used_paths: set[Path] = set()
    used_run_ids: set[str] = set()
    used_capture_digests: set[tuple[str, str]] = set()
    previous_after_s = -math.inf
    observations = []
    reasons = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, Mapping):
            raise ValueError(f"capture pair {index} must be a mapping")
        run_id = pair.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in used_run_ids:
            raise ValueError("unperturbed run IDs must be unique")
        used_run_ids.add(run_id)
        try:
            before_at_s = float(pair["before_captured_at_s"])
            after_at_s = float(pair["after_captured_at_s"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("capture pair timestamps are required") from exc
        if (not math.isfinite(before_at_s) or not math.isfinite(after_at_s)
                or before_at_s <= previous_after_s or after_at_s <= before_at_s):
            raise ValueError("capture pairs must be in distinct increasing time windows")
        previous_after_s = after_at_s
        paths = [Path(pair[key]) for key in ("before", "after")]
        for path in paths:
            resolved = path.resolve()
            if not path.is_file() or path.is_symlink() or resolved in used_paths:
                raise ValueError("capture images must be independent regular files")
            used_paths.add(resolved)
        measurement = measure_contact_board_marks(
            paths[0], paths[1], mask, pixel_size_mm=manifest.get("pixel_size_mm"),
        )
        digests = (_sha256(paths[0]), _sha256(paths[1]))
        if digests in used_capture_digests:
            raise ValueError("capture pairs repeat identical image content")
        used_capture_digests.add(digests)
        row = {"index": index, "run_id": run_id,
               "before_captured_at_s": before_at_s, "after_captured_at_s": after_at_s,
               "before_sha256": digests[0], "after_sha256": digests[1],
               "measurement": measurement}
        observations.append(row)
        if measurement.get("qualified") is not True:
            reasons.append(f"pair_{index}:image_unqualified")
        elif measurement.get("corridor_mask_sha256") != mask_digest:
            reasons.append(f"pair_{index}:corridor_mask_identity_differs")
        elif measurement.get("outside_corridor_area_px2") != 0:
            reasons.append(f"pair_{index}:normal_trace_outside_declared_corridor")
    qualified = not reasons
    registration_errors = [row["measurement"].get("registration_error_px")
                           for row in observations
                           if row["measurement"].get("registration_error_px") is not None]
    return {
        "schema": OUTPUT_SCHEMA,
        "unperturbed_runs": len(observations),
        "detectable": qualified,
        "frozen_before_candidate": qualified,
        "mask_sha256": mask_digest,
        "admitted_at_s": time.time(),
        "max_registration_error_px": max(registration_errors) if registration_errors else None,
        "failure_reasons": reasons,
        "observations": observations,
        "claim_scope": "visible new marks outside the nominal 5 N trace only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = admit_corridor(json.loads(args.manifest.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(result, target, indent=2, sort_keys=True, allow_nan=False)
        target.write("\n")
    return 0 if result["detectable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
