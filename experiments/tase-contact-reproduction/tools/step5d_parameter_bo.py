#!/usr/bin/env python3
"""Deterministic, offline Step5d parameter proposal primitives.

This module has no bridge, controller, socket, Dashboard, or robot imports.
It reads only immutable post-process results and the authoritative receiver
queue view.  CUDA/qLogNEI is intentionally imported lazily because the
receiver/control runtime must remain dependency-light.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from step5d_autotune_contract import Evaluation, ForceCandidate, TrialDisposition
from step5d_autotune_v3.atomic_io import atomic_bytes
from step5d_autotune_v3.state import read_strict_json
from step5d_parameter_outbox import RESULT_SCHEMA
from step5d_parameter_queue import authoritative_view
from step5d_parameter_search_domain import (
    production_candidate_catalog,
    require_search_candidate,
)


PROPOSAL_SCHEMA = "step5d.parameter-receiver/offline-bo-proposal-v1"
PROFILE_ID = "nf5000-slew250-a250"
ORIENTATION_KO = 0.4
Optimizer = Callable[
    [Sequence[Any], Sequence[ForceCandidate], int, int],
    tuple[tuple[ForceCandidate, ...], dict[str, Any]],
]


class ParameterBoError(RuntimeError):
    """The formal optimizer input or output is inconsistent."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_object(path: Path, role: str) -> dict[str, Any]:
    payload = read_strict_json(path, role=role)
    if not isinstance(payload, dict):
        raise ParameterBoError(f"{role} must be a JSON object")
    return payload


def _finite(payload: Mapping[str, Any], key: str, role: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParameterBoError(f"{role} {key} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ParameterBoError(f"{role} {key} must be finite")
    return number


def _positive_int(payload: Mapping[str, Any], key: str, role: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ParameterBoError(f"{role} {key} must be a positive integer")
    return value


def _candidate(payload: Mapping[str, Any], role: str) -> ForceCandidate:
    orientation = _finite(payload, "orientation_ko", role)
    if not math.isclose(orientation, ORIENTATION_KO, rel_tol=0.0, abs_tol=1e-12):
        raise ParameterBoError(f"{role} orientation_ko is not fixed at 0.4")
    try:
        return ForceCandidate(
            force_p_gain=_finite(payload, "force_p_gain", role),
            force_i_gain=_finite(payload, "force_i_gain", role),
            force_damping=_finite(payload, "force_damping", role),
            normal_filter_tau_s=_finite(payload, "normal_filter_tau_s", role),
        )
    except ValueError as exc:
        raise ParameterBoError(f"{role} is not a canonical candidate: {exc}") from exc


def _eligible(result: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    objective = result.get("objective")
    profile = result.get("profile")
    if not isinstance(objective, dict) or not isinstance(profile, dict):
        return False, ("objective_or_profile_missing",)
    if objective.get("complete_bins") != 550 or objective.get("required_bins") != 550:
        reasons.append("objective_bins_incomplete")
    # Profile qualification is diagnostic evidence, not an observation
    # deletion gate.  In particular, normal-rate limiter saturation duty must
    # not erase an otherwise complete, safely closed force-MAE observation.
    # Live safety and safe closure are owned upstream; BO trains on the sealed
    # objective and may use profile fields only as diagnostics.
    for key in ("mae_n", "bias_n", "std_n", "coverage_12_plus_minus_1_ratio"):
        try:
            _finite(objective, key, "objective")
        except ParameterBoError:
            reasons.append(f"objective_{key}_invalid")
    return not reasons, tuple(reasons)


def load_observations(
    outbox_root: Path,
) -> tuple[tuple[Any, ...], frozenset[str], tuple[dict[str, Any], ...]]:
    """Load immutable sealed results once per result identity.

    Ineligible results still reserve their candidate identity.  This prevents
    a failed or structurally unsuitable candidate from being proposed again.
    An empty history is valid to the feeder; the formal optimizer itself
    enforces its six-observation gate.
    """

    results_root = outbox_root.expanduser().absolute() / "results"
    if results_root.is_symlink() or not results_root.is_dir():
        raise ParameterBoError("postprocess results root must be a real directory")
    observations: list[Any] = []
    seen_candidate_uids: set[str] = set()
    material: list[dict[str, Any]] = []
    dispatch_sequences: set[int] = set()
    trial_uids: set[str] = set()
    for result_path in sorted(results_root.glob("*.json")):
        if result_path.name.endswith(".part.json"):
            continue
        if result_path.is_symlink() or not result_path.is_file():
            raise ParameterBoError("postprocess result must be a real file")
        result = _strict_object(result_path, "postprocess result")
        if result.get("schema") != RESULT_SCHEMA or result.get("status") != "SUCCEEDED":
            raise ParameterBoError("unsupported postprocess result schema/status")
        dispatch_sequence = _positive_int(result, "dispatch_sequence", "postprocess result")
        trial_uid = result.get("trial_uid")
        if (
            not isinstance(trial_uid, str)
            or len(trial_uid) != 64
            or any(character not in "0123456789abcdef" for character in trial_uid)
        ):
            raise ParameterBoError("postprocess result trial_uid is invalid")
        if dispatch_sequence in dispatch_sequences or trial_uid in trial_uids:
            raise ParameterBoError("duplicate dispatch or trial identity")
        dispatch_sequences.add(dispatch_sequence)
        trial_uids.add(trial_uid)
        candidate_payload = result.get("candidate")
        if not isinstance(candidate_payload, dict):
            raise ParameterBoError("postprocess result candidate is missing")
        candidate = _candidate(candidate_payload, "postprocess candidate")
        seen_candidate_uids.add(candidate.candidate_uid)
        eligible, exclusion_reasons = _eligible(result)
        objective = result.get("objective")
        identity = result.get("identity")
        if not isinstance(objective, dict) or not isinstance(identity, dict):
            raise ParameterBoError("postprocess objective/identity is missing")
        if eligible:
            backend_id = identity.get("backend_id")
            if not isinstance(backend_id, str) or not backend_id:
                raise ParameterBoError("eligible result backend identity is missing")
            try:
                evaluation = Evaluation(
                    trial_uid=trial_uid,
                    backend_id=backend_id,
                    eligible=True,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective_mae_n=_finite(objective, "mae_n", "objective"),
                    force_bias_n=_finite(objective, "bias_n", "objective"),
                    force_std_n=_finite(objective, "std_n", "objective"),
                    coverage_12_plus_minus_1_ratio=_finite(
                        objective, "coverage_12_plus_minus_1_ratio", "objective"
                    ),
                    complete_bins=550,
                    safe_closure=True,
                    metrics={"postprocess_result_sha256": _sha256_file(result_path)},
                )
            except ValueError as exc:
                raise ParameterBoError("eligible result evaluation is invalid") from exc
            from step5d_autotune_optimizer import Observation

            observations.append(Observation(candidate, evaluation, PROFILE_ID, 1))
        material.append(
            {
                "path": str(result_path.resolve(strict=True)),
                "sha256": _sha256_file(result_path),
                "dispatch_sequence": dispatch_sequence,
                "trial_uid": trial_uid,
                "candidate_uid": candidate.candidate_uid,
                "eligible": eligible,
                "exclusion_reasons": list(exclusion_reasons),
                "objective_mae_n": objective.get("mae_n") if eligible else None,
            }
        )
    return tuple(observations), frozenset(seen_candidate_uids), tuple(material)


def load_pending_candidates(
    receiver_root: Path,
    *,
    queue_view: Mapping[str, Any] | None = None,
) -> tuple[frozenset[str], tuple[dict[str, Any], ...]]:
    """Return candidate identities from pending *and inflight* requests."""

    receiver_root = receiver_root.expanduser().absolute()
    view = authoritative_view(receiver_root) if queue_view is None else queue_view
    requests = view.get("requests")
    state = view.get("state")
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
        raise ParameterBoError("authoritative queue request view is missing")
    if not isinstance(state, Mapping):
        raise ParameterBoError("authoritative queue state view is missing")
    inflight = state.get("inflight")
    inflight_uid = inflight.get("request_uid") if isinstance(inflight, Mapping) else None
    pending_uids: set[str] = set()
    material: list[dict[str, Any]] = []
    pending_uids_from_view = {
        str(row.get("request_uid"))
        for row in view.get("pending_requests", ())
        if isinstance(row, Mapping) and row.get("request_uid")
    }
    for request in requests:
        if not isinstance(request, Mapping):
            raise ParameterBoError("authoritative queue request is not an object")
        request_uid = request.get("request_uid")
        if request_uid not in pending_uids_from_view and request_uid != inflight_uid:
            continue
        overlay = request.get("overlay")
        if not isinstance(overlay, dict):
            raise ParameterBoError("pending request overlay is missing")
        candidate = _candidate(overlay, "pending candidate")
        enqueue_sequence = _positive_int(request, "enqueue_sequence", "pending request")
        request_path = receiver_root / "requests" / f"{enqueue_sequence:012d}.json"
        if request_path.is_symlink() or not request_path.is_file():
            raise ParameterBoError("pending request source file is missing")
        pending_uids.add(candidate.candidate_uid)
        material.append(
            {
                "path": str(request_path.resolve(strict=True)),
                "sha256": _sha256_file(request_path),
                "enqueue_sequence": enqueue_sequence,
                "request_uid": request_uid,
                "candidate_uid": candidate.candidate_uid,
                "position": request.get("position"),
                "source": request.get("source"),
                "inflight": request_uid == inflight_uid,
            }
        )
    material.sort(key=lambda row: int(row["enqueue_sequence"]))
    return frozenset(pending_uids), tuple(material)


def formal_cuda_qlognei(
    observations: Sequence[Any],
    candidates: Sequence[ForceCandidate],
    *,
    q: int,
    seed: int,
) -> tuple[tuple[ForceCandidate, ...], dict[str, Any]]:
    """Run the repository's deterministic CUDA qLogNEI implementation."""

    from step5d_autotune_optimizer import cuda_botorch_joint_candidates

    return cuda_botorch_joint_candidates(observations, candidates, q=q, seed=seed)


def best_eligible_candidate(observations: Sequence[Any]) -> ForceCandidate | None:
    eligible = [item for item in observations if getattr(item, "eligible", False)]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (float(item.objective), item.candidate.candidate_uid,
                          item.evaluation.trial_uid),
    ).candidate


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise ParameterBoError("immutable BO proposal differs")
        return
    atomic_bytes(path, encoded)


def propose_candidates(
    *,
    outbox_root: Path,
    receiver_root: Path,
    output_path: Path,
    q: int,
    seed: int,
    label_start: int,
    source_prefix: str = "formal_outbox_bo",
    optimizer: Callable[..., tuple[tuple[ForceCandidate, ...], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
        raise ParameterBoError("q must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ParameterBoError("seed must be a non-negative integer")
    if isinstance(label_start, bool) or not isinstance(label_start, int) or label_start <= 0:
        raise ParameterBoError("label_start must be a positive integer")
    observations, observed_uids, observation_material = load_observations(outbox_root)
    pending_uids, pending_material = load_pending_candidates(receiver_root)
    excluded = observed_uids | pending_uids
    catalog = tuple(
        require_search_candidate(candidate, role="BO catalog candidate")
        for candidate in production_candidate_catalog()
        if candidate.candidate_uid not in excluded
    )
    if len(catalog) < q:
        raise ParameterBoError("candidate catalog is smaller than q")
    if optimizer is None:
        selected, optimizer_metadata = formal_cuda_qlognei(
            observations, catalog, q=q, seed=seed
        )
    else:
        try:
            selected, optimizer_metadata = optimizer(observations, catalog, q, seed)
        except TypeError:
            selected, optimizer_metadata = optimizer(observations, catalog)
    if (
        len(selected) != q
        or len({candidate.candidate_uid for candidate in selected}) != q
        or any(candidate.candidate_uid in excluded for candidate in selected)
    ):
        raise ParameterBoError("optimizer returned invalid or excluded candidates")
    try:
        selected = tuple(
            require_search_candidate(candidate, role="optimizer-selected candidate")
            for candidate in selected
        )
    except ValueError as exc:
        raise ParameterBoError(str(exc)) from exc
    material_digest = hashlib.sha256(
        json.dumps(
            {"observations": observation_material, "pending": pending_material},
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    proposed: list[dict[str, Any]] = []
    for index, candidate in enumerate(selected):
        label = f"R{label_start + index}"
        occurrence_nonce = hashlib.sha256(
            f"{material_digest}:{seed}:{candidate.candidate_uid}:{label}".encode("utf-8")
        ).hexdigest()[:32]
        proposed.append(
            {
                "label": label,
                "candidate_uid": candidate.candidate_uid,
                "force_p_gain": candidate.force_p_gain,
                "force_i_gain": candidate.force_i_gain,
                "force_damping": candidate.force_damping,
                "normal_filter_tau_s": candidate.normal_filter_tau_s,
                "orientation_ko": ORIENTATION_KO,
                "position": "next",
                "source": f"{source_prefix}_q{q}_seed{seed}:{label}",
                "occurrence_nonce": occurrence_nonce,
            }
        )
    payload = {
        "schema": PROPOSAL_SCHEMA,
        "status": "OFFLINE_READY_NOT_SUBMITTED",
        "objective_contract": {"target_force_n": 12.0, "window": "[5,60)s"},
        "seed": seed,
        "q": q,
        "observed_candidate_count": len(observed_uids),
        "pending_candidate_count": len(pending_uids),
        "material_sha256": material_digest,
        "optimizer": optimizer_metadata,
        "proposals": proposed,
    }
    _write_immutable_json(output_path, payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outbox-root", type=Path, required=True)
    parser.add_argument("--receiver-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--q", type=int, default=8)
    parser.add_argument("--seed", type=int, default=9009)
    parser.add_argument("--label-start", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        propose_candidates(
            outbox_root=args.outbox_root,
            receiver_root=args.receiver_root,
            output_path=args.output,
            q=args.q,
            seed=args.seed,
            label_start=args.label_start,
        )
    except (ParameterBoError, OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
