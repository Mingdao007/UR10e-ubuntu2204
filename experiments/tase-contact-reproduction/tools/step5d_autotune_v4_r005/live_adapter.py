"""Owner-gated r005 composition over the mature r004 transport stack.

The adapter owns the r005 route, ledger, queue, objective, and unbounded host
loop.  The injected writer is the only object allowed to own a transport.  In
production the owner supplies the already verified mature writer factory after
all receipt checks; constructing this module remains side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from step5d_autotune_v4_r004.live_campaign import LiveCampaignRunner
from step5d_autotune_v4_r004.campaign import Candidate as R004Candidate
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from step5d_autotune_v4_r004.identity import SessionIdentityGate
from step5d_autotune_v4_r004.identity import (
    ControllerReadbackReceipt,
    RuntimeIdentityEvidence,
    Script1StartReceipt,
)
from step5d_autotune_v4_r004.prerequisites import (
    load_controller_receipt,
    load_runtime_evidence,
    load_script1_receipt,
)
from step5d_autotune_v4_r004.session import SessionPhase
from step5d_autotune_v4_r004.timing import TimingEvidence, TimingError
from step5d_autotune_v4_r004.transport import (
    LiveR004KunweiTransport,
    LiveR004RTDETransport,
)
from step5d_autotune_v4_r004_live_writer import LIVE_ACK as R004_LIVE_ACK
from step5d_autotune_v4_r004_live_writer import LiveR004Writer
from step5d_autotune_v4_r004.evidence import AttemptEvidence, PathSample, QualificationEvidence
from step5d_autotune_v4_r004.wire import AttemptKind as R004AttemptKind
from step5d_eoat_profiles import load_new_eoat_profile

from step5d_force_objective import (
    MAX_SOURCE_AGE_S,
    ForceObjectiveBuilder,
    ForcePathSample,
)

from .contracts import PROGRAM, R005Contract, load_contract
from .alignment import JointVelocityPacket, align_qdot_actual_qd
from .observations import ObservationLedger
from .optimizer import V4BoAdapter
from .queue import QueuePort
from .runtime import Attempt, AttemptResult, HostLoop, RuntimePort
from .tp import CONTROLLER_DIR, RUNTIME_PROTOCOL as R005_RUNTIME_PROTOCOL


R005_LIVE_ACK = "RUN_LIVE_R005_WITH_EXPLICIT_ACK"
LIVE_ADAPTER_SCHEMA = "step5d.autotune-v4/r005-live-adapter-v2"
# Receipt timestamps are provenance/order evidence here; optimizer attestation
# TTL and live source freshness remain enforced by their own gates.
INT32_MAX = 2**31 - 1
BOUNDED_MOTION_METRICS_VERSION = "r005-bounded-motion-diagnostics-v1"
_MOTION_DIAGNOSTIC_FIELDS = (
    "path_duration_s",
    "path_observed_span_s",
    "path_physical_span_s",
    "path_coverage_interval_s",
    "path_cadence_hz",
    "path_phase",
    "xy_error_p95_m",
    "xy_error_max_m",
    "endpoint_error_max_m",
    "velocity_error_p95_m_s",
    "qd_correlation",
    "qd_joint_correlations",
    "qd_lag_s",
)
_BOUNDED_FORCE_OBJECTIVE_FIELDS = (
    "schema",
    "version",
    "target_force_n",
    "formal_window_s",
    "legacy_shadow_version",
    "legacy_window_s",
    "bin_width_s",
    "required_bins",
    "complete_bins",
    "legacy_complete_bins",
    "coverage_bitmap",
    "sample_identity_digest",
    "sample_identity_count",
    "sample_count",
    "deduplicated_replays",
    "force_mae_v2",
    "v2_mae_n",
    "r004_legacy_shadow",
    "legacy_mae_n",
    "delta_n",
    "trainable",
    "provenance",
    "semantic_fingerprint",
    "receipt_version",
    "raw_evidence_digest",
    "sufficient_statistics_digest",
    "builder_seal_sha256",
)


class R005LiveAdapterError(RuntimeError):
    """The r005 live boundary is incomplete or not fail-closed."""


def _bounded_numeric(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R005LiveAdapterError(f"{role} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise R005LiveAdapterError(f"{role} must be finite")
    return numeric


def _bounded_attempt_diagnostics(raw: AttemptEvidence) -> dict[str, Any]:
    """Project only scalar/small typed diagnostics into the r005 ledger."""

    source_metrics = raw.metrics if isinstance(raw.metrics, Mapping) else {}
    projected: dict[str, Any] = {
        "motion_metrics_version": BOUNDED_MOTION_METRICS_VERSION,
        "path_sample_count": int(raw.path_samples),
        "motion_gate_passed": bool(raw.motion_gate_passed),
        "timing_gate_passed": bool(raw.timing_gate_passed),
    }
    for field in _MOTION_DIAGNOSTIC_FIELDS:
        value = getattr(raw, field, None)
        if value is None:
            value = source_metrics.get(field)
        if value is None:
            continue
        if field == "path_phase":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
                raise R005LiveAdapterError("AttemptEvidence path phase is not bounded")
            projected[field] = value
        elif field == "qd_joint_correlations":
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 6
                or any(
                    not isinstance(item, (int, float))
                    or isinstance(item, bool)
                    or not math.isfinite(float(item))
                    or not -1.0 <= float(item) <= 1.0
                    for item in value
                )
            ):
                raise R005LiveAdapterError(
                    "AttemptEvidence joint qd correlations are not a bounded six-vector"
                )
            projected[field] = [float(item) for item in value]
        else:
            numeric = _bounded_numeric(value, f"AttemptEvidence {field}")
            if field == "qd_correlation" and not -1.0 <= numeric <= 1.0:
                raise R005LiveAdapterError("AttemptEvidence qd correlation is outside [-1,1]")
            projected[field] = numeric

    timing = raw.timing_evidence
    if timing is None:
        raw_timing = source_metrics.get("timing_evidence")
        if isinstance(raw_timing, TimingEvidence):
            timing = raw_timing
        elif isinstance(raw_timing, Mapping):
            try:
                timing = TimingEvidence.from_mapping(raw_timing)
            except TimingError as exc:
                raise R005LiveAdapterError("AttemptEvidence timing evidence is invalid") from exc
    if timing is not None:
        projected["timing_evidence"] = timing.as_dict()
    return projected


def _bounded_force_objective(objective: Any) -> dict[str, Any]:
    """Keep objective provenance without copying formal bins or sample IDs."""

    if objective is None:
        return {"present": False}
    summary: dict[str, Any] = {"present": True}
    for field in _BOUNDED_FORCE_OBJECTIVE_FIELDS:
        if not hasattr(objective, field):
            continue
        value = getattr(objective, field)
        if isinstance(value, tuple):
            value = list(value)
        if isinstance(value, list):
            if len(value) > 4:
                raise R005LiveAdapterError(
                    f"bounded force objective field {field} is unexpectedly large"
                )
            value = list(value)
        if isinstance(value, (str, bool, int, float)) or value is None or isinstance(value, list):
            summary[field] = value
        else:
            raise R005LiveAdapterError(
                f"bounded force objective field {field} has an unsupported type"
            )
    return summary


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R005LiveAdapterError(f"{role} must be a lowercase SHA-256")
    return value


def _regular_json(path: Path, role: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R005LiveAdapterError(f"{role} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R005LiveAdapterError(f"{role} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise R005LiveAdapterError(f"{role} must be an object")
    return value


def _observed_at_seconds(row: Mapping[str, Any], role: str) -> float:
    """Read a receipt timestamp without assigning it a freshness policy."""

    observed = row.get("observed_at_s", row.get("observed_at_unix_s"))
    if observed is None and row.get("observed_at_unix_ns") is not None:
        try:
            observed = float(row["observed_at_unix_ns"]) / 1_000_000_000.0
        except (TypeError, ValueError, OverflowError):
            observed = None
    try:
        value = float(observed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise R005LiveAdapterError(f"{role} timestamp is invalid") from exc
    if not math.isfinite(value):
        raise R005LiveAdapterError(f"{role} timestamp is nonfinite")
    return value


def _validate_not_from_future(observed_at_s: float, now_s: float, role: str) -> None:
    if observed_at_s > now_s:
        raise R005LiveAdapterError(f"{role} timestamp is from the future")


def _validate_controller_receipt_content(
    receipt: ControllerReadbackReceipt,
    *,
    contract: Any,
    expected_triplet: Mapping[str, str],
) -> None:
    """Keep r004 content/EOAT/readback identity while omitting only receipt age."""

    script2 = contract.raw.get("script2", {})
    expected_target = (
        script2.get("controller_target")
        if isinstance(script2, Mapping)
        else None
    ) or f"{CONTROLLER_DIR}/{contract.raw['program']}.urp"
    expected_eoat = getattr(contract, "eoat_sha256", None) or contract.raw[
        "invariants"
    ]["eoat_profile"]["sha256"]
    if (
        receipt.program != contract.raw["program"]
        or receipt.controller_target != expected_target
    ):
        raise R005LiveAdapterError("controller readback program target differs")
    if dict(expected_triplet) != {
        "script": receipt.script_sha256,
        "txt": receipt.txt_sha256,
        "urp": receipt.urp_sha256,
    }:
        raise R005LiveAdapterError("controller readback triplet differs")
    if receipt.eoat_identity_sha256 != expected_eoat:
        raise R005LiveAdapterError("controller readback EOAT identity differs")
    if receipt.safety_mode != "NORMAL" or not receipt.stationary:
        raise R005LiveAdapterError("controller readback is not stationary Safety NORMAL")


def _validate_script1_receipt_content(
    receipt: Script1StartReceipt,
    *,
    expected_script_sha256: str,
    expected_eoat_sha256: str,
) -> None:
    """Keep Script1 content/Home/EOAT identity while omitting only receipt age."""

    if receipt.script_sha256 != expected_script_sha256:
        raise R005LiveAdapterError("Script 1 receipt SHA differs")
    if receipt.eoat_identity_sha256 != expected_eoat_sha256:
        raise R005LiveAdapterError("Script 1 V4 EOAT identity differs")
    if not receipt.stationary or receipt.safety_mode != "NORMAL":
        raise R005LiveAdapterError("Script 1 receipt stationary/Safety contract differs")


def _r004_parent_contract(contract: R005Contract) -> Any:
    """Resolve the mature control policy only through r005's bound parent."""

    from step5d_autotune_v4_r004.contracts import load_contract as load_r004_contract

    relative = "config/step5d/autotune_v4_r004.json"
    parent_path = contract.path.parent / "autotune_v4_r004.json"
    expected = contract.r004_parent_sha256.get(relative)
    if expected is None or _sha256(parent_path) != expected:
        raise R005LiveAdapterError("r005 Script1 parent contract binding differs")
    return load_r004_contract(parent_path)


def _script1_hashes(contract: R005Contract) -> Mapping[str, str]:
    """Resolve Script1 only through r005's content-addressed r004 parent."""

    return _r004_parent_contract(contract).script1_sha256


@dataclass(frozen=True)
class R005LiveInputs:
    """All read-only admission inputs required before transport construction."""

    controller_receipt: Path
    script1_receipt: Path
    runtime_evidence: Path
    runtime_attestation: Path
    optimizer_pointer: Path
    launch_profile_path: Path
    release_manifest_sha256: str
    baseline_ledger: Path
    software_baseline_receipt: Path
    ledger_path: Path
    queue_root: Path
    authority_root: Path
    controller_host: str
    kunwei_host: str
    kunwei_port: int
    route_id: str
    attempt_id: str
    resident_session_id: str
    session_epoch: int
    expected_triplet: Mapping[str, str]
    eoat_sha256: str
    campaign_fingerprint: str
    contract_sha256: str
    now_s: float | None = None

    def validate(self, *, contract: R005Contract) -> None:
        if self.contract_sha256 != contract.sha256:
            raise R005LiveAdapterError("r005 contract digest differs")
        if self.campaign_fingerprint != contract.campaign_fingerprint:
            raise R005LiveAdapterError("r005 campaign fingerprint differs")
        _digest(self.eoat_sha256, "r005 EOAT identity")
        expected_eoat = contract.raw["invariants"]["eoat_profile"]["sha256"]
        if self.eoat_sha256 != expected_eoat:
            raise R005LiveAdapterError("r005 EOAT identity differs from the release contract")
        if "r004" in self.route_id.lower() or "r004" in self.attempt_id.lower():
            raise R005LiveAdapterError("r005 live identity cannot use an r004 route/attempt")
        if "r005" not in self.route_id.lower() or "r005" not in self.attempt_id.lower():
            raise R005LiveAdapterError("r005 route and attempt identities are required")
        if (
            isinstance(self.session_epoch, bool)
            or not isinstance(self.session_epoch, int)
            or not 1 <= self.session_epoch <= INT32_MAX
            or not self.resident_session_id
        ):
            raise R005LiveAdapterError(
                "r005 resident epoch must be a positive TP/RTDE INT32"
            )
        if not self.controller_host or not self.kunwei_host or self.kunwei_port <= 0:
            raise R005LiveAdapterError("r005 transport endpoints are incomplete")
        if self.authority_root.is_symlink() or not self.authority_root.is_dir():
            raise R005LiveAdapterError("r005 authority root must already be a directory")
        if self.queue_root.is_symlink() or not self.queue_root.is_dir():
            raise R005LiveAdapterError("r005 queue root must already be a directory")

        controller = _regular_json(self.controller_receipt, "r005 controller receipt")
        script1 = _regular_json(self.script1_receipt, "r005 Script1 receipt")
        runtime = _regular_json(self.runtime_evidence, "r005 runtime evidence")
        attestation = _regular_json(self.runtime_attestation, "r005 runtime attestation")
        pointer = _regular_json(self.optimizer_pointer, "canonical V3 optimizer pointer")
        launch_profile = _regular_json(self.launch_profile_path, "r005 V3 launch profile")
        baseline = _regular_json(self.baseline_ledger, "r005 baseline ledger")
        software_baseline = _regular_json(
            self.software_baseline_receipt, "r005 software baseline receipt"
        )
        script1_hashes = _script1_hashes(contract)
        controller_receipt = load_controller_receipt(self.controller_receipt)
        script1_receipt = load_script1_receipt(self.script1_receipt)
        _validate_controller_receipt_content(
            controller_receipt,
            contract=contract,
            expected_triplet=self.expected_triplet,
        )
        _validate_script1_receipt_content(
            script1_receipt,
            expected_script_sha256=script1_hashes["script"],
            expected_eoat_sha256=expected_eoat,
        )
        if controller_receipt.route_id != self.route_id:
            raise R005LiveAdapterError("r005 controller receipt route differs")
        _digest(self.release_manifest_sha256, "r005 release manifest digest")
        if launch_profile.get("tp_program_id") != PROGRAM:
            raise R005LiveAdapterError("r005 launch profile program identity differs")
        if launch_profile.get("schema") != "step5d.autotune-v3/launch-profile-v1":
            raise R005LiveAdapterError("r005 launch profile schema differs")
        if baseline.get("campaign_fingerprint") != contract.campaign_fingerprint:
            raise R005LiveAdapterError("r005 baseline campaign fingerprint differs")
        if script1.get("script_sha256") != script1_hashes["script"]:
            raise R005LiveAdapterError("r005 Script1 receipt differs from its frozen parent binding")
        if software_baseline.get("schema") != "step5d.autotune-v4/r005-software-baseline-v1":
            raise R005LiveAdapterError("r005 software baseline schema differs")
        if software_baseline.get("zero_tare_config_write") is not False:
            raise R005LiveAdapterError("r005 software baseline must not write zero/tare configuration")
        if software_baseline.get("parse_errors") != 0 or software_baseline.get("dropped_bytes") != 0:
            raise R005LiveAdapterError("r005 software baseline transport evidence is not clean")
        sample_count = software_baseline.get("sample_count")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 900:
            raise R005LiveAdapterError("r005 software baseline has too few physical frames")
        self.software_baseline_n()
        if self.ledger_path.exists():
            if self.ledger_path.is_symlink() or not self.ledger_path.is_file():
                raise R005LiveAdapterError("r005 observation ledger is not a regular file")
        else:
            raise R005LiveAdapterError("r005 observation ledger must exist before live startup")

        now = float(time.time() if self.now_s is None else self.now_s)
        if not math.isfinite(now):
            raise R005LiveAdapterError("r005 admission timestamp is nonfinite")
        receipt_rows = (
            ("controller receipt", controller),
            ("Script1 receipt", script1),
            ("runtime evidence", runtime),
            ("software baseline receipt", software_baseline),
        )
        receipt_observed_at_s = {
            role: _observed_at_seconds(row, role) for role, row in receipt_rows
        }
        for role, observed_at_s in receipt_observed_at_s.items():
            _validate_not_from_future(observed_at_s, now, role)
        if not (
            receipt_observed_at_s["Script1 receipt"]
            <= receipt_observed_at_s["software baseline receipt"]
            <= receipt_observed_at_s["runtime evidence"]
        ):
            raise R005LiveAdapterError(
                "r005 managed prepare ordering requires "
                "Script1 <= software baseline <= runtime evidence"
            )
        for role, row in (("controller receipt", controller), ("Script1 receipt", script1)):
            if role == "controller receipt" and row.get("program") != PROGRAM:
                raise R005LiveAdapterError(f"{role} program identity differs")
            if row.get("safety_mode") != "NORMAL":
                raise R005LiveAdapterError(f"{role} Safety mode is not NORMAL")
            if row.get("stationary") is not True:
                raise R005LiveAdapterError(f"{role} is not stationary")
        if runtime.get("program") != PROGRAM:
            raise R005LiveAdapterError("r005 runtime program identity differs")
        if runtime.get("session_epoch") not in {None, self.session_epoch}:
            raise R005LiveAdapterError("r005 runtime epoch differs")
        if runtime.get("resident_session_id") not in {None, self.resident_session_id}:
            raise R005LiveAdapterError("r005 resident session identity differs")
        if runtime.get("program_running") is not True or runtime.get("uninterrupted") is not True:
            raise R005LiveAdapterError("r005 resident runtime is not uninterrupted and running")
        expected_runtime_hi, expected_runtime_lo = runtime_identity_limbs(
            PROGRAM,
            contract.sha256,
            contract.campaign_fingerprint,
        )
        for role, row in (("controller receipt", controller), ("runtime evidence", runtime)):
            if (
                row.get("runtime_protocol") != R005_RUNTIME_PROTOCOL
                or row.get("runtime_digest_hi") != expected_runtime_hi
                or row.get("runtime_digest_lo") != expected_runtime_lo
            ):
                raise R005LiveAdapterError(f"{role} r005 runtime identity differs")
        if attestation.get("profile") not in {None, "optimizer"}:
            raise R005LiveAdapterError("optimizer attestation profile differs")
        if pointer.get("schema") is None or not isinstance(pointer.get("profiles"), Mapping):
            raise R005LiveAdapterError("canonical V3 optimizer pointer is incomplete")
        try:
            from step5d_autotune_v3.runtime_installation import current_pointer_path
            from step5d_optimizer_runtime import resolve_optimizer_runtime

            canonical_pointer_path = current_pointer_path()
            if Path(self.optimizer_pointer).resolve() != canonical_pointer_path.resolve():
                raise R005LiveAdapterError("optimizer pointer is not the canonical V3 pointer")
            resolved = resolve_optimizer_runtime()
            attestation_observed_ns = attestation.get("observed_at_unix_ns")
            if isinstance(attestation_observed_ns, bool):
                raise R005LiveAdapterError("optimizer attestation timestamp is invalid")
            try:
                attestation_observed_s = float(attestation_observed_ns) / 1_000_000_000.0
                attestation_age_s = now - attestation_observed_s
            except (TypeError, ValueError, OverflowError) as exc:
                raise R005LiveAdapterError("optimizer attestation timestamp is invalid") from exc
            if (
                not math.isfinite(attestation_age_s)
                or attestation_age_s < 0.0
                or attestation_age_s > resolved.declaration.stale_after_s
            ):
                raise R005LiveAdapterError("optimizer attestation is stale")
            if dict(pointer) != dict(resolved.pointer):
                raise R005LiveAdapterError("optimizer pointer content differs from resolved V3 identity")
            attestation_path = Path(str(resolved.pointer["attestation_path"]))
            if Path(self.runtime_attestation).resolve() != attestation_path.resolve():
                raise R005LiveAdapterError("runtime attestation is not the canonical V3 attestation")
            if _sha256(Path(self.runtime_attestation)) != resolved.pointer["attestation_sha256"]:
                raise R005LiveAdapterError("runtime attestation digest differs from the canonical V3 pointer")
            if dict(attestation) != dict(resolved.attestation):
                raise R005LiveAdapterError("runtime attestation content differs from canonical V3 identity")
        except R005LiveAdapterError:
            raise
        except Exception as exc:
            raise R005LiveAdapterError(f"canonical optimizer runtime is unavailable: {exc}") from exc
        if not isinstance(self.expected_triplet, Mapping) or set(self.expected_triplet) != {"script", "txt", "urp"}:
            raise R005LiveAdapterError("exact r005 script/txt/urp triplet is required")
        for role, path_or_digest in self.expected_triplet.items():
            _digest(path_or_digest, f"expected r005 {role} digest")

    def software_baseline_n(self) -> tuple[float, float, float, float, float, float]:
        row = _regular_json(self.software_baseline_receipt, "r005 software baseline receipt")
        raw = row.get("mean_wrench_n_nm")
        if not isinstance(raw, list) or len(raw) != 6:
            raise R005LiveAdapterError("r005 software baseline must contain six values")
        try:
            values = tuple(float(value) for value in raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise R005LiveAdapterError("r005 software baseline values are invalid") from exc
        if not all(math.isfinite(value) for value in values):
            raise R005LiveAdapterError("r005 software baseline values must be finite")
        return values  # type: ignore[return-value]

    def expected_triplet_matches(self, paths: Mapping[str, Path]) -> bool:
        """Bind the admitted digest triplet to the exact local artifact files."""

        if set(paths) != {"script", "txt", "urp"}:
            return False
        for role, path in paths.items():
            candidate = Path(path)
            if candidate.is_symlink() or not candidate.is_file():
                return False
            try:
                if _sha256(candidate) != self.expected_triplet[role]:
                    return False
            except OSError:
                return False
        return True


@dataclass(frozen=True)
class _R005MatureIdentityContract:
    """The small identity view consumed by the mature r004 primitives."""

    path: Path
    sha256: str
    campaign_fingerprint: str
    eoat_sha256: str
    script1_sha256: Mapping[str, str]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class _R005MaturePrerequisites:
    """r005 receipt bundle presented through the r004 writer seam."""

    contract: _R005MatureIdentityContract
    controller: ControllerReadbackReceipt
    script1: Script1StartReceipt
    runtime: RuntimeIdentityEvidence
    expected_triplet: Mapping[str, str]
    route_id: str
    session_epoch: int
    resident_session_id: str
    input_baseline_ledger_sha256: str

    def validate(self, *, now_s: float) -> None:
        if self.controller.route_id != self.route_id:
            raise R005LiveAdapterError("r005 controller route differs from the owner route")
        _validate_not_from_future(
            self.controller.observed_at_s,
            now_s,
            "controller receipt",
        )
        _validate_not_from_future(
            self.script1.observed_at_s,
            now_s,
            "Script1 receipt",
        )
        _validate_controller_receipt_content(
            self.controller,
            contract=self.contract,
            expected_triplet=self.expected_triplet,
        )
        profile = load_new_eoat_profile()
        self.controller.validate_eoat_readback(
            payload_kg=profile.payload_kg,
            payload_cog_m=profile.cog_m,
            tcp_offset_m_rad=profile.controller_tcp_m_rad,
        )
        _validate_script1_receipt_content(
            self.script1,
            expected_script_sha256=self.contract.script1_sha256["script"],
            expected_eoat_sha256=self.contract.eoat_sha256,
        )
        expected_hi, expected_lo = runtime_identity_limbs(
            self.contract.raw["program"],
            self.contract.sha256,
            self.contract.campaign_fingerprint,
        )
        if (
            self.controller.runtime_protocol != R005_RUNTIME_PROTOCOL
            or self.controller.runtime_digest_hi != expected_hi
            or self.controller.runtime_digest_lo != expected_lo
            or self.runtime.program != self.contract.raw["program"]
            or self.runtime.script_sha256 != self.controller.script_sha256
            or self.runtime.runtime_protocol != R005_RUNTIME_PROTOCOL
            or self.runtime.runtime_digest_hi != expected_hi
            or self.runtime.runtime_digest_lo != expected_lo
            or self.runtime.session_epoch != self.session_epoch
            or self.runtime.resident_session_id != self.resident_session_id
            or not self.runtime.program_running
            or not self.runtime.uninterrupted
        ):
            raise R005LiveAdapterError("r005 resident runtime identity is not the same uninterrupted session")
        if (
            self.controller.safety_mode != "NORMAL"
            or not self.controller.stationary
            or self.runtime.observed_at_s <= self.controller.observed_at_s
        ):
            raise R005LiveAdapterError("r005 entry requires fresh stationary Safety NORMAL evidence")


class _R005SessionIdentityGate(SessionIdentityGate):
    """Use r005 content identity while leaving r004 receipt-age policy untouched."""

    def begin_play(
        self,
        *,
        controller_receipt: ControllerReadbackReceipt,
        script1_receipt: Script1StartReceipt,
        now_s: float,
        epoch: int,
        session_id: str,
        expected_triplet: Mapping[str, str],
    ) -> None:
        if self._active:
            raise R005LiveAdapterError("resident session already active")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0 or not session_id:
            raise R005LiveAdapterError("new session epoch/id is invalid")
        if controller_receipt.receipt_sha256 in self._used_controller_receipts:
            raise R005LiveAdapterError("static controller readback receipt reused across sessions")
        _validate_controller_receipt_content(
            controller_receipt,
            contract=self.contract,
            expected_triplet=expected_triplet,
        )
        _validate_script1_receipt_content(
            script1_receipt,
            expected_script_sha256=self.contract.script1_sha256["script"],
            expected_eoat_sha256=self.contract.eoat_sha256,
        )
        self._active = True
        self._epoch = epoch
        self._session_id = session_id
        self._expected = controller_receipt
        self._invalidated_reason = ""
        self._last_runtime_observed_at_s = float(now_s)
        self._used_controller_receipts.add(controller_receipt.receipt_sha256)


@dataclass(frozen=True)
class _R005MatureAttempt:
    """Duck-typed attempt surface required by the mature r004 control loop."""

    ordinal: int
    kind: R004AttemptKind
    candidate: R004Candidate


class R005MatureWriter:
    """Adapt the verified r004 writer while retaining the r005 identity plane."""

    offline_test_mode = False

    def __init__(self, writer: LiveR004Writer) -> None:
        self.writer = writer
        self._attempt: Attempt | None = None
        self._ticket: Any = None

    @property
    def _path_sample_sink(self) -> Callable[[PathSample], None] | None:
        return getattr(self.writer, "_path_sample_sink", None)

    @_path_sample_sink.setter
    def _path_sample_sink(self, value: Callable[[PathSample], None] | None) -> None:
        setattr(self.writer, "_path_sample_sink", value)

    @staticmethod
    def _candidate(candidate: Any) -> R004Candidate:
        if not hasattr(candidate, "canonical"):
            raise R005LiveAdapterError("r005 candidate is not typed")
        payload = dict(candidate.canonical)
        payload.pop("i_off", None)
        try:
            return R004Candidate(**payload)
        except Exception as exc:
            raise R005LiveAdapterError("r005 candidate cannot bind to mature controller candidate") from exc

    @staticmethod
    def _kind(kind: str) -> R004AttemptKind:
        # r005 deliberately has its own attempt-kind identity.  The mature
        # stack still needs its wire-level execution family, but indexing the
        # r004 enum by the r005 string would reject every bootstrap and BO
        # attempt (and would silently couple the new campaign to r004 names).
        mapping = {
            "QUALIFICATION": R004AttemptKind.QUALIFICATION,
            "BOOTSTRAP_PD": R004AttemptKind.BATCH_A,
            "BO_TRIAL": R004AttemptKind.BATCH_B,
            "RETEST": R004AttemptKind.RETEST,
        }
        try:
            return mapping[kind]
        except (KeyError, TypeError) as exc:
            raise R005LiveAdapterError(f"r005 attempt kind is not mature-stack compatible: {kind!r}") from exc

    @staticmethod
    def _token(candidate: Any) -> int:
        token = int(str(candidate.candidate_uid)[:8], 16) & 0x7FFFFFFF
        return max(1, token)

    def open(self, *, live_ack: str) -> None:
        if live_ack != R005_LIVE_ACK:
            raise R005LiveAdapterError("r005 live acknowledgement differs")
        self.writer.open(live_ack=R004_LIVE_ACK)

    def close(self) -> None:
        self.writer.close()

    def home(self) -> None:
        if not self.writer.active or self.writer.session.phase is not SessionPhase.READY_HOME_NEXT:
            raise R005LiveAdapterError("r005 mature writer is not verified at Home")

    def sync_qualification_passes(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 3:
            raise R005LiveAdapterError("r005 qualification count is outside 0..3")
        self.writer.set_baseline_state(
            consecutive_successes=count,
            sticky_one_newton_latched=0,
        )

    def dispatch(self, attempt: Attempt, ticket: Any) -> None:
        if self._attempt is not None:
            raise R005LiveAdapterError("r005 mature writer has two dispatched attempts")
        self._attempt = attempt
        self._ticket = ticket

    def arm(self, attempt: Attempt) -> None:
        if self._attempt != attempt or self._ticket is None:
            raise R005LiveAdapterError("r005 ARM does not match the dispatched ticket")
        mature_candidate = self._candidate(attempt.candidate)
        self.writer.candidate = mature_candidate
        self.writer.arm_unbounded(
            ordinal=attempt.attempt_sequence,
            kind=self._kind(attempt.kind),
            candidate_token=self._token(attempt.candidate),
        )

    def run_60s(self, attempt: Attempt) -> AttemptEvidence | QualificationEvidence:
        if self._attempt != attempt:
            raise R005LiveAdapterError("r005 run does not match the dispatched ticket")
        mature_attempt = _R005MatureAttempt(
            ordinal=attempt.attempt_sequence,
            kind=self._kind(attempt.kind),
            candidate=self._candidate(attempt.candidate),
        )
        try:
            return self.writer.execute_attempt(
                mature_attempt,
                timeout_s=180.0,
                allow_safe_nontrainable=True,
            )
        finally:
            self._attempt = None
            self._ticket = None

    def safe_return(self, attempt: Attempt, result: AttemptResult) -> AttemptResult:
        del attempt
        if self.writer.session.phase is not SessionPhase.READY_HOME_NEXT:
            raise R005LiveAdapterError("mature writer did not verify safe return Home")
        return result

    def revoke_authority(self, reason: str) -> None:
        self.writer.stop(reason)


def build_verified_mature_r005_writer(
    inputs: R005LiveInputs,
    *,
    contract: R005Contract | None = None,
    path_sample_sink: Callable[[PathSample], None] | None = None,
) -> R005MatureWriter:
    """Construct the mature writer after r005 admission, without opening transport."""

    active = contract or load_contract()
    mature_parent = _r004_parent_contract(active)
    script1_hashes = mature_parent.script1_sha256
    controller = load_controller_receipt(inputs.controller_receipt)
    script1 = load_script1_receipt(inputs.script1_receipt)
    runtime = load_runtime_evidence(inputs.runtime_evidence)
    mature_raw = dict(mature_parent.raw)
    mature_script2 = dict(mature_raw.get("script2", {}))
    mature_script2["controller_target"] = (
        "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v4_r005.urp"
    )
    mature_raw.update(program=PROGRAM, script2=mature_script2)
    identity_contract = _R005MatureIdentityContract(
        path=active.path,
        sha256=active.sha256,
        campaign_fingerprint=active.campaign_fingerprint,
        eoat_sha256=inputs.eoat_sha256,
        script1_sha256=script1_hashes,
        raw=mature_raw,
    )
    prerequisites = _R005MaturePrerequisites(
        contract=identity_contract,
        controller=controller,
        script1=script1,
        runtime=runtime,
        expected_triplet=dict(inputs.expected_triplet),
        route_id=inputs.route_id,
        session_epoch=inputs.session_epoch,
        resident_session_id=inputs.resident_session_id,
        input_baseline_ledger_sha256=_sha256(inputs.baseline_ledger),
    )
    writer = LiveR004Writer(
        prerequisites,  # type: ignore[arg-type]
        authority_root=inputs.authority_root,
        route_id=inputs.route_id,
        attempt_id=inputs.attempt_id,
        controller_host=inputs.controller_host,
        kunwei_host=inputs.kunwei_host,
        kunwei_port=inputs.kunwei_port,
        software_baseline_n=inputs.software_baseline_n(),
        identity_namespace="r005",
        runtime_protocol=R005_RUNTIME_PROTOCOL,
        canonical_runtime_only=True,
        path_sample_sink=path_sample_sink,
    )
    # ResidentSession is the mature state machine, but its shared identity
    # gate owns r004 receipt TTLs.  Replace only that gate at the r005 seam;
    # all content, EOAT, route, session, and runtime checks remain local here.
    writer.session.identity = _R005SessionIdentityGate(identity_contract)
    return R005MatureWriter(writer)


class R005WriterPort(Protocol):
    """The single mature writer owned by the production route."""

    def open(self, *, live_ack: str) -> None: ...

    def close(self) -> None: ...

    def home(self) -> None: ...

    def dispatch(self, attempt: Attempt, ticket: Any) -> None: ...

    def arm(self, attempt: Attempt) -> None: ...

    def run_60s(self, attempt: Attempt) -> Any: ...

    def safe_return(self, attempt: Attempt, result: AttemptResult) -> AttemptResult: ...

    def revoke_authority(self, reason: str) -> None: ...


class R005LiveRuntimePort:
    """RuntimePort that delegates every physical operation to one writer."""

    def __init__(self, writer: R005WriterPort) -> None:
        self.writer = writer
        self.writer_calls = 0

    def home(self) -> None:
        self.writer.home()

    def sync_qualification_passes(self, count: int) -> None:
        method = getattr(self.writer, "sync_qualification_passes", None)
        if method is None:
            raise R005LiveAdapterError("mature writer lacks qualification state synchronization")
        method(count)

    def dispatch(self, attempt: Attempt, ticket: Any) -> None:
        self.writer.dispatch(attempt, ticket)

    def arm(self, attempt: Attempt) -> None:
        self.writer.arm(attempt)

    def run_60s(self, attempt: Attempt) -> AttemptResult:
        result = self.writer.run_60s(attempt)
        if not isinstance(result, AttemptResult):
            raise R005LiveAdapterError("production writer did not return a typed AttemptResult")
        return result

    def safe_return(self, attempt: Attempt, result: AttemptResult) -> AttemptResult:
        returned = self.writer.safe_return(attempt, result)
        if not isinstance(returned, AttemptResult):
            raise R005LiveAdapterError("production safe return did not return a typed AttemptResult")
        return returned

    def revoke_authority(self, reason: str) -> None:
        self.writer.revoke_authority(reason)


@dataclass(frozen=True)
class R005LiveTransportStack:
    """Factories inherited from the mature r004 controller/RTDE stack."""

    rtde_factory: Callable[..., LiveR004RTDETransport]
    kunwei_factory: Callable[..., LiveR004KunweiTransport]
    controller_writer_type: type[LiveR004Writer]
    campaign_runner_type: type[LiveCampaignRunner]


class R005LiveWriterAdapter:
    """Bind r005 identity and raw force evidence to one injected writer."""

    def __init__(self, writer: R005WriterPort, *, contract: R005Contract) -> None:
        if writer is None:
            raise R005LiveAdapterError("r005 requires one verified writer")
        self.writer = writer
        self.contract = contract
        self.open_count = 0
        self._objective_builder = ForceObjectiveBuilder()
        self._path_samples: list[ForcePathSample] = []
        self._last_joint_evidence = None
        self._last_rtde_frame_identity: float | int | None = None
        self._rtde_sequence = 0

    def observe_r004_path_sample(self, sample: PathSample) -> None:
        if sample.path_time_s is None or sample.path_phase is None:
            return
        if sample.qdot is None or sample.actual_qd is None:
            raise R005LiveAdapterError("raw PATH qdot/actual_qd evidence is incomplete")
        source_sequences = dict(sample.source_sequences)
        raw_rtde_identity = source_sequences.get("rtde")
        if (
            isinstance(raw_rtde_identity, bool)
            or not isinstance(raw_rtde_identity, (int, float))
            or not math.isfinite(float(raw_rtde_identity))
        ):
            raise R005LiveAdapterError("raw PATH RTDE frame identity is invalid")
        if (
            self._last_rtde_frame_identity is not None
            and float(raw_rtde_identity) < float(self._last_rtde_frame_identity)
        ):
            raise R005LiveAdapterError("raw PATH RTDE frame identity regressed")
        if self._last_rtde_frame_identity != raw_rtde_identity:
            self._rtde_sequence += 1
            self._last_rtde_frame_identity = raw_rtde_identity
        source_sequences["rtde"] = self._rtde_sequence
        source_ages_s = dict(sample.source_ages_s)
        for source in sorted(source_ages_s, key=str):
            age = source_ages_s[source]
            if (
                isinstance(age, bool)
                or not isinstance(age, (int, float))
                or not math.isfinite(float(age))
                or float(age) < 0.0
                or float(age) > MAX_SOURCE_AGE_S
            ):
                diagnostic = {
                    "adapter_last_rtde_identity": self._last_rtde_frame_identity,
                    "adapter_normalized_rtde_sequence": self._rtde_sequence,
                    "limit": MAX_SOURCE_AGE_S,
                    "observed_at_s": sample.observed_at_s,
                    "offending_numeric_age": (
                        None
                        if isinstance(age, bool) or not isinstance(age, (int, float))
                        else float(age)
                    ),
                    "offending_source": source,
                    "path_time_s": sample.path_time_s,
                    "raw_rtde_identity": raw_rtde_identity,
                    "source_ages_s": source_ages_s,
                    "source_sequences": dict(sample.source_sequences),
                }
                raise R005LiveAdapterError(
                    "r005 raw PATH source age invariant violation: "
                    + json.dumps(
                        diagnostic,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
        converted = ForcePathSample(
            path_time_s=sample.path_time_s,
            path_phase=25,
            filtered_normal_n=sample.filtered_normal_n,
            source_sequences=source_sequences,
            source_ages_s=source_ages_s,
            commanded_qdot=tuple(sample.qdot),
            actual_qd=tuple(sample.actual_qd),
            timestamp_s=sample.observed_at_s,
        )
        # The qdot stored by the mature writer is indexed by the packet echo
        # consumed by this exact RTDE frame, not by the newer packet published
        # later in the same host iteration.
        packet_sequence = source_sequences.get("tp")
        rtde_sequence = source_sequences.get("rtde")
        if isinstance(packet_sequence, int) and isinstance(rtde_sequence, int):
            self._last_joint_evidence = align_qdot_actual_qd(
                JointVelocityPacket(
                    packet_sequence=packet_sequence,
                    rtde_sequence=rtde_sequence,
                    timestamp_s=sample.observed_at_s,
                    values=tuple(sample.qdot),
                ),
                JointVelocityPacket(
                    packet_sequence=packet_sequence,
                    rtde_sequence=rtde_sequence,
                    timestamp_s=sample.observed_at_s,
                    values=tuple(sample.actual_qd),
                ),
            )
        self._objective_builder.add(converted)
        self._path_samples.append(converted)

    def open(self, *, live_ack: str) -> None:
        if live_ack != R005_LIVE_ACK:
            raise R005LiveAdapterError("r005 live acknowledgement differs")
        if self.open_count:
            raise R005LiveAdapterError("r005 writer was opened more than once")
        self.writer.open(live_ack=R005_LIVE_ACK)
        self.open_count = 1

    def close(self) -> None:
        self.writer.close()

    def sync_qualification_passes(self, count: int) -> None:
        method = getattr(self.writer, "sync_qualification_passes", None)
        if method is None:
            raise R005LiveAdapterError("verified writer lacks qualification state synchronization")
        method(count)

    def run_60s(self, attempt: Attempt) -> AttemptResult:
        self._objective_builder = ForceObjectiveBuilder()
        self._path_samples.clear()
        self._last_joint_evidence = None
        self._last_rtde_frame_identity = None
        self._rtde_sequence = 0
        raw = self.writer.run_60s(attempt)
        if isinstance(raw, AttemptResult):
            if (
                raw.force_objective is not None
                and raw.force_objective.provenance != "raw_path_evidence"
                and not getattr(self.writer, "offline_test_mode", False)
            ):
                raise R005LiveAdapterError("production AttemptResult used a synthetic objective provenance")
            if (
                raw.force_objective is not None
                and raw.force_objective.trainable
                and not self._path_samples
                and not getattr(self.writer, "offline_test_mode", False)
            ):
                raise R005LiveAdapterError("trainable production objective was not bound to raw PATH samples")
            if (
                raw.kind != "QUALIFICATION"
                and raw.force_objective is not None
                and not raw.raw_path_samples
                and not getattr(self.writer, "offline_test_mode", False)
            ):
                raise R005LiveAdapterError(
                    "production objective was returned without durable raw PATH samples"
                )
            return raw
        if not isinstance(raw, (AttemptEvidence, QualificationEvidence)):
            raise R005LiveAdapterError("mature writer returned an unknown evidence type")
        if isinstance(raw, QualificationEvidence):
            return AttemptResult(
                epoch=attempt.epoch,
                attempt_sequence=attempt.attempt_sequence,
                kind=attempt.kind,
                candidate=attempt.candidate,
                safe_return=raw.return_gate_passed,
                binding_ok=True,
                safety_gate=raw.safety_gate_passed,
                contact_gate=raw.contact_gate_passed,
                return_gate=raw.return_gate_passed,
                motion_gate=False,
                timing_gate=raw.timing_gate_passed,
                identity_gate=True,
                qualification_passed=raw.qualification_passed,
                duration_s=0.0,
                metrics={"mature_evidence_sha256": raw.evidence_sha256},
                execution_id=attempt.execution_id,
            )
        objective = self._objective_builder.finalize()
        binding = None if self._last_joint_evidence is None else self._last_joint_evidence.as_dict()
        metrics = {
            "mature_evidence_sha256": raw.evidence_sha256,
            "force_objective": _bounded_force_objective(objective),
            "raw_path_sample_count": len(self._path_samples),
        }
        metrics.update(_bounded_attempt_diagnostics(raw))
        if binding is not None:
            metrics["joint_velocity_binding"] = binding
        return AttemptResult(
            epoch=attempt.epoch,
            attempt_sequence=attempt.attempt_sequence,
            kind=attempt.kind,
            candidate=attempt.candidate,
            safe_return=raw.return_gate_passed,
            binding_ok=binding is not None,
            safety_gate=raw.safety_gate_passed,
            contact_gate=raw.contact_gate_passed,
            return_gate=raw.return_gate_passed,
            motion_gate=raw.motion_gate_passed,
            timing_gate=raw.timing_gate_passed,
            identity_gate=True,
            qualification_passed=False,
            duration_s=float(raw.path_duration_s or 0.0),
            # The live adapter passes the complete builder output and the
            # immutable raw samples through to the ledger.  `trainable` is
            # deliberately false until the ledger's fresh subprocess seam
            # verifies this exact artifact.
            force_objective=objective,
            alignment_ok=binding is not None,
            metrics=metrics,
            execution_id=attempt.execution_id,
            joint_evidence=self._last_joint_evidence,
            raw_path_samples=tuple(self._path_samples),
        )

    def safe_return(self, attempt: Attempt, result: AttemptResult) -> AttemptResult:
        method = getattr(self.writer, "safe_return", None)
        if method is None:
            # LiveR004Writer's execute_attempt already proves its fixed-Home
            # return before yielding AttemptEvidence.  A writer without that
            # proof cannot pass this compatibility seam.
            if result.safe_return and result.return_gate:
                return result
            raise R005LiveAdapterError("mature writer has no verified safe-return method")
        returned = method(attempt, result)
        if not isinstance(returned, AttemptResult):
            raise R005LiveAdapterError("safe-return writer returned an untyped result")
        return returned

    def __getattr__(self, name: str) -> Any:
        return getattr(self.writer, name)


class R005LiveAdapter:
    """Actual production-shaped composition point for the r005 host loop."""

    def __init__(
        self,
        *,
        contract: R005Contract | None = None,
        transport_stack: R005LiveTransportStack | None = None,
        writer_factory: Callable[[R005LiveInputs, Callable[[PathSample], None]], R005WriterPort] | None = None,
        host_loop_factory: Callable[..., HostLoop] | None = None,
    ) -> None:
        self.contract = contract or load_contract()
        self.transport_stack = transport_stack or R005LiveTransportStack(
            rtde_factory=LiveR004RTDETransport,
            kunwei_factory=LiveR004KunweiTransport,
            controller_writer_type=LiveR004Writer,
            campaign_runner_type=LiveCampaignRunner,
        )
        self.writer_factory = writer_factory
        self.host_loop_factory = host_loop_factory
        self._writer: R005LiveWriterAdapter | None = None
        self.last_stop_reason: str | None = None
        self.last_events: tuple[str, ...] = ()

    @property
    def production_stop_after_ordinal(self) -> bool:
        return False

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "schema": LIVE_ADAPTER_SCHEMA,
            "program": PROGRAM,
            "adapter": "r005_host_loop_over_verified_r004_live_stack",
            "composition": "prerequisites->r005-ledger->v3-queue->v4-bo->one-runtime-port->host-loop",
            "rtde_transport": self.transport_stack.rtde_factory.__name__,
            "kunwei_transport": self.transport_stack.kunwei_factory.__name__,
            "controller_writer": self.transport_stack.controller_writer_type.__name__,
            "campaign_runner": self.transport_stack.campaign_runner_type.__name__,
            "production_live_stop_after_ordinal": False,
            "dashboard_actions": False,
            "load": False,
            "play": False,
            "live_evidence": False,
        }

    def adapt_verified_writer(self, writer: R005WriterPort) -> R005LiveWriterAdapter:
        adapted = R005LiveWriterAdapter(writer, contract=self.contract)
        # The mature r004 writer exposes this additive, default-None seam.  It
        # lets r005 consume the exact raw PATH packets without copying the
        # controller loop or its motion/safety algorithms.
        sink_owner = getattr(writer, "writer", writer)
        if hasattr(sink_owner, "_path_sample_sink"):
            setattr(sink_owner, "_path_sample_sink", adapted.observe_r004_path_sample)
        else:
            raise R005LiveAdapterError("mature writer has no raw PATH evidence seam")
        self._writer = adapted
        return adapted

    def build_host_loop(
        self,
        *,
        queue: QueuePort,
        ledger: ObservationLedger,
        optimizer: V4BoAdapter,
        writer: R005WriterPort,
    ) -> HostLoop:
        adapted = self.adapt_verified_writer(writer)
        selected_loop = self.host_loop_factory or HostLoop
        return selected_loop(
            contract=self.contract,
            queue=queue,
            ledger=ledger,
            optimizer=optimizer,
            runtime=R005LiveRuntimePort(adapted),
        )

    def run_forever(
        self,
        *,
        inputs: R005LiveInputs,
        queue: QueuePort,
        ledger: ObservationLedger,
        optimizer: V4BoAdapter,
        writer: R005WriterPort | None = None,
    ) -> str:
        # All validation must complete before writer construction/opening.
        inputs.validate(contract=self.contract)
        selected = writer
        if selected is None and self.writer_factory is not None:
            selected = self.writer_factory(inputs, lambda sample: None)
        if selected is None:
            raise R005LiveAdapterError(
                "owner must provide the verified mature r004 writer factory; no transport was opened"
            )
        loop = self.build_host_loop(
            queue=queue,
            ledger=ledger,
            optimizer=optimizer,
            writer=selected,
        )
        adapted = self._writer
        if adapted is None:
            raise R005LiveAdapterError("r005 writer composition was not retained")
        adapted.open(live_ack=R005_LIVE_ACK)
        try:
            while not loop.terminal:
                loop.run_one()
            self.last_stop_reason = loop.stop_reason
            self.last_events = tuple(loop.events)
            return loop.status
        finally:
            self.last_stop_reason = loop.stop_reason
            self.last_events = tuple(loop.events)
            adapted.close()

    def build_transport_pair(
        self,
        *,
        controller_host: str,
        kunwei_host: str,
        kunwei_port: int,
    ) -> tuple[LiveR004RTDETransport, LiveR004KunweiTransport]:
        if not controller_host or not kunwei_host or kunwei_port <= 0:
            raise R005LiveAdapterError("r005 transport endpoints are incomplete")
        return (
            self.transport_stack.rtde_factory(controller_host),
            self.transport_stack.kunwei_factory(kunwei_host, port=kunwei_port),
        )


__all__ = [
    "LIVE_ADAPTER_SCHEMA",
    "R005LiveAdapter",
    "R005LiveAdapterError",
    "R005LiveInputs",
    "R005LiveRuntimePort",
    "R005LiveTransportStack",
    "R005LiveWriterAdapter",
    "R005MatureWriter",
    "R005_LIVE_ACK",
    "build_verified_mature_r005_writer",
]
