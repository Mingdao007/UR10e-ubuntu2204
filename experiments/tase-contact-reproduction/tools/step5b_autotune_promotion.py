#!/usr/bin/env python3
"""Build a fingerprinted Step5b-to-Step5d outer-loop parameter overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from step5b_autotune_contract import (
    EXPERIMENT_ROOT,
    OBJECTIVE_NAME,
    OBJECTIVE_UNIT,
    Candidate,
)
from step5b_autotune_evidence import (
    BACKEND_ID,
    TRIAL_SPEC_SCHEMA,
    candidate_uid_from_payload,
    fingerprint_core,
    physical_capture_uid_from_sha256,
    quarantine_jsonl_record,
    read_evaluation_jsonl,
    sha256_file,
    source_config_fingerprint,
    trial_uid_from_identity,
)
from step5b_autotune_optimizer import Observation


PROMOTION_SCHEMA = "step5b_to_step5d_outer_loop_v1"
REPEATABILITY_LIMIT = 0.15
SELECTION_RULE = "lowest observed 12 N force-MAE incumbent with latest-two distinct-trial repeatability"
STEP5D_ENV_KEYS = (
    "STEP5D_FORCE_P_GAIN",
    "STEP5D_FORCE_I_GAIN",
    "STEP5D_FORCE_DAMPING",
    "STEP5D_NORMAL_FILTER_ALPHA",
)
FINGERPRINT_PATHS = (
    "UR_FORCE_FRAME_CONTRACT.md",
    "config/step5b_autotune_delivery_v2.json",
    "config/step5b_autotune_loop_v2.json",
    "config/step5b_autotune_numeric_sanity.json",
    "config/step5b_autotune_stage_table.json",
    "config/step5b_tp_autotune_authorization_v2.json",
    "config/step5_stage_table.json",
    "programs/step5/autotune/step5b_contact_cycloid_bayes_loop_v2.script",
    "programs/step5/autotune/step5b_contact_cycloid_bayes_loop_v2.txt",
    "programs/step5/autotune/step5b_contact_cycloid_bayes_loop_v2.urp",
    "scripts/step5d-liveprep-operator.sh",
    "tools/contact_semantics.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/step5b_autotune_contract.py",
    "tools/step5b_autotune_evidence.py",
    "tools/step5b_autotune_evaluator.py",
    "tools/step5b_autotune_optimizer.py",
    "tools/step5b_autotune_promotion.py",
    "tools/step5b_autotune_supervisor.py",
    "tools/step5d_runtime_interface.py",
)
REQUIRED_PROVENANCE_ROLES = frozenset(
    {
        "metadata",
        "summary",
        "trial_runtime",
        "trial_spec",
        "capture_complete",
        "fingerprint_pre",
        "fingerprint_post",
        "bridge_csv",
    }
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _strict_json_object(path: Path, *, role: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant is forbidden: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{role} provenance artifact is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{role} provenance artifact is not a JSON object")
    return payload


def verify_provenance_artifacts(payload: dict[str, Any]) -> None:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("eligible observation provenance is missing")
    missing_roles = REQUIRED_PROVENANCE_ROLES.difference(provenance)
    if missing_roles:
        raise ValueError(f"eligible observation provenance roles missing: {sorted(missing_roles)}")

    paths: dict[str, Path] = {}
    digests: dict[str, str] = {}
    for role, reference in provenance.items():
        if reference is None:
            if role in REQUIRED_PROVENANCE_ROLES:
                raise ValueError(f"required provenance role is empty: {role}")
            continue
        if not isinstance(reference, dict):
            raise ValueError(f"provenance reference is not an object: {role}")
        path = Path(str(reference.get("path", "")))
        expected_sha256 = str(reference.get("sha256", ""))
        current_sha256 = sha256_file(path)
        if current_sha256 is None or current_sha256 != expected_sha256:
            raise ValueError(f"{role} bytes changed after evaluation")
        paths[role] = path
        digests[role] = current_sha256

    trial_spec = _strict_json_object(paths["trial_spec"], role="trial_spec")
    runtime = _strict_json_object(paths["trial_runtime"], role="trial_runtime")
    capture = _strict_json_object(paths["capture_complete"], role="capture_complete")
    pre_record = _strict_json_object(paths["fingerprint_pre"], role="fingerprint_pre")
    post_record = _strict_json_object(paths["fingerprint_post"], role="fingerprint_post")
    _strict_json_object(paths["metadata"], role="metadata")
    _strict_json_object(paths["summary"], role="summary")

    identity = {
        "backend_id": BACKEND_ID,
        "session_uid": payload.get("session_uid"),
        "candidate_uid": payload.get("candidate_uid"),
        "trial_uid": payload.get("trial_uid"),
        "trial_spec_sha256": payload.get("trial_spec_sha256"),
        "physical_capture_uid": payload.get("physical_capture_uid"),
    }
    if trial_spec.get("schema_version") != TRIAL_SPEC_SCHEMA:
        raise ValueError("trial_spec provenance schema mismatch")
    if trial_spec.get("backend_id") != BACKEND_ID:
        raise ValueError("trial_spec provenance backend mismatch")
    if trial_spec.get("session_uid") != identity["session_uid"]:
        raise ValueError("trial_spec provenance session_uid mismatch")
    if int(trial_spec.get("trial_id", 0)) != int(payload.get("trial_id", 0)):
        raise ValueError("trial_spec provenance trial_id mismatch")
    if trial_spec.get("candidate_uid") != identity["candidate_uid"]:
        raise ValueError("trial_spec provenance candidate_uid mismatch")
    if trial_spec.get("trial_uid") != identity["trial_uid"]:
        raise ValueError("trial_spec provenance trial_uid mismatch")
    if trial_spec.get("candidate") != payload.get("candidate"):
        raise ValueError("trial_spec provenance candidate mismatch")
    if digests["trial_spec"] != identity["trial_spec_sha256"]:
        raise ValueError("trial_spec provenance digest mismatch")

    for role, artifact in (("trial_runtime", runtime), ("capture_complete", capture)):
        if any(artifact.get(key) != value for key, value in identity.items()):
            raise ValueError(f"{role} provenance full-width identity mismatch")
    if runtime.get("schema_version") != "step5b_autotune_trial_runtime_v4":
        raise ValueError("trial_runtime provenance schema mismatch")
    if capture.get("schema_version") != "step5b_autotune_capture_manifest_v4":
        raise ValueError("capture_complete provenance schema mismatch")

    pre_core = fingerprint_core(pre_record, expected_phase="pre")
    post_core = fingerprint_core(post_record, expected_phase="post")
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, dict):
        raise ValueError("evaluation fingerprint is missing")
    fingerprint_sha256 = fingerprint.get("post_combined_sha256")
    if pre_core != post_core or pre_core.get("combined_sha256") != fingerprint_sha256:
        raise ValueError("pre/post fingerprint provenance differs from evaluation")
    if trial_spec.get("fingerprint_pre_sha256") != fingerprint_sha256:
        raise ValueError("trial_spec fingerprint provenance mismatch")
    for role, artifact in (("trial_runtime", runtime), ("capture_complete", capture)):
        if artifact.get("fingerprint_pre_sha256") != fingerprint_sha256:
            raise ValueError(f"{role} pre fingerprint mismatch")
        if artifact.get("fingerprint_post_sha256") != fingerprint_sha256:
            raise ValueError(f"{role} post fingerprint mismatch")

    bridge_sha256 = digests["bridge_csv"]
    if physical_capture_uid_from_sha256(bridge_sha256) != identity["physical_capture_uid"]:
        raise ValueError("bridge capture identity differs from current artifact")


def read_evaluations(path: Path) -> list[dict[str, Any]]:
    evaluations: list[dict[str, Any]] = []
    seen_trial_uids: set[str] = set()
    seen_physical_capture_uids: set[str] = set()
    current_fingerprint = str(source_config_fingerprint()["combined_sha256"])
    history_fingerprint: str | None = current_fingerprint
    for record in read_evaluation_jsonl(path):
        payload = record.payload
        try:
            candidate_payload = payload.get("candidate")
            if not isinstance(candidate_payload, dict):
                if payload.get("eligible") or payload.get("feasible"):
                    raise ValueError("eligible observation lacks candidate")
                evaluations.append(payload)
                continue
            candidate = Candidate(**candidate_payload)
            candidate.validate(tier2_unlocked=True)
            if payload.get("eligible"):
                Observation.from_payload(payload)
                trial_uid = str(payload.get("trial_uid", ""))
                fingerprint = payload.get("fingerprint")
                if len(trial_uid) != 64:
                    raise ValueError("eligible observation lacks stable trial_uid")
                candidate_uid = str(payload.get("candidate_uid", ""))
                if candidate_uid != candidate_uid_from_payload(candidate.payload()):
                    raise ValueError("eligible observation candidate_uid mismatch")
                session_uid = str(payload.get("session_uid", ""))
                trial_id = int(payload.get("trial_id", 0))
                if trial_uid_from_identity(session_uid, trial_id, candidate_uid) != trial_uid:
                    raise ValueError("eligible observation trial_uid mismatch")
                physical_capture_uid = str(payload.get("physical_capture_uid", ""))
                if len(physical_capture_uid) != 64:
                    raise ValueError("eligible observation lacks physical_capture_uid")
                if payload.get("backend_id") != BACKEND_ID:
                    raise ValueError("eligible observation backend mismatch")
                if not isinstance(fingerprint, dict) or not fingerprint.get("verified"):
                    raise ValueError("eligible observation fingerprint is unverified")
                fingerprint_sha256 = str(fingerprint.get("post_combined_sha256", ""))
                if (
                    len(fingerprint_sha256) != 64
                    or fingerprint.get("pre_combined_sha256") != fingerprint_sha256
                ):
                    raise ValueError("eligible observation fingerprint digest invalid")
                if len(str(payload.get("trial_spec_sha256", ""))) != 64:
                    raise ValueError("eligible observation trial_spec_sha256 invalid")
                if history_fingerprint is not None and fingerprint_sha256 != history_fingerprint:
                    raise ValueError("eligible observation fingerprint crosses history epoch")
                if payload.get("disposition") not in {"OBJECTIVE", "PARAMETER_CONSTRAINT"}:
                    raise ValueError("eligible observation disposition is not trainable")
                if trial_uid in seen_trial_uids:
                    raise ValueError(f"duplicate trial_uid {trial_uid}")
                if physical_capture_uid in seen_physical_capture_uids:
                    raise ValueError(f"duplicate physical_capture_uid {physical_capture_uid}")
                verify_provenance_artifacts(payload)
                seen_trial_uids.add(trial_uid)
                seen_physical_capture_uids.add(physical_capture_uid)
                history_fingerprint = history_fingerprint or fingerprint_sha256
        except (KeyError, TypeError, ValueError) as exc:
            quarantine_jsonl_record(
                path,
                record.line_number,
                record.raw_line,
                f"promotion_observation_rejected:{type(exc).__name__}:{exc}",
            )
            continue
        evaluations.append(payload)
    return evaluations


def candidate_key(payload: dict[str, Any]) -> str:
    return canonical_bytes(payload).decode("utf-8")


def source_fingerprints() -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in FINGERPRINT_PATHS:
        path = EXPERIMENT_ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"promotion fingerprint source missing: {path}")
        result[relative] = sha256_bytes(path.read_bytes())
    return result


def build_promotion_payload(observations_path: Path) -> dict[str, Any]:
    evaluations = read_evaluations(observations_path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for payload in evaluations:
        objective = payload.get("objective")
        trial_uid = str(payload.get("trial_uid", "")).strip()
        qualifying = bool(
            payload.get("eligible")
            and payload.get("feasible")
            and payload.get("full_trial")
            and payload.get("supervisor_closure_verified")
            and payload.get("disposition") == "OBJECTIVE"
            and not payload.get("failures")
            and not payload.get("quarantined")
            and trial_uid
            and payload.get("backend_id") == BACKEND_ID
            and payload.get("fingerprint", {}).get("verified")
            and isinstance(payload.get("candidate"), dict)
        )
        if not qualifying or objective is None:
            continue
        objective = float(objective)
        if not math.isfinite(objective):
            continue
        groups.setdefault(candidate_key(payload["candidate"]), []).append(payload)

    summaries_by_key: dict[str, dict[str, Any]] = {}
    candidate_summaries: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        values = [float(item["objective"]) for item in group]
        latest_two = values[-2:]
        relative_delta = None
        if len(latest_two) == 2:
            relative_delta = abs(latest_two[1] - latest_two[0]) / max(1e-9, min(latest_two))
        summary = {
            "candidate": group[-1]["candidate"],
            "feasible_full_trials": len(group),
            "latest_two_force_mae_n": latest_two,
            "latest_two_relative_delta": relative_delta,
            "source_runs": [str(item.get("run_dir", "")) for item in group],
            "source_trial_uids": [str(item["trial_uid"]) for item in group],
            "source_physical_capture_uids": [
                str(item["physical_capture_uid"]) for item in group
            ],
            "source_backends": [str(item["backend_id"]) for item in group],
            "source_fingerprints": [item["fingerprint"] for item in group],
            "source_artifact_hashes": [item.get("provenance", {}) for item in group],
        }
        candidate_summaries.append(summary)
        summaries_by_key[key] = summary

    fingerprints = source_fingerprints()
    base: dict[str, Any] = {
        "schema_version": PROMOTION_SCHEMA,
        "objective_name": OBJECTIVE_NAME,
        "objective_unit": OBJECTIVE_UNIT,
        "target_force_n": 12.0,
        "repeatability_limit": REPEATABILITY_LIMIT,
        "source_observations": str(observations_path.resolve()),
        "source_observations_sha256": sha256_file(observations_path) or sha256_bytes(b""),
        "source_fingerprints": fingerprints,
        "evaluations_seen": len(evaluations),
        "candidate_summaries": candidate_summaries,
        "live_authorization": False,
        "step5d_auto_apply": False,
    }
    eligible_observations = [item for group in groups.values() for item in group]
    if not eligible_observations:
        return {
            **base,
            "status": "candidate_not_promotable",
            "reason": "no feasible full 60 s force-MAE observation exists",
        }

    incumbent_observation = min(eligible_observations, key=lambda item: float(item["objective"]))
    selected = summaries_by_key[candidate_key(incumbent_observation["candidate"])]
    relative_delta = selected["latest_two_relative_delta"]
    if (
        selected["feasible_full_trials"] < 2
        or relative_delta is None
        or float(relative_delta) > REPEATABILITY_LIMIT
    ):
        return {
            **base,
            "status": "candidate_not_promotable",
            "incumbent_candidate": selected["candidate"],
            "reason": "the 12 N force-MAE incumbent lacks two full trials within 15% repeatability",
        }

    selected = {
        **selected,
        "selection_score_mean_latest_two_force_mae_n": float(
            sum(selected["latest_two_force_mae_n"]) / 2.0
        ),
    }
    candidate = Candidate(**selected["candidate"])
    mapping = {
        "STEP5D_FORCE_P_GAIN": candidate.force_p_gain,
        "STEP5D_FORCE_I_GAIN": candidate.force_i_gain,
        "STEP5D_FORCE_DAMPING": candidate.force_damping,
        "STEP5D_NORMAL_FILTER_ALPHA": candidate.normal_filter_alpha,
    }
    promotion_core = {
        "schema_version": PROMOTION_SCHEMA,
        "objective_name": OBJECTIVE_NAME,
        "objective_unit": OBJECTIVE_UNIT,
        "target_force_n": 12.0,
        "repeatability_limit": REPEATABILITY_LIMIT,
        "selection_rule": SELECTION_RULE,
        "candidate": candidate.payload(),
        "selection_score_mean_latest_two_force_mae_n": selected[
            "selection_score_mean_latest_two_force_mae_n"
        ],
        "source_runs": selected["source_runs"],
        "source_trial_uids": selected["source_trial_uids"],
        "source_physical_capture_uids": selected["source_physical_capture_uids"],
        "source_backends": selected["source_backends"],
        "source_trial_fingerprints": selected["source_fingerprints"],
        "source_artifact_hashes": selected["source_artifact_hashes"],
        "step5d_env": mapping,
        "source_observations_sha256": base["source_observations_sha256"],
        "source_fingerprints": fingerprints,
    }
    return {
        **base,
        "status": "promotable",
        "promotion_id": sha256_bytes(canonical_bytes(promotion_core)),
        **promotion_core,
    }


def env_text(payload: dict[str, Any]) -> str:
    if payload.get("status") != "promotable":
        raise ValueError("cannot render Step5d env for a non-promotable candidate")
    lines = [
        "# Generated Step5b -> Step5d outer-loop overlay; does not authorize live motion.",
        f"# promotion_id={payload['promotion_id']}",
    ]
    if tuple(payload["step5d_env"]) != STEP5D_ENV_KEYS:
        raise ValueError("Step5d env mapping must contain exactly the four governed keys")
    for key, value in payload["step5d_env"].items():
        lines.append(f"export {key}={float(value):.10g}")
    return "\n".join(lines) + "\n"


def write_promotion_artifacts(observations_path: Path, output_dir: Path) -> dict[str, Any]:
    payload = build_promotion_payload(observations_path)
    latest_json = output_dir / "step5b_to_step5d_outer_loop_latest.json"
    if payload.get("status") != "promotable":
        atomic_write(latest_json, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return {**payload, "latest_json": str(latest_json)}

    short_id = str(payload["promotion_id"])[:16]
    immutable_json = output_dir / f"step5b_to_step5d_outer_loop_{short_id}.json"
    immutable_env = output_dir / f"step5b_to_step5d_outer_loop_{short_id}.env"
    payload = {
        **payload,
        "immutable_json": str(immutable_json),
        "immutable_env": str(immutable_env),
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if immutable_json.exists() and immutable_json.read_text(encoding="utf-8") != text:
        raise RuntimeError(f"immutable promotion artifact collision: {immutable_json}")
    if immutable_env.exists() and immutable_env.read_text(encoding="utf-8") != env_text(payload):
        raise RuntimeError(f"immutable promotion env collision: {immutable_env}")
    atomic_write(immutable_json, text)
    atomic_write(immutable_env, env_text(payload))
    atomic_write(latest_json, text)
    return {**payload, "latest_json": str(latest_json)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or args.observations.parent
    payload = write_promotion_artifacts(args.observations, output_dir)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
