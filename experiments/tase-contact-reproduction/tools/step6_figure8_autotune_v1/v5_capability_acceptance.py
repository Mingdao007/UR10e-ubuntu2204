"""One-shot, release-bound capability acceptance for Autotuner V5.

This is not campaign qualification and never calls ``tell_exact``.  It binds
the no-motion recipe probe and no-contact canary, then exercises the already
implemented resident rollover owner through a deterministic set of contact
chains.  Every motion intent is journaled before dispatch, every result is
owner-sealed before fan-in, and the final receipt is cold-recomputable.

The only performance comparison that can block this acceptance is the
predeclared five-pair 90% CI for formal-MAE difference.  The old narrow force
windows remain telemetry only.  Safety, motion, timing, freshness, tube/CBF,
identity, and command-envelope decisions come exclusively from the existing
per-record gate closures.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

from .campaign_runner import _stop_exact_resident
from .core import CompleteCandidateV1, FigureEightCampaignFingerprintV1
from .prepare_live import prepare_figure8_live_run
from .source_identity import build_source_identity
from .v5_campaign import (
    CampaignRoleV2,
    ProposalMethodV2,
    ProposalReceiptV2,
    TrialStageV2,
    V5TrialPlan,
    _candidate_token,
)
from .v5_campaign_runner import (
    V5AmbiguousPhysicalDispatch,
    V5DurableChainResultV1,
    V5ExecutionJournalV1,
)
from .v5_composition_contract import V5AttemptKind
from .v5_lifecycle_ledger import BoundaryMode, LedgerRole, canonical_sha256
from .v5_live_owner import (
    V5_LIVE_BINDING_SCHEMA,
    V5_LIVE_OWNER_VERSION,
    V5LiveChainRequestV1,
    V5SingleWriterOwnerV1,
    build_v5_live_context,
)
from .v5_register_transport import LiveV5RTDETransport, v5_recipe_contract_receipt


V5_CAPABILITY_SCHEMA = "step6.autotune/autotuner-v5-capability-acceptance-v1"
V5_CAPABILITY_VERSION = 1
V5_NO_MOTION_SCHEMA = "step6.autotune/autotuner-v5-live-no-motion-recipe-receipt-v1"
V5_CAPABILITY_CI = 0.90
V5_CAPABILITY_PAIRS = 5
V5_CAPABILITY_MARGIN_N = 0.05
V5_T_CRITICAL_DF4_90 = 2.131846786326649
# These constants are only for reporting the pilot's measurement-resolution
# estimate.  The live equivalence gate remains the declared paired 90% CI and
# the +/-0.05 N margin; neither estimate can widen that gate.
V5_Z_90_TWO_SIDED = 1.6448536269514722
V5_Z_95_ONE_SIDED = 1.6448536269514722
V5_Z_POWER_80 = 0.8416212335729143
V5_DECLARED_PAIR_UPPER_BOUND = 80
V5_NEIGHBOR_LOG2_MOTION_KP_DELTA = 1.0 / 16.0
V5_CAPABILITY_CHAIN_IDS = (
    "cap-same-candidate-01",
    "cap-bounded-neighbor-01",
    "cap-entry-pair-02",
    "cap-entry-pair-03",
    "cap-entry-pair-04",
    "cap-entry-pair-05",
    "cap-four-rollover-01",
)


class V5CapabilityAcceptanceError(RuntimeError):
    """The release-bound, one-shot capability acceptance failed closed."""


class V5CapabilityMeasurementResolutionError(V5CapabilityAcceptanceError):
    """The declared equivalence margin requires more than the pilot budget."""

    def __init__(
        self,
        message: str,
        *,
        mean_n: float,
        ci90_n: Sequence[float],
        differences_n: Sequence[float],
        estimated_pairs_required: int,
        ci90_approx_pairs_required: int,
        pilot_std_n: float,
    ) -> None:
        super().__init__(message)
        self.mean_n = float(mean_n)
        self.ci90_n = tuple(float(value) for value in ci90_n)
        self.differences_n = tuple(float(value) for value in differences_n)
        self.estimated_pairs_required = int(estimated_pairs_required)
        self.ci90_approx_pairs_required = int(ci90_approx_pairs_required)
        self.pilot_std_n = float(pilot_std_n)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/v5-measurement-resolution-block-v2",
            "version": 2,
            "status": "NO_GO_MEASUREMENT_RESOLUTION",
            "mean_difference_n": self.mean_n,
            "ci90_n": list(self.ci90_n),
            "differences_n": list(self.differences_n),
            # The primary estimate is the normal-approximation 80% TOST
            # planning number.  Keep the CI-width estimate separately so it
            # cannot be mistaken for a powered equivalence design.
            "estimated_pairs_required": self.estimated_pairs_required,
            "estimated_pairs_required_tost80": self.estimated_pairs_required,
            "estimated_pairs_required_ci90_approx": self.ci90_approx_pairs_required,
            "pilot_std_n_ddof1": self.pilot_std_n,
            "tost_alpha_each": 0.05,
            "power_target": 0.80,
            "declared_pair_upper_bound": V5_DECLARED_PAIR_UPPER_BOUND,
            "equivalence_margin_n": V5_CAPABILITY_MARGIN_N,
            "margin_widened": False,
            "campaign_qualification": False,
        }


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
        raise V5CapabilityAcceptanceError(
            "V5 capability value is not canonical JSON"
        ) from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha(path: Path) -> str:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise V5CapabilityAcceptanceError(
            f"V5 capability evidence is unavailable: {target}"
        )
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _require_sha(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V5CapabilityAcceptanceError(f"V5 capability {role} SHA-256 is invalid")
    return value


def _read_json(path: Path, role: str) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise V5CapabilityAcceptanceError(f"{role} is not a regular file")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V5CapabilityAcceptanceError(f"{role} is unreadable") from exc
    if not isinstance(value, dict):
        raise V5CapabilityAcceptanceError(f"{role} is not an object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any], *, replace: bool = True) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not replace and destination.exists():
        raise V5CapabilityAcceptanceError(
            f"V5 capability output already exists: {destination}"
        )
    temporary = destination.with_name(
        destination.name + f".tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise V5CapabilityAcceptanceError(
                    f"V5 capability output already exists: {destination}"
                ) from exc
            temporary.unlink()
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _triplet(root: Path, program: str) -> dict[str, str]:
    directory = Path(root) / "programs" / "step6"
    return {
        role: _file_sha(directory / f"{program}.{role}")
        for role in ("script", "txt", "urp")
    }


def verify_named_controller_readback(
    *, root: Path, readback_dir: Path, program: str
) -> dict[str, Any]:
    """Verify one governed uploader put/get manifest for a named Step6 triplet."""

    directory = Path(readback_dir).resolve()
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path, f"{program} controller read-back manifest")
    validation = manifest.get("validation")
    hashes = manifest.get("sha256")
    target_dir = "/programs/andyl/kunwei/step6"
    if (
        manifest.get("status") != "controller read-back verified"
        or not isinstance(validation, Mapping)
        or validation.get("program") != program
        or validation.get("target_dir") != target_dir
        or manifest.get("target_dir") != target_dir
        or manifest.get("fresh_controller_sha_verified") is not True
        or manifest.get("readback_source") != "fresh_controller_get"
        or not isinstance(hashes, Mapping)
    ):
        raise V5CapabilityAcceptanceError(
            f"{program} controller read-back is not one fresh put/get closure"
        )
    expected_triplet = _triplet(Path(root), program)
    validation_keys = {
        "script": "script_sha256",
        "txt": "txt_sha256",
        "urp": "urp_sha256",
    }
    local_hashes = hashes.get("local")
    controller_hashes = hashes.get("controller")
    readback_hashes = hashes.get("readback")
    if not all(
        isinstance(item, Mapping)
        for item in (local_hashes, controller_hashes, readback_hashes)
    ):
        raise V5CapabilityAcceptanceError(
            f"{program} controller read-back hash sets are incomplete"
        )
    for role, validation_key in validation_keys.items():
        extension = f".{role}"
        expected = expected_triplet[role]
        fetched = directory / f"{program}.{role}"
        if (
            validation.get(validation_key) != expected
            or local_hashes.get(extension) != expected
            or controller_hashes.get(extension) != expected
            or readback_hashes.get(extension) != expected
            or _file_sha(fetched) != expected
        ):
            raise V5CapabilityAcceptanceError(
                f"{program} controller/read-back {role} differs from local bytes"
            )
    return {
        "program": program,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _file_sha(manifest_path),
        "triplet_sha256": expected_triplet,
        "fresh_put_get_byte_equality": True,
    }


def verify_no_motion_recipe_receipt(
    path: Path,
    *,
    expected_source_sha256: str,
    expected_triplet_sha256: Mapping[str, str],
) -> dict[str, Any]:
    """Cold-verify that the exact release recipes were accepted without writes."""

    receipt_path = Path(path).resolve()
    value = _read_json(receipt_path, "V5 no-motion recipe receipt")
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    contract = v5_recipe_contract_receipt().as_dict()
    if (
        value.get("schema") != V5_NO_MOTION_SCHEMA
        or value.get("version") != V5_CAPABILITY_VERSION
        or value.get("receipt_sha256") != _sha(unsigned)
        or value.get("source_sha256") != expected_source_sha256
        or value.get("main_package_triplet_sha256")
        != dict(expected_triplet_sha256)
        or value.get("recipe_contract") != contract
        or value.get("recipe_contract_sha256") != _sha(contract)
        or not isinstance(value.get("managed_control_environment_id"), str)
        or len(value.get("managed_control_environment_id", "")) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value.get("managed_control_environment_id", "")
        )
        or value.get("rtde_setup_succeeded") is not True
        or value.get("input_packet_sent") is not False
        or value.get("motion_command_sent") is not False
        or value.get("program_state_changed") is not False
        or value.get("controller_upload_or_readback_performed") is not False
        or value.get("live_acceptance_claim") is not False
    ):
        raise V5CapabilityAcceptanceError(
            "V5 no-motion recipe receipt identity or no-write claim differs"
        )
    return {
        "path": str(receipt_path),
        "file_sha256": _file_sha(receipt_path),
        "receipt_sha256": value["receipt_sha256"],
        "recipe_contract_sha256": value["recipe_contract_sha256"],
        "passed": True,
        "input_packet_sent": False,
        "motion_command_sent": False,
        "program_state_changed": False,
    }


def execute_no_motion_recipe_probe(
    *, root: Path, output_path: Path, controller_host: str
) -> dict[str, Any]:
    """Open the exact 500 Hz RTDE recipes and close without sending any packet."""

    source = build_source_identity(Path(root))
    configured = _read_json(
        Path(root) / "config/step6/r013_figure8_direct_campaign_v1.json",
        "V5 compatibility config",
    )
    if configured.get("controller", {}).get("source_parent_sha256") != source[
        "source_sha256"
    ]:
        raise V5CapabilityAcceptanceError(
            "V5 source closure is stale before no-motion recipe probe"
        )
    contract = v5_recipe_contract_receipt().as_dict()
    transport = LiveV5RTDETransport(str(controller_host))
    try:
        transport.open()
        observed_input_types = list(transport.input_types)
        observed_output_types = list(transport.output_types)
    finally:
        transport.close()
    admission: Mapping[str, Any] = {}
    try:
        candidate = json.loads(
            os.environ.get("STEP5D_EXECUTION_ADMISSION_RECEIPT", "{}")
        )
        if isinstance(candidate, Mapping):
            admission = candidate
    except json.JSONDecodeError:
        pass
    environment_id = admission.get(
        "environment_id", admission.get("environment_sha256", "")
    )
    body = {
        "schema": V5_NO_MOTION_SCHEMA,
        "version": V5_CAPABILITY_VERSION,
        "controller_host": str(controller_host),
        "observed_at_unix_ns": time.time_ns(),
        "managed_control_environment_id": str(environment_id),
        "source_sha256": source["source_sha256"],
        "main_package_triplet_sha256": _triplet(
            Path(root), "step6_figure8_autotune_v1"
        ),
        "recipe_contract": contract,
        "recipe_contract_sha256": _sha(contract),
        "observed_input_types": observed_input_types,
        "observed_output_types": observed_output_types,
        "rtde_setup_succeeded": True,
        "input_packet_sent": False,
        "motion_command_sent": False,
        "program_state_changed": False,
        "controller_upload_or_readback_performed": False,
        "live_acceptance_claim": False,
    }
    receipt = {**body, "receipt_sha256": _sha(body)}
    _atomic_json(Path(output_path).resolve(), receipt, replace=False)
    return receipt


def verify_no_contact_canary(
    *,
    root: Path,
    canary_dir: Path,
    canary_readback_dir: Path,
    expected_home_pose: Sequence[float],
    expected_home_receipt_sha256: str,
) -> dict[str, Any]:
    """Bind the one-shot no-contact evidence to current canary package bytes."""

    directory = Path(canary_dir).resolve()
    evidence_path = directory / "evidence_receipt_v2.json"
    frame_path = directory / "frame_receipt_v2.json"
    trace_path = directory / "raw_rtde_trace.jsonl"
    evidence = _read_json(evidence_path, "V5 no-contact evidence")
    frame = _read_json(frame_path, "V5 no-contact frame receipt")
    readback = verify_named_controller_readback(
        root=Path(root),
        readback_dir=Path(canary_readback_dir),
        program="step6_figure8_no_contact_canary_v1",
    )
    frame_unsigned = {key: item for key, item in frame.items() if key != "frame_sha256"}
    expected_triplet = readback["triplet_sha256"]
    derivation = evidence.get("derivation")
    if (
        evidence.get("schema") != "step6.figure8/no-contact-live-evidence-v1"
        or evidence.get("passed") is not True
        or evidence.get("contact_executed") is not False
        or evidence.get("force_control_executed") is not False
        or evidence.get("campaign_live_acceptance") is not False
        or evidence.get("package_sha256") != expected_triplet
        or frame.get("schema") != "step6.figure8/frozen-home-frame-v1"
        or frame.get("live_canary_passed") is not True
        or tuple(float(item) for item in frame.get("home_pose", ()))
        != tuple(float(item) for item in expected_home_pose)
        or frame.get("home_calibration_receipt_sha256")
        != expected_home_receipt_sha256
        or frame.get("source_package_sha256") != expected_triplet
        or frame.get("frame_sha256") != _sha(frame_unsigned)
        or not isinstance(derivation, Mapping)
        or derivation.get("raw_trace_sha256") != _file_sha(trace_path)
        or frame.get("source_raw_trace_sha256") != _file_sha(trace_path)
    ):
        raise V5CapabilityAcceptanceError(
            "V5 no-contact canary identity, Home, trace, or no-contact claim differs"
        )
    return {
        "directory": str(directory),
        "evidence_path": str(evidence_path),
        "evidence_sha256": _file_sha(evidence_path),
        "frame_path": str(frame_path),
        "frame_sha256": _file_sha(frame_path),
        "raw_trace_path": str(trace_path),
        "raw_trace_sha256": _file_sha(trace_path),
        "controller_readback": readback,
        "passed": True,
        "contact_executed": False,
        "force_control_executed": False,
    }


def capability_anchor_candidate() -> CompleteCandidateV1:
    """Return the frozen, conservative V4/R013 warm-start center."""

    return CompleteCandidateV1(
        controller_path={
            "force_p_gain": 0.019027313840405524,
            "force_damping": 188.36079701683204,
            "force_i_gain": 0.008610779292198037,
            "i_off": False,
            "normal_filter_tau_s": 0.04375,
            "orientation_ko": 0.05,
            "motion_kp": 2.5226892457611436,
        },
        correction_weights=(0.0,) * 6,
    )


def capability_neighbor_candidate() -> CompleteCandidateV1:
    """Change exactly one bounded coordinate by one sixteenth of an octave."""

    anchor = capability_anchor_candidate()
    controller = dict(anchor.controller_path)
    controller["motion_kp"] = float(controller["motion_kp"]) * 2.0 ** (
        V5_NEIGHBOR_LOG2_MOTION_KP_DELTA
    )
    return CompleteCandidateV1(
        controller_path=controller,
        correction_weights=(0.0,) * 6,
    )


def _fixed_plan(
    *,
    chain_id: str,
    position: int,
    ordinal: int,
    epoch: int,
    candidate: CompleteCandidateV1,
    fingerprint: str,
) -> V5TrialPlan:
    key = candidate.candidate_key
    proposal = ProposalReceiptV2(
        ProposalMethodV2.FIXED_REPLAY,
        "controller_path",
        (),
        key,
        (),
        fingerprint,
        CampaignRoleV2.PRIMARY,
        "one-shot-capability-acceptance-not-campaign",
    )
    trial_id = f"{chain_id}-trial-{position:02d}"
    return V5TrialPlan(
        trial_id,
        trial_id + "-attempt",
        V5AttemptKind.PRIMARY_NOVEL,
        candidate,
        key,
        _candidate_token(key),
        ordinal,
        proposal,
        TrialStageV2.PRIMARY_NOVEL,
        False,
        True,
        epoch,
        ordinal,
        ordinal,
    )


def build_capability_chains(
    *, session_epoch: int, capability_fingerprint: str
) -> dict[str, tuple[V5TrialPlan, ...]]:
    """Materialize the fixed seven-chain acceptance schedule."""

    anchor = capability_anchor_candidate()
    neighbor = capability_neighbor_candidate()
    ordinal = 1
    chains: dict[str, tuple[V5TrialPlan, ...]] = {}
    layouts: tuple[tuple[str, tuple[CompleteCandidateV1, ...]], ...] = (
        (V5_CAPABILITY_CHAIN_IDS[0], (anchor, anchor)),
        (V5_CAPABILITY_CHAIN_IDS[1], (anchor, neighbor)),
        (V5_CAPABILITY_CHAIN_IDS[2], (anchor, anchor)),
        (V5_CAPABILITY_CHAIN_IDS[3], (anchor, anchor)),
        (V5_CAPABILITY_CHAIN_IDS[4], (anchor, anchor)),
        (V5_CAPABILITY_CHAIN_IDS[5], (anchor, anchor)),
        (V5_CAPABILITY_CHAIN_IDS[6], (anchor, neighbor, anchor, neighbor, anchor)),
    )
    for chain_id, candidates in layouts:
        plans = []
        for position, candidate in enumerate(candidates, start=1):
            plans.append(
                _fixed_plan(
                    chain_id=chain_id,
                    position=position,
                    ordinal=ordinal,
                    epoch=session_epoch,
                    candidate=candidate,
                    fingerprint=capability_fingerprint,
                )
            )
            ordinal += 1
        chains[chain_id] = tuple(plans)
    return chains


def _sample_force_peaks(record: Any) -> dict[str, float]:
    rows = record.source_artifact.rows[
        record.trial_slice.sample_start_index : record.trial_slice.sample_end_index
    ]
    return {
        "max_abs_raw_signed_normal_n": max(abs(float(row["normal_load_n"])) for row in rows),
        "max_raw_force_norm_n": max(float(row["force_norm_n"]) for row in rows),
        "max_raw_torque_norm_nm": max(float(row["torque_norm_nm"]) for row in rows),
    }


def _record_summary(record: Any) -> dict[str, Any]:
    if record.eligible is not True or record.closure.all_passed is not True:
        raise V5CapabilityAcceptanceError(
            f"capability record is not gate-closed eligible: {record.trial_id}"
        )
    return {
        "trial_id": record.trial_id,
        "attempt_id": record.attempt_id,
        "record_sha256": record.record_sha256,
        "candidate_identity": record.candidate_identity.as_dict(),
        "formal_mae_n": record.metric_snapshot.formal_mae_n,
        "boundary_mode": record.boundary.mode.value,
        "gate_families": {
            "path": record.closure.path.as_dict(),
            "tail": record.closure.tail.as_dict(),
        },
        "gate_closure_evidence_sha256": (
            record.closure.evidence.evidence_sha256
        ),
        "force_peaks": _sample_force_peaks(record),
    }


def _paired_ci90(differences: Sequence[float]) -> tuple[float, float, float]:
    values = tuple(float(item) for item in differences)
    if len(values) != V5_CAPABILITY_PAIRS or not all(
        math.isfinite(item) for item in values
    ):
        raise V5CapabilityAcceptanceError(
            "V5 entry-equivalence requires five finite paired differences"
        )
    mean = math.fsum(values) / len(values)
    variance = math.fsum((item - mean) ** 2 for item in values) / (
        len(values) - 1
    )
    half_width = V5_T_CRITICAL_DF4_90 * math.sqrt(variance / len(values))
    return mean, mean - half_width, mean + half_width


def evaluate_capability_results(
    results: Sequence[V5DurableChainResultV1],
) -> dict[str, Any]:
    """Cold-evaluate the fixed chain set without issuing any live operation."""

    by_id = {result.chain_id: result for result in results}
    if set(by_id) != set(V5_CAPABILITY_CHAIN_IDS):
        raise V5CapabilityAcceptanceError(
            "V5 capability durable chain set is incomplete or unexpected"
        )
    summaries = {
        chain_id: [_record_summary(record) for record in by_id[chain_id].records]
        for chain_id in V5_CAPABILITY_CHAIN_IDS
    }
    same = by_id[V5_CAPABILITY_CHAIN_IDS[0]]
    if (
        len(same.records) != 2
        or same.plans[0].candidate_key != same.plans[1].candidate_key
        or same.records[0].boundary.mode is not BoundaryMode.CONTACT_ROLLOVER
        or same.records[1].boundary.mode is not BoundaryMode.HOME
        or same.activation_receipt.get("rollover_count") != 1
        or same.activation_receipt.get("complete") is not True
    ):
        raise V5CapabilityAcceptanceError("same-candidate rollover evidence differs")

    neighbor = by_id[V5_CAPABILITY_CHAIN_IDS[1]]
    first_controller = neighbor.plans[0].candidate.controller_path
    next_controller = neighbor.plans[1].candidate.controller_path
    changed = tuple(
        key
        for key in first_controller
        if first_controller[key] != next_controller.get(key)
    )
    neighbor_delta = math.log2(
        float(next_controller["motion_kp"]) / float(first_controller["motion_kp"])
    )
    if (
        len(neighbor.records) != 2
        or changed != ("motion_kp",)
        or not math.isclose(
            neighbor_delta,
            V5_NEIGHBOR_LOG2_MOTION_KP_DELTA,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or neighbor.records[0].boundary.mode is not BoundaryMode.CONTACT_ROLLOVER
        or neighbor.records[1].boundary.mode is not BoundaryMode.HOME
        or neighbor.activation_receipt.get("rollover_count") != 1
        or neighbor.activation_receipt.get("complete") is not True
    ):
        raise V5CapabilityAcceptanceError("bounded-neighbor rollover evidence differs")

    pair_ids = (
        V5_CAPABILITY_CHAIN_IDS[0],
        *V5_CAPABILITY_CHAIN_IDS[2:6],
    )
    paired_rows = []
    differences = []
    for pair_index, chain_id in enumerate(pair_ids, start=1):
        result = by_id[chain_id]
        home_entry, rollover_entry = result.records
        if result.plans[0].candidate_key != result.plans[1].candidate_key:
            raise V5CapabilityAcceptanceError(
                "entry-equivalence pair changed candidate"
            )
        difference = (
            rollover_entry.metric_snapshot.formal_mae_n
            - home_entry.metric_snapshot.formal_mae_n
        )
        differences.append(difference)
        paired_rows.append(
            {
                "pair": pair_index,
                "chain_id": chain_id,
                "home_entry_formal_mae_n": home_entry.metric_snapshot.formal_mae_n,
                "rollover_entry_formal_mae_n": rollover_entry.metric_snapshot.formal_mae_n,
                "rollover_minus_home_formal_mae_n": difference,
                "home_entry_record_sha256": home_entry.record_sha256,
                "rollover_entry_record_sha256": rollover_entry.record_sha256,
                "force_peak_comparison": {
                    "home_entry": _sample_force_peaks(home_entry),
                    "rollover_entry": _sample_force_peaks(rollover_entry),
                    "telemetry_only_no_new_peak_threshold": True,
                },
            }
        )
    mean, lower, upper = _paired_ci90(differences)
    equivalence_passed = (
        lower >= -V5_CAPABILITY_MARGIN_N and upper <= V5_CAPABILITY_MARGIN_N
    )
    if not equivalence_passed:
        variance = math.fsum((value - mean) ** 2 for value in differences) / (
            len(differences) - 1
        )
        pilot_std = math.sqrt(variance)
        ci90_pairs = max(
            2,
            math.ceil(
                (V5_Z_90_TWO_SIDED * pilot_std / V5_CAPABILITY_MARGIN_N) ** 2
            ),
        )
        # For a paired TOST with alpha=.05 per one-sided test and 80% power,
        # the planning approximation is (z_.95 + z_.80)^2 sigma^2 / delta^2.
        # This is deliberately reported as an estimate, not used to alter the
        # frozen 80-pair cap or the equivalence margin.
        estimated_pairs = max(
            2,
            math.ceil(
                (
                    (V5_Z_95_ONE_SIDED + V5_Z_POWER_80)
                    * pilot_std
                    / V5_CAPABILITY_MARGIN_N
                )
                ** 2
            ),
        )
        raise V5CapabilityMeasurementResolutionError(
            "V5 Home/rollover formal-MAE 90% CI is outside +/-0.05 N: "
            f"[{lower:.9f},{upper:.9f}]",
            mean_n=mean,
            ci90_n=(lower, upper),
            differences_n=differences,
            estimated_pairs_required=estimated_pairs,
            ci90_approx_pairs_required=ci90_pairs,
            pilot_std_n=pilot_std,
        )

    four = by_id[V5_CAPABILITY_CHAIN_IDS[6]]
    if (
        len(four.records) != 5
        or any(
            record.boundary.mode is not BoundaryMode.CONTACT_ROLLOVER
            for record in four.records[:-1]
        )
        or four.records[-1].boundary.mode is not BoundaryMode.HOME
        or four.activation_receipt.get("rollover_count") != 4
        or four.activation_receipt.get("activation_state_count") != 16
        or four.activation_receipt.get("complete") is not True
    ):
        raise V5CapabilityAcceptanceError("four-rollover chain evidence differs")

    return {
        "same_candidate_single_rollover": {
            "passed": True,
            "chain_id": same.chain_id,
            "records": summaries[same.chain_id],
        },
        "bounded_neighbor_single_rollover": {
            "passed": True,
            "chain_id": neighbor.chain_id,
            "changed_coordinate": "motion_kp",
            "log2_delta": neighbor_delta,
            "records": summaries[neighbor.chain_id],
        },
        "home_vs_rollover_five_paired_entries": {
            "passed": True,
            "signal": "sealed_filtered_normal_n_formal_mae",
            "difference_order": "rollover_entry_minus_home_entry",
            "pair_count": V5_CAPABILITY_PAIRS,
            "confidence_interval": V5_CAPABILITY_CI,
            "student_t_degrees_of_freedom": 4,
            "mean_difference_n": mean,
            "ci90_n": [lower, upper],
            "equivalence_margin_n": V5_CAPABILITY_MARGIN_N,
            "pairs": paired_rows,
        },
        "four_rollover_chain": {
            "passed": True,
            "chain_id": four.chain_id,
            "rollover_count": 4,
            "terminal_boundary": "HOME",
            "records": summaries[four.chain_id],
        },
        "existing_gate_closures_all_passed": True,
        "force_peak_comparison_role": "telemetry_only_existing_hard_safety_remains_authoritative",
        "narrow_force_windows_blocking": False,
        "campaign_qualification": False,
        "epoch_qualification": False,
        "tell_exact_calls": 0,
        "campaign_budget_consumed": 0,
    }


@dataclass(frozen=True)
class V5CapabilityAcceptanceInputsV1:
    root: Path
    state_root: Path
    controller_readback_dir: Path
    canary_readback_dir: Path
    canary_dir: Path
    home_calibration_receipt: Path
    no_motion_recipe_receipt: Path
    robot_host: str
    kunwei_host: str
    kunwei_port: int
    release_identity_sha256: str
    source_identity: Mapping[str, Any]
    controller_triplet_sha256: Mapping[str, str]
    compatibility_fingerprint: FigureEightCampaignFingerprintV1

    def __post_init__(self) -> None:
        for path, role in (
            (self.root, "repository root"),
            (self.controller_readback_dir, "main read-back"),
            (self.canary_readback_dir, "canary read-back"),
            (self.canary_dir, "no-contact canary"),
            (self.home_calibration_receipt, "Home calibration receipt"),
            (self.no_motion_recipe_receipt, "no-motion recipe receipt"),
        ):
            if not Path(path).resolve().exists():
                raise V5CapabilityAcceptanceError(
                    f"V5 capability {role} is unavailable"
                )
        _require_sha(self.release_identity_sha256, "release identity")
        _require_sha(self.source_identity.get("source_sha256"), "source identity")
        if set(self.controller_triplet_sha256) != {"script", "txt", "urp"}:
            raise V5CapabilityAcceptanceError(
                "V5 capability main controller triplet is incomplete"
            )
        if not isinstance(
            self.compatibility_fingerprint, FigureEightCampaignFingerprintV1
        ):
            raise TypeError("V5 capability compatibility fingerprint must be typed")
        if not self.robot_host or not self.kunwei_host or int(self.kunwei_port) <= 0:
            raise V5CapabilityAcceptanceError("V5 capability endpoints are invalid")


class V5CapabilityAcceptanceV1:
    """Execute or cold-resume exactly one release-bound acceptance sequence."""

    def __init__(
        self,
        inputs: V5CapabilityAcceptanceInputsV1,
        *,
        prepare_live: Callable[..., Mapping[str, Any]] = prepare_figure8_live_run,
        context_builder: Callable[..., Any] = build_v5_live_context,
        owner_factory: Callable[[Any], Any] = V5SingleWriterOwnerV1,
        stop_resident: Callable[..., Mapping[str, Any]] = _stop_exact_resident,
    ) -> None:
        if not isinstance(inputs, V5CapabilityAcceptanceInputsV1):
            raise TypeError("V5 capability inputs must be typed")
        self.inputs = inputs
        self.root = Path(inputs.root).resolve()
        self.state_root = Path(inputs.state_root).resolve()
        self.prepare_live = prepare_live
        self.context_builder = context_builder
        self.owner_factory = owner_factory
        self.stop_resident = stop_resident
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
        identity_body = {
            "schema": "step6.autotune/autotuner-v5-capability-identity-v1",
            "version": V5_CAPABILITY_VERSION,
            "release_identity_sha256": inputs.release_identity_sha256,
            "source_identity_sha256": inputs.source_identity["source_sha256"],
            "main_controller_triplet_sha256": dict(
                inputs.controller_triplet_sha256
            ),
            "no_motion_recipe_receipt_sha256": self.no_motion["receipt_sha256"],
            "no_contact_evidence_sha256": self.canary["evidence_sha256"],
            "no_contact_frame_sha256": self.canary["frame_sha256"],
            "no_contact_raw_trace_sha256": self.canary["raw_trace_sha256"],
            "home_geometry_calibration_receipt_sha256": home["receipt_sha256"],
            "pair_count": V5_CAPABILITY_PAIRS,
            "confidence_interval": V5_CAPABILITY_CI,
            "equivalence_margin_n": V5_CAPABILITY_MARGIN_N,
            "maximum_rollovers_per_chain": 4,
            "performance_force_windows_blocking": False,
            "campaign_qualification": False,
        }
        self.capability_fingerprint = _sha(identity_body)
        self.identity = {
            **identity_body,
            "capability_fingerprint_sha256": self.capability_fingerprint,
        }
        self._ensure_identity()

    @property
    def receipt_path(self) -> Path:
        return self.state_root / "capability_receipt.json"

    @property
    def phase_path(self) -> Path:
        return self.state_root / "phase_runtime.json"

    def _ensure_identity(self) -> None:
        marker = self.state_root / "capability_identity.json"
        if marker.is_file():
            if _read_json(marker, "V5 capability identity") != self.identity:
                raise V5CapabilityAcceptanceError(
                    "V5 capability state root identity changed"
                )
            return
        if self.state_root.exists() and any(self.state_root.iterdir()):
            raise V5CapabilityAcceptanceError(
                "V5 capability state root is non-fresh without an identity marker"
            )
        self.state_root.mkdir(parents=True, exist_ok=True)
        _atomic_json(marker, self.identity)

    def _journal(self) -> V5ExecutionJournalV1:
        return V5ExecutionJournalV1(
            self.state_root / "execution.jsonl",
            campaign_fingerprint=self.capability_fingerprint,
            release_identity_sha256=self.inputs.release_identity_sha256,
            role=LedgerRole.PRIMARY,
        )

    def _prepare_or_reuse(self) -> tuple[Path, int]:
        if self.phase_path.is_file():
            phase = _read_json(self.phase_path, "V5 capability phase")
            if (
                phase.get("status") in {"resident_ready", "owner_open"}
                and phase.get("capability_fingerprint_sha256")
                == self.capability_fingerprint
            ):
                run_dir = Path(str(phase["run_dir"])).resolve()
                ready = _read_json(
                    run_dir / "r013_live_owner_ready.json",
                    "V5 capability resident READY",
                )
                return run_dir, int(ready["session_epoch"])
        live_root = self.state_root / "live"
        live_root.mkdir(parents=True, exist_ok=True)
        serial = 1
        while (live_root / f"resident-{serial:03d}").exists():
            serial += 1
        run_dir = live_root / f"resident-{serial:03d}"
        phase = {
            "schema": "step6.autotune/autotuner-v5-capability-phase-v1",
            "version": V5_CAPABILITY_VERSION,
            "status": "preparing",
            "capability_fingerprint_sha256": self.capability_fingerprint,
            "run_dir": str(run_dir),
        }
        _atomic_json(self.phase_path, phase)
        nonce = time.time_ns()
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
            campaign_id="autotuner-v5-capability-acceptance",
            run_id=f"autotuner-v5-capability-{nonce}",
            attempt_id=f"r006-v5-capability-{nonce}",
        )
        ready = _read_json(
            run_dir / "r013_live_owner_ready.json",
            "V5 capability resident READY",
        )
        epoch = int(ready["session_epoch"])
        binding = {
            "schema": V5_LIVE_BINDING_SCHEMA,
            "version": V5_LIVE_OWNER_VERSION,
            "campaign_fingerprint": self.capability_fingerprint,
            "release_identity_sha256": self.inputs.release_identity_sha256,
            "role": LedgerRole.PRIMARY.value,
            "session_epoch": epoch,
            "run_dir": str(run_dir.resolve()),
            "compatibility_fingerprint_sha256": ready[
                "figure8_campaign_fingerprint_sha256"
            ],
        }
        _atomic_json(
            run_dir / "v5_live_binding.json",
            {**binding, "binding_sha256": canonical_sha256(binding)},
        )
        _atomic_json(
            self.phase_path,
            {
                **phase,
                "status": "resident_ready",
                "session_epoch": epoch,
                "v5_live_binding": str(run_dir / "v5_live_binding.json"),
            },
        )
        return run_dir, epoch

    def _release_resident(self) -> dict[str, Any]:
        phase = _read_json(self.phase_path, "V5 capability phase")
        epoch = int(phase.get("session_epoch", 0))
        if epoch <= 0:
            raise V5CapabilityAcceptanceError(
                "V5 capability release lacks a resident session epoch"
            )
        release_path = (
            self.state_root
            / "writer-release-receipts"
            / f"session-{epoch}.json"
        )
        if release_path.is_file():
            cold = _read_json(release_path, "V5 capability writer release")
            if (
                cold.get("home_verified") is True
                and cold.get("stopped") is True
                and cold.get("writer_released") is True
                and cold.get("run_dir") == phase.get("run_dir")
                and cold.get("session_epoch") == phase.get("session_epoch")
            ):
                if phase.get("status") != "released_stopped_home":
                    _atomic_json(
                        self.phase_path,
                        {
                            **phase,
                            "status": "released_stopped_home",
                            "writer_release_receipt": str(release_path),
                        },
                    )
                return cold
            raise V5CapabilityAcceptanceError(
                "V5 capability session release receipt differs"
            )
        raw = dict(
            self.stop_resident(
                robot_host=self.inputs.robot_host,
                expected_home_pose=self.inputs.compatibility_fingerprint.home_pose,
            )
        )
        release = {
            **raw,
            "home_verified": raw.get("passed") is True,
            "stopped": raw.get("passed") is True,
            "run_dir": phase.get("run_dir"),
            "session_epoch": phase.get("session_epoch"),
        }
        _atomic_json(release_path, release, replace=False)
        if (
            release.get("home_verified") is not True
            or release.get("stopped") is not True
            or release.get("writer_released") is not True
        ):
            raise V5CapabilityAcceptanceError(
                "V5 capability did not finish Home/STOPPED/writer-released"
            )
        _atomic_json(
            self.phase_path,
            {
                **phase,
                "status": "released_stopped_home",
                "writer_release_receipt": str(release_path),
            },
        )
        return release

    def _receipt_body(
        self, journal: V5ExecutionJournalV1, release: Mapping[str, Any]
    ) -> dict[str, Any]:
        evaluation = evaluate_capability_results(journal.results)
        return {
            "schema": V5_CAPABILITY_SCHEMA,
            "version": V5_CAPABILITY_VERSION,
            "status": "PASSED",
            "capability_fingerprint_sha256": self.capability_fingerprint,
            "release_identity_sha256": self.inputs.release_identity_sha256,
            "identity": self.identity,
            "no_motion_recipe": self.no_motion,
            "no_contact_protocol": self.canary,
            "contact_capabilities": evaluation,
            "execution_journal_path": str(journal.path),
            "execution_journal_head_sha256": journal.head_sha256,
            "durable_chain_count": len(journal.results),
            "ambiguous_chain_ids": list(journal.ambiguous_chain_ids),
            "writer_release": dict(release),
            "verified_home": True,
            "stopped": True,
            "writer_released": True,
            "campaign_qualification": False,
            "epoch_qualification": False,
            "three_contact_qualification": False,
            "tell_exact_calls": 0,
            "campaign_budget_consumed": 0,
            "narrow_force_windows_blocking": False,
            "automatic_promotion": False,
        }

    def verify_receipt(self) -> dict[str, Any]:
        receipt = _read_json(self.receipt_path, "V5 capability receipt")
        journal = self._journal()
        journal.cold_verify()
        if journal.ambiguous_chain_ids:
            raise V5CapabilityAcceptanceError(
                "V5 capability receipt has ambiguous physical dispatch"
            )
        phase = _read_json(self.phase_path, "V5 capability phase")
        release_path = phase.get("writer_release_receipt")
        if not isinstance(release_path, str):
            raise V5CapabilityAcceptanceError(
                "V5 capability phase lacks a writer release receipt"
            )
        release = _read_json(Path(release_path), "V5 capability writer release")
        body = self._receipt_body(journal, release)
        expected = {**body, "receipt_sha256": canonical_sha256(body)}
        if receipt != expected:
            raise V5CapabilityAcceptanceError(
                "V5 capability receipt differs from cold durable evidence"
            )
        return receipt

    def run(self) -> dict[str, Any]:
        if self.receipt_path.is_file():
            return self.verify_receipt()
        journal = self._journal()
        journal.recover_sealed_results()
        if journal.ambiguous_chain_ids:
            try:
                self._release_resident()
            except BaseException as release_error:
                raise V5CapabilityAcceptanceError(
                    "ambiguous capability dispatch and resident release failed: "
                    f"chains={','.join(journal.ambiguous_chain_ids)}; "
                    f"release={release_error}"
                ) from release_error
            raise V5AmbiguousPhysicalDispatch(
                "capability motion dispatch has no owner-sealed result; resident "
                "is stopped at Home: " + ",".join(journal.ambiguous_chain_ids)
            )
        completed = {result.chain_id for result in journal.results}
        missing = [
            chain_id for chain_id in V5_CAPABILITY_CHAIN_IDS if chain_id not in completed
        ]
        if missing:
            run_dir, epoch = self._prepare_or_reuse()
            chains = build_capability_chains(
                session_epoch=epoch,
                capability_fingerprint=self.capability_fingerprint,
            )
            context: Any | None = None
            error: BaseException | None = None
            try:
                context = self.context_builder(
                    run_dir=run_dir,
                    controller_host=self.inputs.robot_host,
                    kunwei_host=self.inputs.kunwei_host,
                    kunwei_port=int(self.inputs.kunwei_port),
                    launch_profile=run_dir / "figure8_launch_profile.json",
                    expected_campaign_fingerprint=self.capability_fingerprint,
                    expected_release_identity_sha256=self.inputs.release_identity_sha256,
                    expected_role=LedgerRole.PRIMARY,
                )
                phase = _read_json(self.phase_path, "V5 capability phase")
                _atomic_json(self.phase_path, {**phase, "status": "owner_open"})
                owner = self.owner_factory(context)
                for chain_id in missing:
                    plans = chains[chain_id]
                    durable_path = journal.append_dispatch(chain_id, plans)
                    request = V5LiveChainRequestV1(
                        chain_id,
                        plans,
                        self.capability_fingerprint,
                        self.inputs.release_identity_sha256,
                        LedgerRole.PRIMARY,
                        str(durable_path),
                    )
                    sealed = owner.execute_chain(request)
                    journal.append_result(plans, sealed)
                    phase = _read_json(self.phase_path, "V5 capability phase")
                    _atomic_json(
                        self.phase_path,
                        {
                            **phase,
                            "status": "owner_open",
                            "completed_chain_ids": [
                                result.chain_id for result in journal.results
                            ],
                            "execution_journal_head_sha256": journal.head_sha256,
                        },
                    )
            except BaseException as exc:
                error = exc
            close_error: BaseException | None = None
            if context is not None:
                try:
                    context.close()
                except BaseException as exc:
                    close_error = exc
            release_error: BaseException | None = None
            try:
                self._release_resident()
            except BaseException as exc:
                release_error = exc
            if error is not None:
                if close_error is not None or release_error is not None:
                    raise V5CapabilityAcceptanceError(
                        "V5 capability failed and cleanup also failed: "
                        f"run={error}; close={close_error}; release={release_error}"
                    ) from error
                raise error
            if close_error is not None:
                raise close_error
            if release_error is not None:
                raise release_error
        release = self._release_resident()
        journal.cold_verify()
        if journal.ambiguous_chain_ids:
            raise V5CapabilityAcceptanceError(
                "V5 capability ended with an ambiguous physical dispatch"
            )
        try:
            body = self._receipt_body(journal, release)
        except V5CapabilityMeasurementResolutionError as exc:
            block_body = {
                **exc.as_dict(),
                "capability_fingerprint_sha256": self.capability_fingerprint,
                "release_identity_sha256": self.inputs.release_identity_sha256,
                "source_identity_sha256": self.inputs.source_identity["source_sha256"],
                "execution_journal_path": str(journal.path),
                "execution_journal_head_sha256": journal.head_sha256,
                "writer_release": dict(release),
                "primary_started": False,
                "correction_started": False,
            }
            _atomic_json(
                self.state_root / "measurement_resolution_block.json",
                {**block_body, "receipt_sha256": canonical_sha256(block_body)},
            )
            raise
        receipt = {**body, "receipt_sha256": canonical_sha256(body)}
        _atomic_json(self.receipt_path, receipt)
        return self.verify_receipt()


__all__ = [
    "V5_CAPABILITY_CHAIN_IDS",
    "V5_CAPABILITY_CI",
    "V5_CAPABILITY_MARGIN_N",
    "V5_CAPABILITY_PAIRS",
    "V5_CAPABILITY_SCHEMA",
    "V5CapabilityAcceptanceError",
    "V5CapabilityMeasurementResolutionError",
    "V5CapabilityAcceptanceInputsV1",
    "V5CapabilityAcceptanceV1",
    "build_capability_chains",
    "capability_anchor_candidate",
    "capability_neighbor_candidate",
    "evaluate_capability_results",
    "execute_no_motion_recipe_probe",
    "verify_named_controller_readback",
    "verify_no_contact_canary",
    "verify_no_motion_recipe_receipt",
]
