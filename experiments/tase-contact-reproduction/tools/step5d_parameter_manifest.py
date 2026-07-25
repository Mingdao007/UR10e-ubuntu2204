#!/usr/bin/env python3
"""Validate and seed the approved ten-row Step5d parameter manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v3.runtime_profile import (
    DEFAULT_OVERLAY,
    load_launch_profile,
    normalize_trial_overlay,
)
from step5d_parameter_queue import (
    ParameterQueueError,
    initialize,
    list_requests,
    load_state,
    submit_manifest,
)


SCHEMA = "step5d.parameter-receiver/initial-manifest-v1"
BASELINE = (0.001, 0.00001, 7.0)
ORIENTATION_KO = 0.4
QUARTER_OCTAVE = 0.25
EXCLUDED_PATH_TOKENS = (
    "gazebo",
    "ursim",
    "simulation",
    "qualification",
    "offline",
    "dry_run",
)


class ParameterManifestError(RuntimeError):
    pass


def _load(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ParameterManifestError(f"{role} must be a real regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParameterManifestError(f"{role} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParameterManifestError(f"{role} must be a JSON object")
    return payload


def _candidate_uid(
    row: Mapping[str, Any],
    *,
    launch_profile_path: Path,
) -> str:
    profile_payload = _load(launch_profile_path, "V3 launch profile")
    program = profile_payload.get("tp_program_id")
    if not isinstance(program, str) or not program:
        raise ParameterManifestError("V3 launch profile lacks tp_program_id")
    profile = load_launch_profile(
        launch_profile_path,
        expected_tp_program_id=program,
    )
    overlay_input = {
        **DEFAULT_OVERLAY,
        "force_p_gain": float(row["force_p_gain"]),
        "force_i_gain": float(row["force_i_gain"]),
        "force_damping": float(row["force_damping"]),
        "orientation_ko": float(row.get("orientation_ko", ORIENTATION_KO)),
    }
    overlay_input.pop("control_candidate_uid", None)
    return str(normalize_trial_overlay(overlay_input, profile=profile)["control_candidate_uid"])


def _extract_overlay(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidates = (
        payload.get("trial_overlay"),
        payload.get("overlay"),
        payload.get("candidate"),
        payload.get("parameters"),
    )
    for row in candidates:
        if isinstance(row, Mapping) and {
            "force_p_gain",
            "force_i_gain",
            "force_damping",
        }.issubset(row):
            return row
    for value in payload.values():
        if isinstance(value, Mapping):
            found = _extract_overlay(value)
            if found is not None:
                return found
    return None


def import_physical_attempt_uids(
    experiment_root: Path,
    *,
    launch_profile_path: Path,
) -> frozenset[str]:
    """Import authoritative legacy rows and captures that physically ran."""

    result: set[str] = set()
    ledger = experiment_root / "config/step5/step5d_autotune_v3_attempt_ledger.json"
    if ledger.is_file() and not ledger.is_symlink():
        payload = _load(ledger, "frozen physical attempt ledger")
        for entry in payload.get("entries", ()):
            if not isinstance(entry, Mapping):
                continue
            parameters = entry.get("parameters")
            if isinstance(parameters, Mapping):
                result.add(
                    _candidate_uid(parameters, launch_profile_path=launch_profile_path)
                )

    runs = experiment_root / "runs"
    if not runs.is_dir() or runs.is_symlink():
        return frozenset(result)
    evidence_paths = list(runs.glob("**/trial_briefs/*.trial-brief.json"))
    evidence_paths.extend(runs.glob("**/autotune_trials/*/metadata.json"))
    for path in sorted(set(evidence_paths)):
        relative = path.relative_to(runs).as_posix().lower()
        if any(token in relative for token in EXCLUDED_PATH_TOKENS):
            continue
        if path.name == "metadata.json" and not (path.parent / "capture.csv").is_file():
            continue
        try:
            payload = _load(path, "physical attempt evidence")
            overlay = _extract_overlay(payload)
            if overlay is not None:
                result.add(
                    _candidate_uid(overlay, launch_profile_path=launch_profile_path)
                )
        except (KeyError, TypeError, ValueError, ParameterManifestError):
            continue
    return frozenset(result)


def validate_manifest(
    path: Path,
    *,
    launch_profile_path: Path,
    attempted_control_uids: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], ...]:
    payload = _load(path, "initial parameter manifest")
    if set(payload) != {"schema", "parameters"} or payload["schema"] != SCHEMA:
        raise ParameterManifestError("initial parameter manifest fields differ")
    rows = payload["parameters"]
    if not isinstance(rows, list) or len(rows) != 10:
        raise ParameterManifestError("initial parameter manifest must contain exactly 10 rows")
    normalized = []
    previous = BASELINE
    seen: set[str] = set()
    for index, value in enumerate(rows, start=1):
        if not isinstance(value, Mapping) or set(value) != {
            "force_p_gain",
            "force_i_gain",
            "force_damping",
        }:
            raise ParameterManifestError(f"parameter row {index} fields differ")
        current = (
            float(value["force_p_gain"]),
            float(value["force_i_gain"]),
            float(value["force_damping"]),
        )
        if not all(math.isfinite(item) and item > 0 for item in current):
            raise ParameterManifestError(f"parameter row {index} is not finite and positive")
        if not math.isclose(current[1], BASELINE[1], rel_tol=0.0, abs_tol=1e-15):
            raise ParameterManifestError(f"parameter row {index} changes fixed I")
        changed = [
            offset
            for offset, (left, right) in enumerate(zip(previous, current))
            if not math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-15)
        ]
        if len(changed) != 1 or changed[0] not in {0, 2}:
            raise ParameterManifestError(
                f"parameter row {index} must change exactly one P/D coordinate"
            )
        ratio = current[changed[0]] / previous[changed[0]]
        if not math.isclose(abs(math.log2(ratio)), QUARTER_OCTAVE, abs_tol=1e-12):
            raise ParameterManifestError(
                f"parameter row {index} is not a quarter-octave step"
            )
        row = {
            **dict(value),
            "orientation_ko": ORIENTATION_KO,
            "source": f"approved_initial_10:P{index:02d}",
            "position": "tail",
            "occurrence_nonce": hashlib.sha256(
                f"step5d-approved-initial-10\0{index}\0{current!r}".encode("ascii")
            ).hexdigest()[:32],
        }
        uid = _candidate_uid(row, launch_profile_path=launch_profile_path)
        if uid in seen:
            raise ParameterManifestError(f"parameter row {index} repeats a candidate")
        if uid in attempted_control_uids:
            raise ParameterManifestError(
                f"parameter row {index} already has a physical attempt: {uid}"
            )
        seen.add(uid)
        normalized.append(row)
        previous = current
    return tuple(normalized)


def seed_initial_manifest(
    queue_root: Path,
    *,
    campaign_id: str,
    release_manifest_sha256: str,
    launch_profile_path: Path,
    manifest_path: Path,
    experiment_root: Path,
) -> tuple[dict[str, Any], ...]:
    initialize(
        queue_root,
        campaign_id=campaign_id,
        release_manifest_sha256=release_manifest_sha256,
        launch_profile_path=launch_profile_path,
    )
    state = load_state(queue_root)
    if state["revision"] == 0:
        attempted = import_physical_attempt_uids(
            experiment_root,
            launch_profile_path=launch_profile_path,
        )
        rows = validate_manifest(
            manifest_path,
            launch_profile_path=launch_profile_path,
        )
        rows = tuple(
            row
            for row in rows
            if _candidate_uid(
                row,
                launch_profile_path=launch_profile_path,
            )
            not in attempted
        )
        if not rows:
            return ()
        return submit_manifest(
            queue_root,
            launch_profile_path=launch_profile_path,
            rows=rows,
            attempted_control_uids=attempted,
        )
    rows = validate_manifest(
        manifest_path,
        launch_profile_path=launch_profile_path,
    )
    existing = list_requests(queue_root)
    expected_by_source = {
        str(row["source"]): row
        for row in rows
    }
    for index, request in enumerate(existing, start=1):
        source = str(request.get("source", ""))
        expected = expected_by_source.get(source)
        if expected is None:
            continue
        overlay = request.get("overlay")
        expected_uid = _candidate_uid(
            expected,
            launch_profile_path=launch_profile_path,
        )
        if (
            not isinstance(overlay, Mapping)
            or request.get("control_candidate_uid") != expected_uid
            or request.get("source") != expected["source"]
            or request.get("position") != expected["position"]
            or request.get("occurrence_nonce") != expected["occurrence_nonce"]
            or any(
                not math.isclose(
                    float(overlay.get(name, math.nan)),
                    float(expected[name]),
                    rel_tol=1e-12,
                    abs_tol=1e-15,
                )
                for name in (
                    "force_p_gain",
                    "force_i_gain",
                    "force_damping",
                    "orientation_ko",
                )
            )
        ):
            raise ParameterManifestError(
                f"existing receiver differs from initial row {index}"
            )
    return ()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--queue-root", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--launch-profile", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    rows = seed_initial_manifest(
        args.queue_root,
        campaign_id=args.campaign_id,
        release_manifest_sha256=args.release_manifest_sha256,
        launch_profile_path=args.launch_profile,
        manifest_path=args.manifest,
        experiment_root=args.experiment_root,
    )
    print(json.dumps({"seeded": len(rows)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ParameterManifestError, ParameterQueueError, ValueError) as exc:
        print(f"initial parameter manifest blocked: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
