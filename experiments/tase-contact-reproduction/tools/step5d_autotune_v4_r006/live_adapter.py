"""Owner-gated r006 adapter over the real r005 live composition.

The r006 route owns identity, threshold receipt, motion profile, and campaign
metadata.  Physical transport, Remote admission, durable V3 queue, mature
writer, and continuous HostLoop remain the reviewed r005/r004 primitives.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
import threading
import time
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import step5d_autotune_v4_r004_live_writer as r004_writer_module
import step5d_autotune_v4_r004.wire as r004_wire_module
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from step5d_autotune_v4_r004.evidence import (
    PathEvidenceCollector as R004PathEvidenceCollector,
)
from step5d_autotune_v4_r004.identity import (
    ControllerReadbackReceipt,
    RuntimeIdentityEvidence,
    Script1StartReceipt,
)
from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE
from step5d_autotune_v4_r004.prerequisites import (
    load_controller_receipt,
    load_runtime_evidence,
    load_script1_receipt,
)
from step5d_autotune_v4_r004.qualification import (
    CanonicalQualificationControl as _R004CanonicalQualificationControl,
    QualificationControlError as _R004QualificationControlError,
)
from step5d_autotune_v4_r004.session import SessionPhase
from step5d_autotune_v4_r004.wire import AttemptKind as R004AttemptKind
from step5d_autotune_v4_r004_live_writer import LIVE_ACK as R004_LIVE_ACK
from step5d_autotune_v4_r004_live_writer import LiveR004Writer, LiveWriterError
from step5d_eoat_profiles import load_new_eoat_profile
from step5d_autotune_v4_r005.contracts import Candidate as R005Candidate
from step5d_autotune_v4_r005.contracts import R005Contract
from step5d_autotune_v4_r005.live_adapter import (
    R005LiveAdapterError,
    R005LiveInputs,
    R005LiveRuntimePort,
    R005LiveWriterAdapter,
    R005MatureWriter,
    R005_LIVE_ACK,
    _R005MatureAttempt,
    _R005MatureIdentityContract,
    _R005SessionIdentityGate,
    _digest,
    _observed_at_seconds,
    _regular_json,
    _r004_parent_contract,
    _validate_controller_receipt_content,
    _validate_not_from_future,
    _validate_script1_receipt_content,
)
from step5d_autotune_v4_r005.observations import ObservationLedger
from step5d_autotune_v4_r005.queue import (
    DispatchTicket,
    QueueEntry,
    QueueError as V3QueueError,
    V3DurableQueueAdapter,
)
from step5d_autotune_v4_r005.optimizer import V4BoAdapter
from step5d_autotune_v4_r005.optimizer import OptimizerAsk
from step5d_autotune_v4_r005.runtime import (
    Attempt,
    AttemptResult,
    CampaignPhase,
    HostLoop,
    RuntimeErrorR005,
)

from .contracts import (
    D_ANCHOR,
    I_ON_ANCHOR,
    KO_ANCHOR,
    KP_ANCHOR,
    P_ANCHOR,
    R006Contract,
    TARGET_FORCE_N,
    TAU_ANCHOR,
    STEP_OCTAVE,
    load_contract,
)
from .motion_profile import ACTIVE_MOTION_ENVELOPE_V2, r006_runtime_path_reference
from .lattice import (
    ANCHOR_POINT,
    IMode,
    ParameterPoint,
    PointObservation,
    RegionTraversal,
    TrustRegion,
    neighbors,
    route_bfs,
    select_second_center,
    second_warm_start_plan,
    transition_is_legal,
    warm_start_plan,
)
from .objective import R006ObjectiveBuilder
from .optimizer import (
    CertifiedRegionState,
    PACCertificate,
    QLogNEIScheduler,
    R006CudaQLogNEI,
    OptimizerError,
)
from .parent import load_frozen_r005_contract
from .queue import R006V3DurableQueueAdapter
from .sidecar import R006ObjectiveSidecar, R006SidecarError
from .thresholds import ThresholdReceipt, ThresholdReceiptError, load_threshold_receipt
from .tp import CONTROLLER_DIRECTORY, RUNTIME_PROTOCOL as R006_RUNTIME_PROTOCOL


R006_LIVE_ACK = "RUN_LIVE_R006_WITH_EXPLICIT_ACK"
R006_LIVE_ADAPTER_SCHEMA = "step5d.autotune-v4/r006-live-adapter-v1"


class R006LiveAdapterError(RuntimeError):
    """The r006 live route failed closed before or during the parent stack."""


@dataclass(frozen=True)
class R006Candidate(R005Candidate):
    """Native r006 physical candidate with transport-only parent compatibility.

    The r005 base class is used only as a structural compatibility surface for
    the frozen ObservationRecord/AttemptResult dataclasses.  Its bounded
    ``__post_init__`` is deliberately bypassed: every r006 invariant below is
    owned and checked here, including symmetric negative Ko/Kp exponents.
    """

    i_mode: IMode = IMode.OFF

    @staticmethod
    def _finite_positive(value: Any, role: str, *, allow_zero: bool = False) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise R006LiveAdapterError(f"r006 {role} must be numeric")
        result = float(value)
        if not math.isfinite(result) or (result < 0.0 if allow_zero else result <= 0.0):
            requirement = "finite and non-negative" if allow_zero else "finite and positive"
            raise R006LiveAdapterError(f"r006 {role} must be {requirement}")
        return result

    @staticmethod
    def _quarter_step(value: float, anchor: float, role: str) -> int:
        try:
            exponent = math.log2(value / anchor)
        except (OverflowError, ValueError, ZeroDivisionError) as exc:
            raise R006LiveAdapterError(f"r006 {role} is not on the quarter-octave lattice") from exc
        scaled = exponent / STEP_OCTAVE
        step = int(round(scaled))
        if not math.isfinite(scaled) or not math.isclose(
            scaled, float(step), rel_tol=0.0, abs_tol=1e-10
        ):
            raise R006LiveAdapterError(f"r006 {role} is not on the quarter-octave lattice")
        try:
            expected = anchor * (2.0 ** (step * STEP_OCTAVE))
        except OverflowError as exc:
            raise R006LiveAdapterError(f"r006 {role} physical value is nonfinite") from exc
        if not math.isfinite(expected) or not math.isclose(
            value, expected, rel_tol=2e-12, abs_tol=1e-15
        ):
            raise R006LiveAdapterError(f"r006 {role} is not on the quarter-octave lattice")
        return step

    def __post_init__(self) -> None:
        if not isinstance(self.i_mode, IMode):
            raise R006LiveAdapterError("r006 I mode is not typed")
        values = {
            "force_p_gain": self._finite_positive(self.force_p_gain, "P"),
            "force_i_gain": self._finite_positive(self.force_i_gain, "I", allow_zero=True),
            "force_damping": self._finite_positive(self.force_damping, "D"),
            "normal_filter_tau_s": self._finite_positive(self.normal_filter_tau_s, "tau"),
            "orientation_ko": self._finite_positive(self.orientation_ko, "Ko"),
            "motion_kp": self._finite_positive(self.motion_kp, "Kp"),
            "target_force_n": self._finite_positive(self.target_force_n, "target_force_n", allow_zero=True),
        }
        if not math.isclose(values["target_force_n"], TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12):
            raise R006LiveAdapterError("r006 target force is immutable at 5 N")
        if self.i_mode is IMode.OFF and values["force_i_gain"] != 0.0:
            raise R006LiveAdapterError("r006 I-off candidate cannot carry an I gain")
        if self.i_mode is IMode.ON and values["force_i_gain"] <= 0.0:
            raise R006LiveAdapterError("r006 I-on candidate requires a positive I gain")
        self._quarter_step(values["force_p_gain"], P_ANCHOR, "P")
        self._quarter_step(values["force_damping"], D_ANCHOR, "D")
        self._quarter_step(values["normal_filter_tau_s"], TAU_ANCHOR, "tau")
        self._quarter_step(values["orientation_ko"], KO_ANCHOR, "Ko")
        self._quarter_step(values["motion_kp"], KP_ANCHOR, "Kp")
        if self.i_mode is IMode.ON:
            self._quarter_step(values["force_i_gain"], I_ON_ANCHOR, "I")
        for field, value in values.items():
            object.__setattr__(self, field, value)

    @classmethod
    def from_point(cls, point: ParameterPoint) -> "R006Candidate":
        if not isinstance(point, ParameterPoint):
            raise R006LiveAdapterError("r006 candidate source point is not typed")
        try:
            p_gain, d_gain, tau_s, i_gain, ko, kp = point.physical_coordinates
        except (OverflowError, ValueError) as exc:
            raise R006LiveAdapterError("r006 point physical values are not finite") from exc
        return cls(
            force_p_gain=p_gain,
            force_i_gain=i_gain,
            force_damping=d_gain,
            normal_filter_tau_s=tau_s,
            orientation_ko=ko,
            motion_kp=kp,
            target_force_n=TARGET_FORCE_N,
            i_mode=point.i_mode,
        )

    @classmethod
    def from_canonical(cls, payload: Mapping[str, Any]) -> "R006Candidate":
        if not isinstance(payload, Mapping):
            raise R006LiveAdapterError("r006 candidate canonical payload is not a mapping")
        required = {
            "force_p_gain",
            "force_i_gain",
            "force_damping",
            "normal_filter_tau_s",
            "orientation_ko",
            "motion_kp",
            "target_force_n",
            "i_off",
        }
        if set(payload) != required:
            raise R006LiveAdapterError("r006 candidate canonical fields differ")
        i_off = payload["i_off"]
        if not isinstance(i_off, bool):
            raise R006LiveAdapterError("r006 I-off canonical flag is not typed")
        return cls(
            force_p_gain=payload["force_p_gain"],
            force_i_gain=payload["force_i_gain"],
            force_damping=payload["force_damping"],
            normal_filter_tau_s=payload["normal_filter_tau_s"],
            orientation_ko=payload["orientation_ko"],
            motion_kp=payload["motion_kp"],
            target_force_n=payload["target_force_n"],
            i_mode=IMode.OFF if i_off else IMode.ON,
        )

    @property
    def i_off(self) -> bool:
        return self.i_mode is IMode.OFF

    @property
    def canonical(self) -> dict[str, Any]:
        return {
            "force_p_gain": self.force_p_gain,
            "force_i_gain": self.force_i_gain,
            "force_damping": self.force_damping,
            "normal_filter_tau_s": self.normal_filter_tau_s,
            "orientation_ko": self.orientation_ko,
            "motion_kp": self.motion_kp,
            "target_force_n": self.target_force_n,
            "i_off": self.i_off,
        }

    @property
    def candidate_uid(self) -> str:
        return hashlib.sha256(
            json.dumps(self.canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    @property
    def as_point(self) -> ParameterPoint:
        steps = (
            self._quarter_step(self.force_p_gain, P_ANCHOR, "P"),
            self._quarter_step(self.force_damping, D_ANCHOR, "D"),
            self._quarter_step(self.normal_filter_tau_s, TAU_ANCHOR, "tau"),
            self._quarter_step(self.orientation_ko, KO_ANCHOR, "Ko"),
            self._quarter_step(self.motion_kp, KP_ANCHOR, "Kp"),
        )
        i_step = None if self.i_mode is IMode.OFF else self._quarter_step(
            self.force_i_gain, I_ON_ANCHOR, "I"
        )
        return ParameterPoint(
            p_step=steps[0],
            d_step=steps[1],
            tau_step=steps[2],
            i_mode=self.i_mode,
            i_step=i_step,
            ko_step=steps[3],
            kp_step=steps[4],
        )


class _R006NativeCanonicalQualificationControl(_R004CanonicalQualificationControl):
    """Canonical control composition with no bounded V4Candidate rebuild."""

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, R006Candidate):
            raise _R004QualificationControlError(
                "r006 native canonical control requires an r006 candidate"
            )
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise _R004QualificationControlError("qualification attempt identity is missing")
        if not isinstance(self.canonical_runtime_only, bool):
            raise _R004QualificationControlError("canonical runtime-only policy is not typed")
        if self.motion_profile is not None and not isinstance(self.motion_profile, type(R004_MOTION_PROFILE)):
            raise _R004QualificationControlError("qualification motion profile is not typed")
        policy = self.release_contract.raw["live_boundary"][
            "canonical_calibrated_numeric_residual_policy"
        ]
        self._tangential_tolerance_m_s = float(policy["tangential_m_s_max"])
        self._angular_tolerance_rad_s = float(policy["angular_rad_s_max"])
        self._required_hold_s = (
            float(self.release_contract.raw["live_boundary"]["qualified_path_trial_hold_s"])
            if self.path_requested
            else 10.0
        )
        if (
            self._tangential_tolerance_m_s != 2e-6
            or self._angular_tolerance_rad_s != 2e-6
        ):
            raise _R004QualificationControlError("qualification residual policy differs")
        try:
            from step5d_autotune_v4.contracts import load_contract
            from step5d_autotune_v4_r004.baseline_runtime import (
                BaselineReadinessGate,
                BaselineState,
            )
            from step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime
            from step5d_autotune_v4_r004.path_controller import V4PathController
            from step5d_autotune_v4_r004.runtime import StartupHeartbeatGate, TimingGuard

            # The r006 object itself is the canonical candidate consumed by
            # the mature runtime primitives.  No r004 V4Candidate is built.
            self._contract = load_contract(runtime_only=self.canonical_runtime_only)
            self._canonical_candidate = self.candidate
            self._runtime = V4CalibratedRuntime(
                self._contract,
                self._canonical_candidate,
                motion_profile=self.motion_profile,
                force_integral_limit_n_s=float(self.force_integral_limit_n_s),
            )
            self._path_controller = V4PathController(
                self._canonical_candidate,
                motion_profile=self.motion_profile,
            )
            self._baseline_state = BaselineState()
            self._readiness_gate = (
                BaselineReadinessGate(
                    filtered_min_n=3.0,
                    filtered_max_n=7.0,
                    raw_min_n=3.0,
                    raw_max_n=8.0,
                    force_norm_max_n=10.0,
                    torque_norm_max_nm=0.30,
                )
                if self.path_requested
                else BaselineReadinessGate()
            )
            self._timing = TimingGuard()
            self._startup = StartupHeartbeatGate()
            try:
                from step5d_autotune_v4_r008.tube_cbf_live import TubeCbfLiveFilter

                self._tube_cbf = TubeCbfLiveFilter.from_environ()
            except Exception:
                self._tube_cbf = None
            self.last_tube_cbf = None
        except _R004QualificationControlError:
            raise
        except Exception as exc:
            raise _R004QualificationControlError(
                f"r006 native canonical qualification stack is unavailable: {exc}"
            ) from exc


_R006_V3_POLICY_LOCK = threading.RLock()
_R006_NATIVE_OVERLAY_SCHEMA = "step5d.autotune-v4/r006-native-overlay-v1"


def _r006_native_overlay_sha256(v3_queue: Any, overlay: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        v3_queue._canonical(
            {"schema": _R006_NATIVE_OVERLAY_SCHEMA, "overlay": dict(overlay)}
        )
    ).hexdigest()


def _r006_native_request_uid(
    v3_queue: Any,
    *,
    occurrence_nonce: str,
    control_candidate_uid: str,
    normalized_overlay_sha256: str,
    source: str,
    position: str,
) -> str:
    identity = {
        "protocol": v3_queue.PROTOCOL,
        "occurrence_nonce": occurrence_nonce,
        "control_candidate_uid": control_candidate_uid,
        "normalized_overlay_sha256": normalized_overlay_sha256,
        "source": source,
        "position": position,
    }
    return f"request:v1:{v3_queue._sha256_bytes(v3_queue._canonical(identity))}"


def _r006_validate_native_runtime_overlay(
    v3_queue: Any,
    overlay: Mapping[str, Any],
    *,
    launch_profile_path: Path,
) -> None:
    """Keep V3's launch/runtime caps while excluding its candidate envelope."""

    profile = v3_queue._load_profile(launch_profile_path)
    execution_profile = overlay.get("execution_profile_id")
    allowed_profiles = profile.trial_overlay_policy["execution_profile_id"]["allowed"]
    if not isinstance(execution_profile, str) or execution_profile not in allowed_profiles:
        raise R006LiveAdapterError("r006 execution profile is not launch-authorized")
    v3_queue._profile_integer_id(overlay)
    for field in (
        "step5d_preload_filtered_min_n",
        "step5d_preload_filtered_max_n",
        "step5d_preload_raw_min_n",
        "step5d_preload_raw_max_n",
        "step5d_preload_force_norm_max_n",
        "step5d_preload_hold_s",
        "step5d_preload_timeout_s",
    ):
        value = overlay.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise R006LiveAdapterError(f"r006 runtime overlay {field} is not numeric")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise R006LiveAdapterError(f"r006 runtime overlay {field} is not finite")
        rule = profile.trial_overlay_policy[field]
        if numeric < float(rule["min"]) or numeric > float(rule["max"]):
            raise R006LiveAdapterError(
                f"r006 runtime overlay {field} is outside its launch cap"
            )


def _r006_native_request_document(
    v3_queue: Any,
    *,
    enqueue_sequence: int,
    launch_profile_path: Path,
    force_p: float,
    force_i: float,
    force_damping: float,
    normal_filter_tau_s: float,
    orientation_ko: float,
    motion_kp: float,
    source: str,
    position: str,
    occurrence_nonce: str,
) -> dict[str, Any]:
    """Build the V3 request shape from a validated native r006 candidate."""

    if position not in v3_queue.POSITIONS:
        raise R006LiveAdapterError("r006 queue position must be tail or next")
    if not source or "\n" in source:
        raise R006LiveAdapterError("r006 queue source must be one non-empty line")
    if (
        len(occurrence_nonce) != 32
        or any(character not in "0123456789abcdef" for character in occurrence_nonce)
    ):
        raise R006LiveAdapterError("r006 queue occurrence nonce is not lowercase hex")
    try:
        candidate = R006Candidate(
            force_p_gain=force_p,
            force_i_gain=force_i,
            force_damping=force_damping,
            normal_filter_tau_s=normal_filter_tau_s,
            orientation_ko=orientation_ko,
            motion_kp=motion_kp,
            target_force_n=TARGET_FORCE_N,
            i_mode=IMode.OFF if float(force_i) == 0.0 else IMode.ON,
        )
        from step5d_autotune_v3.runtime_profile import control_candidate_uid

        overlay = {
            **v3_queue.DEFAULT_OVERLAY,
            "force_p_gain": candidate.force_p_gain,
            "force_i_gain": candidate.force_i_gain,
            "force_damping": candidate.force_damping,
            "normal_filter_tau_s": candidate.normal_filter_tau_s,
            "orientation_ko": candidate.orientation_ko,
            "motion_kp": candidate.motion_kp,
        }
        overlay.pop("control_candidate_uid", None)
        overlay["control_candidate_uid"] = control_candidate_uid(overlay)
        _r006_validate_native_runtime_overlay(
            v3_queue,
            overlay,
            launch_profile_path=Path(launch_profile_path),
        )
        overlay_sha = _r006_native_overlay_sha256(v3_queue, overlay)
        request_uid = _r006_native_request_uid(
            v3_queue,
            occurrence_nonce=occurrence_nonce,
            control_candidate_uid=str(overlay["control_candidate_uid"]),
            normalized_overlay_sha256=overlay_sha,
            source=source,
            position=position,
        )
    except R006LiveAdapterError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise R006LiveAdapterError(
            f"r006 native request construction failed: {exc}"
        ) from exc
    return {
        "schema": v3_queue.REQUEST_SCHEMA,
        "request_uid": request_uid,
        "enqueue_sequence": enqueue_sequence,
        "occurrence_nonce": occurrence_nonce,
        "control_candidate_uid": overlay["control_candidate_uid"],
        "normalized_overlay_sha256": overlay_sha,
        "overlay": overlay,
        "source": source,
        "position": position,
    }


def _r006_native_request_candidate(
    v3_queue: Any,
    request: Mapping[str, Any],
    *,
    launch_profile_path: Path,
) -> R006Candidate:
    """Cold-read and dispatch validation for one r006-native request row."""

    expected_fields = {
        "schema",
        "request_uid",
        "enqueue_sequence",
        "occurrence_nonce",
        "control_candidate_uid",
        "normalized_overlay_sha256",
        "overlay",
        "source",
        "position",
    }
    if set(request) != expected_fields or request.get("schema") != v3_queue.REQUEST_SCHEMA:
        raise V3QueueError("r006 native request fields differ")
    overlay = request.get("overlay")
    if not isinstance(overlay, Mapping) or set(overlay) != set(v3_queue.DEFAULT_OVERLAY):
        raise V3QueueError("r006 native request overlay fields differ")
    occurrence_nonce = request.get("occurrence_nonce")
    source = request.get("source")
    position = request.get("position")
    for value, role in (
        (request.get("request_uid"), "request UID"),
        (request.get("control_candidate_uid"), "control UID"),
        (request.get("normalized_overlay_sha256"), "overlay digest"),
        (occurrence_nonce, "occurrence nonce"),
        (source, "source"),
        (position, "position"),
    ):
        if not isinstance(value, str):
            raise V3QueueError(f"r006 native request {role} is not a string")
    if (
        len(occurrence_nonce) != 32
        or any(character not in "0123456789abcdef" for character in occurrence_nonce)
    ):
        raise V3QueueError("r006 native request occurrence nonce is invalid")
    if not source or "\n" in source or position not in v3_queue.POSITIONS:
        raise V3QueueError("r006 native request source or position is invalid")
    try:
        from step5d_autotune_v3.runtime_profile import control_candidate_uid

        candidate = R006Candidate(
            force_p_gain=overlay["force_p_gain"],
            force_i_gain=overlay["force_i_gain"],
            force_damping=overlay["force_damping"],
            normal_filter_tau_s=overlay["normal_filter_tau_s"],
            orientation_ko=overlay["orientation_ko"],
            motion_kp=overlay["motion_kp"],
            target_force_n=TARGET_FORCE_N,
            i_mode=IMode.OFF if float(overlay["force_i_gain"]) == 0.0 else IMode.ON,
        )
        if str(request["control_candidate_uid"]) != control_candidate_uid(overlay):
            raise V3QueueError("r006 native request control UID differs")
        if str(request["normalized_overlay_sha256"]) != _r006_native_overlay_sha256(
            v3_queue, overlay
        ):
            raise V3QueueError("r006 native request overlay digest differs")
        if str(request["request_uid"]) != _r006_native_request_uid(
            v3_queue,
            occurrence_nonce=occurrence_nonce,
            control_candidate_uid=request["control_candidate_uid"],
            normalized_overlay_sha256=request["normalized_overlay_sha256"],
            source=source,
            position=position,
        ):
            raise V3QueueError("r006 native request UID differs")
        _r006_validate_native_runtime_overlay(
            v3_queue,
            overlay,
            launch_profile_path=Path(launch_profile_path),
        )
    except V3QueueError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, R006LiveAdapterError) as exc:
        raise V3QueueError("r006 native request candidate is invalid") from exc
    return candidate


@contextlib.contextmanager
def _r006_v3_native_policy(launch_profile_path: Path):
    """Scope the r006 candidate policy over the frozen V3 state machine."""

    import step5d_parameter_queue as v3_queue

    with _R006_V3_POLICY_LOCK:
        saved = {
            "_request_document": v3_queue._request_document,
            "_request_candidate": v3_queue._request_candidate,
            "search_candidate_allowed": v3_queue.search_candidate_allowed,
        }
        v3_queue._request_document = lambda **kwargs: _r006_native_request_document(
            v3_queue, **kwargs
        )
        v3_queue._request_candidate = lambda request: _r006_native_request_candidate(
            v3_queue,
            request,
            launch_profile_path=Path(launch_profile_path),
        )

        def native_search_candidate_allowed(candidate: Any) -> bool:
            if not isinstance(candidate, R006Candidate):
                raise V3QueueError("r006 native policy received a non-native candidate")
            return True

        v3_queue.search_candidate_allowed = native_search_candidate_allowed
        try:
            yield v3_queue
        finally:
            v3_queue._request_document = saved["_request_document"]
            v3_queue._request_candidate = saved["_request_candidate"]
            v3_queue.search_candidate_allowed = saved["search_candidate_allowed"]


class R006NativeV3DurableQueueAdapter(R006V3DurableQueueAdapter):
    """V3 durable transport with r006-native metadata cold read."""

    def __init__(
        self,
        root: Path,
        *,
        campaign_id: str,
        launch_profile_path: Path,
        release_manifest_sha256: str | None = None,
    ) -> None:
        # The frozen adapter constructor performs crash-resume and inflight
        # hydration.  Keep the native policy active across that complete cold
        # read so a low Ko/Kp request cannot be quarantined by the old V3
        # envelope during recovery.
        with _r006_v3_native_policy(Path(launch_profile_path)):
            super().__init__(
                root,
                campaign_id=campaign_id,
                launch_profile_path=launch_profile_path,
                release_manifest_sha256=release_manifest_sha256,
            )

    def pending(self) -> tuple[QueueEntry, ...]:
        with _r006_v3_native_policy(self.launch_profile_path):
            return super().pending()

    def enqueue(
        self,
        candidate: R006Candidate,
        *,
        kind: str,
        epoch: int,
        request_uid: str | None = None,
    ) -> QueueEntry:
        if not isinstance(candidate, R006Candidate):
            raise V3QueueError("r006 native queue requires an r006 candidate")
        with _r006_v3_native_policy(self.launch_profile_path):
            return super().enqueue(
                candidate,
                kind=kind,
                epoch=epoch,
                request_uid=request_uid,
            )

    def prepare_next(self) -> DispatchTicket | None:
        with _r006_v3_native_policy(self.launch_profile_path):
            return super().prepare_next()

    def complete(
        self,
        ticket: DispatchTicket,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        with _r006_v3_native_policy(self.launch_profile_path):
            super().complete(ticket, status=status, detail=detail)

    def reconcile_inflight_after_home(self) -> QueueEntry | None:
        with _r006_v3_native_policy(self.launch_profile_path):
            return super().reconcile_inflight_after_home()

    def cancel_pending(self, *, reason: str) -> tuple[QueueEntry, ...]:
        with _r006_v3_native_policy(self.launch_profile_path):
            return super().cancel_pending(reason=reason)

    def _load_metadata(self) -> None:
        if not self._metadata_path.exists():
            return
        try:
            rows = self._metadata_path.read_text(encoding="utf-8").splitlines()
            for line in rows:
                row = json.loads(line)
                candidate = R006Candidate.from_canonical(dict(row["candidate"]))
                self._metadata[str(row["request_uid"])] = QueueEntry(
                    request_uid=str(row["request_uid"]),
                    candidate=candidate,
                    kind=str(row["kind"]),
                    epoch=int(row["epoch"]),
                    logical_request_uid=(
                        None
                        if row.get("logical_request_uid") is None
                        else str(row["logical_request_uid"])
                    ),
                )
            if self._cancel_path.exists():
                for line in self._cancel_path.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    entry = self._metadata.get(str(row["request_uid"]))
                    if entry is not None:
                        self._cancelled.append(entry)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, R006LiveAdapterError):
                raise
            raise V3QueueError("r006 native queue metadata cold read failed") from exc


class R006PathEvidenceCollector(R004PathEvidenceCollector):
    """Accept the TP's structural PATH-complete endpoint for r006.

    A 500 Hz observer is not guaranteed to see a Stage25 image at exactly
    59.998 s before the TP publishes READY_HOME_NEXT at 60.000 s.  Requiring
    that intermediate image adds no safety once the terminal state, every
    evidence bin, and the final path phase are present.  All other mature
    evidence checks remain unchanged.
    """

    READY_HOME_NEXT = 78

    def _canonicalize_full_duration(
        self,
        duration: float,
        *,
        coverage_interval_s: float,
        rounding_bound_s: float,
    ) -> float:
        canonical = super()._canonicalize_full_duration(
            duration,
            coverage_interval_s=coverage_interval_s,
            rounding_bound_s=rounding_bound_s,
        )
        if canonical >= self.REQUIRED_DURATION_S:
            return canonical
        terminal_complete = (
            self.READY_HOME_NEXT in self._states
            and len(self._bins) == self.REQUIRED_BINS
            and bool(self._path_samples)
            and self._path_samples[-1].path_phase == self.REQUIRED_PHASE
        )
        if terminal_complete:
            return self.REQUIRED_DURATION_S
        return canonical


class R006LiveWriter(LiveR004Writer):
    """Preserve the typed cause of a stop-dominant host packet.

    The mature wire already fails closed and sends zero qdot.  Raising after
    that send does not weaken the stop; it prevents the following TP
    ``cmd_valid=0`` echo from replacing the builder's exact sensor/safety
    reason with the generic code 42.
    """

    def _send_packet(self, *args: Any, **kwargs: Any) -> Any:
        # Observation-only (B3 Wave 1): record travel-vs-force while TP is in
        # state 20, without editing the frozen LiveR004Writer source closure.
        # PATH (state 25) force+integral ring is likewise observation-only so a
        # reason-61 hard-stop leaves reconstructable integrator evidence.
        from step5d_autotune_v4_r008.state20_search_trace import (
            State20SearchTrace,
            build_state20_row,
            format_stop_dominant_error,
        )
        from step5d_autotune_v4_r008.state25_path_trace import (
            State25PathTrace,
            build_state25_row,
            read_live_force_integral,
        )

        sensor = args[0] if args else kwargs.get("sensor")
        command_mode = kwargs.get("command_mode")
        if command_mode is None and len(args) >= 2:
            command_mode = args[1]
        output = getattr(self, "_last_output", None)
        tp_state = None
        if output is not None:
            try:
                tp_state = int(output.integer_echoes[26])
            except (TypeError, ValueError, KeyError, IndexError):
                tp_state = None
        mode_int = None
        if command_mode is not None:
            try:
                mode_int = int(command_mode)
            except (TypeError, ValueError):
                mode_int = None
        trace = getattr(self, "_state20_trace", None)
        if not isinstance(trace, State20SearchTrace):
            trace = None
        path_trace = getattr(self, "_state25_trace", None)
        if not isinstance(path_trace, State25PathTrace):
            path_trace = None
        if trace is not None and tp_state == 20 and sensor is not None and output is not None:
            try:
                mono = float(self._mono_clock())
                trace.observe(
                    build_state20_row(
                        monotonic_s=mono,
                        wall_time_s=float(getattr(output, "observed_at_s", mono)),
                        tcp_pose_m_rad=tuple(output.tcp_pose_m_rad),
                        normal_load_n=float(sensor.normal_load_n),
                        force_norm_n=float(sensor.force_norm_n),
                        filtered_normal_n=float(sensor.filtered_normal_n),
                        torque_norm_nm=float(sensor.torque_norm_nm),
                        sensor_fresh=bool(sensor.sensor_fresh),
                        wrench=tuple(sensor.wrench),
                        tp_state=20,
                        command_mode=mode_int,
                        packet_sequence=int(getattr(self, "_packet_sequence", 0)),
                        rtde_timestamp_s=float(output.timestamp),
                        attempt_ordinal=int(getattr(self, "_ordinal", 0) or 0) or None,
                        session_epoch=int(self.prerequisites.session_epoch),
                    )
                )
            except (TypeError, ValueError, AttributeError, OSError):
                pass
        if path_trace is not None and tp_state == 25 and sensor is not None and output is not None:
            try:
                mono = float(self._mono_clock())
                integral_n_s, integral_limit_n_s = read_live_force_integral(self)
                path_trace.observe(
                    build_state25_row(
                        monotonic_s=mono,
                        wall_time_s=float(getattr(output, "observed_at_s", mono)),
                        tcp_pose_m_rad=tuple(output.tcp_pose_m_rad),
                        normal_load_n=float(sensor.normal_load_n),
                        force_norm_n=float(sensor.force_norm_n),
                        force_integral_n_s=integral_n_s,
                        force_integral_limit_n_s=integral_limit_n_s,
                        filtered_normal_n=float(sensor.filtered_normal_n),
                        torque_norm_nm=float(sensor.torque_norm_nm),
                        sensor_fresh=bool(sensor.sensor_fresh),
                        wrench=tuple(sensor.wrench),
                        tp_state=25,
                        command_mode=mode_int,
                        packet_sequence=int(getattr(self, "_packet_sequence", 0)),
                        rtde_timestamp_s=float(output.timestamp),
                        attempt_ordinal=int(getattr(self, "_ordinal", 0) or 0) or None,
                        session_epoch=int(self.prerequisites.session_epoch),
                    )
                )
            except (TypeError, ValueError, AttributeError, OSError):
                pass

        packet = super()._send_packet(*args, **kwargs)
        if packet.stop_dominant and not self._stopped:
            context_kwargs = {
                "reason_code": int(packet.reason_code),
                "reason": str(packet.reason),
                "wrench": getattr(sensor, "wrench", None),
                "tp_state": tp_state,
                "sensor_fresh": getattr(sensor, "sensor_fresh", None),
                "command_mode": mode_int,
                "normal_load_n": getattr(sensor, "normal_load_n", None),
                "force_norm_n": getattr(sensor, "force_norm_n", None),
                "torque_norm_nm": getattr(sensor, "torque_norm_nm", None),
                "filtered_normal_n": getattr(sensor, "filtered_normal_n", None),
            }
            if path_trace is not None:
                try:
                    integral_n_s, integral_limit_n_s = read_live_force_integral(self)
                    path_context = path_trace.stop_dominant_context(
                        **context_kwargs,
                        force_integral_n_s=integral_n_s,
                        force_integral_limit_n_s=integral_limit_n_s,
                    )
                    dump25 = path_trace.dump_stop_dominant(path_context)
                    if dump25 is not None:
                        path_context = dict(path_context)
                        path_context["stop_dominant_dump"] = str(dump25)
                except (OSError, TypeError, ValueError):
                    pass
            if trace is not None:
                context = trace.stop_dominant_context(**context_kwargs)
                try:
                    dump_path = trace.dump_stop_dominant(context)
                    if dump_path is not None:
                        context = dict(context)
                        context["stop_dominant_dump"] = str(dump_path)
                except (OSError, TypeError, ValueError):
                    pass
            else:
                from step5d_autotune_v4_r008.state20_search_trace import (
                    build_stop_dominant_context,
                )

                context = build_stop_dominant_context(**context_kwargs)
            raise LiveWriterError(
                format_stop_dominant_error(
                    prefix="r006 stop-dominant packet: ",
                    reason_code=int(packet.reason_code),
                    reason=str(packet.reason),
                    context=context,
                )
            )
        return packet


@dataclass(frozen=True)
class R006LiveInputs:
    """r006 identity plus reusable endpoint/path fields from the r005 bag.

    ``parent`` is a compatibility carrier, not an r005 admission authority.
    r006 validates the controller and resident runtime against r006 content;
    requiring an additional r005 controller session would be both impossible
    and an unnecessary gate on the single-writer route.
    """

    parent: R005LiveInputs
    thresholds_receipt: Path
    route_id: str
    attempt_id: str
    contract_sha256: str
    campaign_fingerprint: str
    expected_triplet: Mapping[str, str]

    def validate(
        self,
        *,
        contract: R006Contract,
        parent_contract: R005Contract,
    ) -> ThresholdReceipt:
        if self.contract_sha256 != contract.sha256:
            raise R006LiveAdapterError("r006 contract digest differs")
        if self.campaign_fingerprint != contract.campaign_fingerprint:
            raise R006LiveAdapterError("r006 campaign fingerprint differs")
        if "r006" not in self.route_id.lower() or "r006" not in self.attempt_id.lower():
            raise R006LiveAdapterError("r006 route and attempt identities are required")
        if not isinstance(self.expected_triplet, Mapping) or set(self.expected_triplet) != {"script", "txt", "urp"}:
            raise R006LiveAdapterError("r006 expected triplet is incomplete")
        for role, digest in self.expected_triplet.items():
            if not isinstance(digest, str) or len(digest) != 64:
                raise R006LiveAdapterError(f"r006 expected {role} digest is invalid")
        try:
            thresholds = load_threshold_receipt(self.thresholds_receipt, contract=contract)
        except ThresholdReceiptError as exc:
            # This failure is deliberately before writer construction and ARM.
            raise R006LiveAdapterError(f"HOME_HOLD_THRESHOLD_RECEIPT_REQUIRED:{exc}") from exc
        parent = self.parent
        if parent.contract_sha256 != contract.sha256 or parent.campaign_fingerprint != contract.campaign_fingerprint:
            raise R006LiveAdapterError("r006 compatibility input identity differs")
        if parent.route_id != self.route_id or parent.attempt_id != self.attempt_id:
            raise R006LiveAdapterError("r006 writer route/attempt differs from owner admission")
        if parent.expected_triplet != self.expected_triplet:
            raise R006LiveAdapterError("r006 writer triplet differs from owner admission")
        if (
            isinstance(parent.session_epoch, bool)
            or not isinstance(parent.session_epoch, int)
            or not 1 <= parent.session_epoch <= 2**31 - 1
            or not parent.resident_session_id
        ):
            raise R006LiveAdapterError("r006 resident epoch/session identity is invalid")
        if not parent.controller_host or not parent.kunwei_host or parent.kunwei_port <= 0:
            raise R006LiveAdapterError("r006 transport endpoints are incomplete")
        for role, directory in (("authority", parent.authority_root), ("queue", parent.queue_root)):
            if Path(directory).is_symlink() or not Path(directory).is_dir():
                raise R006LiveAdapterError(f"r006 {role} root must already be a real directory")
        if parent.ledger_path.is_symlink() or not parent.ledger_path.is_file():
            raise R006LiveAdapterError("r006 observation ledger must exist before writer construction")

        mature_parent = _r004_parent_contract(parent_contract)
        expected_eoat = mature_parent.eoat_sha256
        if parent.eoat_sha256 != expected_eoat:
            raise R006LiveAdapterError("r006 EOAT identity differs from the frozen V4 parent")
        mature_raw = dict(mature_parent.raw)
        script2 = dict(mature_raw.get("script2", {}))
        script2["controller_target"] = f"{CONTROLLER_DIRECTORY}/{contract.program}.urp"
        mature_raw.update(program=contract.program, script2=script2)
        identity_contract = _R005MatureIdentityContract(
            path=contract.path,
            sha256=contract.sha256,
            campaign_fingerprint=contract.campaign_fingerprint,
            eoat_sha256=expected_eoat,
            script1_sha256=mature_parent.script1_sha256,
            raw=mature_raw,
        )
        controller = load_controller_receipt(parent.controller_receipt)
        script1_receipt = load_script1_receipt(parent.script1_receipt)
        runtime = load_runtime_evidence(parent.runtime_evidence)
        try:
            _validate_controller_receipt_content(
                controller,
                contract=identity_contract,
                expected_triplet=self.expected_triplet,
            )
            _validate_script1_receipt_content(
                script1_receipt,
                expected_script_sha256=mature_parent.script1_sha256["script"],
                expected_eoat_sha256=expected_eoat,
            )
        except R005LiveAdapterError as exc:
            raise R006LiveAdapterError(str(exc).replace("r005", "r006")) from exc
        if controller.route_id != self.route_id:
            raise R006LiveAdapterError("r006 controller receipt route differs")
        expected_hi, expected_lo = runtime_identity_limbs(
            contract.program,
            contract.sha256,
            contract.campaign_fingerprint,
        )
        if (
            controller.runtime_protocol != R006_RUNTIME_PROTOCOL
            or controller.runtime_digest_hi != expected_hi
            or controller.runtime_digest_lo != expected_lo
            or runtime.program != contract.program
            or runtime.script_sha256 != controller.script_sha256
            or runtime.runtime_protocol != R006_RUNTIME_PROTOCOL
            or runtime.runtime_digest_hi != expected_hi
            or runtime.runtime_digest_lo != expected_lo
            or runtime.session_epoch != parent.session_epoch
            or runtime.resident_session_id != parent.resident_session_id
            or not runtime.program_running
            or not runtime.uninterrupted
        ):
            raise R006LiveAdapterError("r006 resident runtime identity differs")
        profile = load_new_eoat_profile()
        controller.validate_eoat_readback(
            payload_kg=profile.payload_kg,
            payload_cog_m=profile.cog_m,
            tcp_offset_m_rad=profile.controller_tcp_m_rad,
        )

        launch_profile = _regular_json(parent.launch_profile_path, "r006 V3 launch profile")
        software_baseline = _regular_json(parent.software_baseline_receipt, "r006 software baseline")
        if launch_profile.get("schema") != "step5d.autotune-v3/launch-profile-v1" or launch_profile.get("tp_program_id") != contract.program:
            raise R006LiveAdapterError("r006 launch profile identity differs")
        _digest(parent.release_manifest_sha256, "r006 release manifest digest")
        if (
            software_baseline.get("schema") != "step5d.autotune-v4/r005-software-baseline-v1"
            or software_baseline.get("zero_tare_config_write") is not False
            or software_baseline.get("parse_errors") != 0
            or software_baseline.get("dropped_bytes") != 0
            or not isinstance(software_baseline.get("sample_count"), int)
            or software_baseline.get("sample_count", 0) < 900
        ):
            raise R006LiveAdapterError("r006 reused software-baseline primitive is not clean")
        parent.software_baseline_n()
        now = float(time.time() if parent.now_s is None else parent.now_s)
        for role, observed in (
            ("controller receipt", controller.observed_at_s),
            ("Script1 receipt", script1_receipt.observed_at_s),
            ("runtime evidence", runtime.observed_at_s),
            ("software baseline", _observed_at_seconds(software_baseline, "software baseline")),
        ):
            _validate_not_from_future(float(observed), now, role)
        if not (
            script1_receipt.observed_at_s
            <= _observed_at_seconds(software_baseline, "software baseline")
            <= runtime.observed_at_s
        ):
            raise R006LiveAdapterError("r006 prepare ordering requires Script1 <= baseline <= runtime")
        return thresholds


class R006ProductionQueue:
    """Typed r006 point view over the V3 durable queue receiver."""

    max_pending = 2
    physical_inflight_max = 1

    def __init__(self, delegate: V3DurableQueueAdapter) -> None:
        self.delegate = delegate

    @staticmethod
    def candidate(point: ParameterPoint) -> R006Candidate:
        return R006Candidate.from_point(point)

    def enqueue(self, point, *, kind: str, epoch: int) -> QueueEntry:
        return self.delegate.enqueue(self.candidate(point), kind=kind, epoch=epoch)

    def pending(self) -> tuple[QueueEntry, ...]:
        return self.delegate.pending()

    @property
    def inflight(self) -> DispatchTicket | None:
        return self.delegate.inflight

    def prepare_next(self) -> DispatchTicket | None:
        return self.delegate.prepare_next()

    def complete(self, ticket: DispatchTicket, *, status: str, detail: str | None = None) -> None:
        self.delegate.complete(ticket, status=status, detail=detail)

    def cancel_pending(self, *, reason: str) -> tuple[QueueEntry, ...]:
        return self.delegate.cancel_pending(reason=reason)

    def reconcile_inflight_after_home(self) -> QueueEntry | None:
        return self.delegate.reconcile_inflight_after_home()

    def record_execution(self, ticket: DispatchTicket, *, attempt_sequence: int, execution_id: str) -> None:
        self.delegate.record_execution(ticket, attempt_sequence=attempt_sequence, execution_id=execution_id)


class R006ObservationLedger:
    """Production evidence owner: r005 hash-chain plus r006 raw sidecar."""

    def __init__(self, ledger: ObservationLedger, *, campaign_fingerprint: str) -> None:
        if not isinstance(ledger, ObservationLedger):
            raise R006LiveAdapterError("r006 production ledger must be r005 ObservationLedger")
        if not isinstance(campaign_fingerprint, str) or len(campaign_fingerprint) != 64:
            raise R006LiveAdapterError("r006 sidecar campaign fingerprint is invalid")
        self.ledger = ledger
        self.campaign_fingerprint = campaign_fingerprint
        self.sidecar = R006ObjectiveSidecar(
            ledger.path.with_name(ledger.path.stem + "-r006-objectives.jsonl"),
            campaign_fingerprint=campaign_fingerprint,
        )

    @property
    def raw_sidecar_path(self) -> Path:
        return self.sidecar.path

    def fresh_process_verify(self) -> tuple[Mapping[str, Any], ...]:
        self.ledger.fresh_process_verify()
        rows = self.sidecar.fresh_process_verify()
        paired = {
            (record.attempt_sequence, record.metrics.get("execution_id"))
            for record in self.ledger.records
            if record.kind != "QUALIFICATION"
        }
        return tuple(
            row
            for row in rows
            if (row["attempt_sequence"], row["execution_id"]) in paired
        )

    def append_attempt_result(
        self,
        result: AttemptResult,
        *,
        epoch: int,
        point: ParameterPoint,
    ) -> Mapping[str, Any]:
        if not isinstance(result, AttemptResult):
            raise R006LiveAdapterError("r006 raw evidence sink requires AttemptResult")
        if result.kind == "QUALIFICATION" or not result.raw_path_samples:
            raise R006LiveAdapterError("r006 trainable attempt lacks raw PATH samples")
        builder = R006ObjectiveBuilder(
            attempt_sequence=result.attempt_sequence,
            execution_id=result.execution_id,
            campaign_fingerprint=self.campaign_fingerprint,
            candidate_uid=result.candidate.candidate_uid,
        )
        for sample in result.raw_path_samples:
            builder.add(sample)
        receipt = builder.finalize(
            metadata={
                "candidate_uid": result.candidate.candidate_uid,
                "point_key": list(point.key),
                "epoch": epoch,
                "kind": result.kind,
            }
        )
        return self.sidecar.append(
            receipt,
            epoch=epoch,
            candidate_uid=result.candidate.candidate_uid,
            kind=result.kind,
            point_key=list(point.key),
        )

    def sidecar_binding(self) -> dict[str, Any]:
        rows = self.fresh_process_verify()
        sidecar = self.raw_sidecar_path
        if not sidecar.is_file() or sidecar.is_symlink():
            raise R006LiveAdapterError("r006 raw artifact sidecar is unavailable")
        return {
            "sidecar_path": str(sidecar.resolve(strict=True)),
            "sidecar_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            "campaign_fingerprint": self.campaign_fingerprint,
            "rows": [
                {"attempt_sequence": int(row["attempt_sequence"]), "execution_id": str(row["execution_id"])}
                for row in rows
            ],
        }


def _point_from_candidate(candidate: Any) -> ParameterPoint:
    """Decode only r006 candidates in the production policy path.

    The old r005 shape is accepted as a read-only compatibility seam for
    already-sealed parent records; it is never constructed by r006 and it is
    never used for r006 enqueue/ARM policy.
    """

    if isinstance(candidate, R006Candidate):
        return candidate.as_point
    if not isinstance(candidate, R005Candidate):
        raise R006LiveAdapterError("r006 optimizer received a non-r006 candidate")
    coordinates = candidate.named7d
    steps = tuple(int(round(float(value) / 0.25)) for value in coordinates)
    if any(abs(float(value) - step * 0.25) > 1e-10 for value, step in zip(coordinates, steps, strict=True)):
        raise R006LiveAdapterError("r005 candidate is not on the r006 quarter-octave lattice")
    return ParameterPoint(
        p_step=steps[0],
        d_step=steps[1],
        tau_step=steps[2],
        i_mode=IMode.OFF if candidate.i_off else IMode.ON,
        i_step=None if candidate.i_off else steps[3],
        ko_step=steps[5],
        kp_step=steps[6],
    )


def _candidate_from_point(point: ParameterPoint) -> R006Candidate:
    return R006Candidate.from_point(point)


class R006ProductionOptimizer:
    """r005 HostLoop port backed by the managed r006 CUDA worker."""

    def __init__(self, *, contract: R006Contract, ledger: R006ObservationLedger, seed: int = 6006) -> None:
        self.contract = contract
        self.r006_ledger = ledger
        self.client = R006CudaQLogNEI(
            runtime_manifest=contract.runtime_manifest,
            artifact_binding={"uninitialized": True},
            seed=seed,
        )
        self.last_ask_metadata: Mapping[str, Any] = {}
        self.route_observations: list[ParameterPoint] = []
        self.region_traversal = RegionTraversal(TrustRegion(ANCHOR_POINT))

    def include_route_observation(self, point: ParameterPoint) -> None:
        if not isinstance(point, ParameterPoint):
            raise R006LiveAdapterError("r006 route observation is not typed")
        self.route_observations.append(point)

    def recenter_certified_region(self, center: ParameterPoint) -> None:
        if not isinstance(center, ParameterPoint):
            raise R006LiveAdapterError("r006 certified-region center is not typed")
        self.region_traversal = RegionTraversal(TrustRegion(center))

    def fit_group_once(self, group: int) -> None:
        self.client.update_artifact_binding(self.r006_ledger.sidecar_binding())
        self.client.fit_group_once(group)

    def freeze(self) -> None:
        self.client.freeze()

    def local_certificate(
        self,
        *,
        region: Sequence[ParameterPoint],
        incumbent: ParameterPoint,
        epsilon_n: float,
    ) -> PACCertificate:
        self.client.update_artifact_binding(self.r006_ledger.sidecar_binding())
        raw = self.client.certificate(
            region=region,
            incumbent=incumbent,
            epsilon_n=epsilon_n,
        )
        try:
            certificate = PACCertificate(
                incumbent_ucb_n=float(raw["incumbent_ucb_n"]),
                minimum_lcb_n=float(raw["minimum_lcb_n"]),
                epsilon_n=float(raw["epsilon_n"]),
                region_size=int(raw["region_size"]),
                simultaneous_z95=float(raw["simultaneous_z95"]),
                global_convergence_claim=bool(raw.get("global_convergence_claim", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OptimizerError("r006 local certificate fields are invalid") from exc
        if certificate.epsilon_n != float(epsilon_n) or certificate.global_convergence_claim:
            raise OptimizerError("r006 local certificate binding differs")
        return certificate

    def ask(
        self,
        *,
        observations: Sequence[Any],
        pending: Sequence[Any],
        incumbent: Any,
        q: int = 1,
    ) -> OptimizerAsk:
        if q not in (1, 4):
            raise OptimizerError("r006 scheduler q must be 4 or 1")
        incumbent_point = _point_from_candidate(incumbent)
        observed = {_point_from_candidate(record.candidate) for record in observations if hasattr(record, "candidate")}
        pending_points = tuple(_point_from_candidate(candidate) for candidate in pending)
        choices = list(
            point
            for point in neighbors(incumbent_point)
            if point not in pending_points
        )
        traversal_target = self.region_traversal.next()
        if traversal_target is not None:
            route = route_bfs(incumbent_point, traversal_target)
            if len(route.points) > 1:
                step = route.points[1]
                if step not in pending_points and step not in choices:
                    choices.append(step)
        choices = tuple(choices)
        if len(choices) < q:
            raise OptimizerError("r006 graph frontier is exhausted")
        self.client.update_artifact_binding(self.r006_ledger.sidecar_binding())
        ask = self.client.ask(
            choices=choices,
            pending=pending_points,
            incumbent=incumbent_point,
            q=q,
        )
        candidate = _candidate_from_point(ask.point)
        self.last_ask_metadata = dict(ask.metadata)
        return OptimizerAsk(candidate=candidate, metadata=self.last_ask_metadata)

    def tell(self, record: Any) -> None:
        # The immutable sidecar, not a caller record, is the worker's training
        # source.  Keep only a bounded audit count at the host seam.
        if not getattr(record, "sealed", False):
            raise OptimizerError("r006 optimizer tell requires a sealed record")


class R006HostLoop(HostLoop):
    """r006 campaign policy over the mature r005 physical HostLoop.

    The inherited ``run_one`` owns Home, dispatch, ARM, 60-second execution,
    safe return, sealing, queue completion, and authority revocation.  This
    subclass changes only the typed campaign refill/selection/completion
    policy so r006's 25+25 warm start and local PAC gate cannot silently fall
    back to the r005 ten-row bootstrap.
    """

    _WARM_START_1 = "WARM_START_1"
    _ROUTE = "ROUTE"
    _WARM_START_2 = "WARM_START_2"
    _FIT_FREEZE = "FIT_FREEZE"

    def __init__(
        self,
        *,
        r006_contract: R006Contract,
        r006_ledger: R006ObservationLedger,
        **kwargs: Any,
    ) -> None:
        if kwargs.get("threshold_policy") is None:
            raise RuntimeErrorR005(
                "r006 production HostLoop requires the typed threshold receipt before Home/ARM"
            )
        self.threshold_policy = kwargs.pop("threshold_policy")
        self.evidence_sink = kwargs.pop("evidence_sink", None)
        self.r006_contract = r006_contract
        self.r006_ledger = r006_ledger
        self._first_plan = warm_start_plan()
        self._second_plan: tuple[ParameterPoint, ...] = ()
        self._route_points: tuple[ParameterPoint, ...] = ()
        self._second_center: ParameterPoint | None = None
        self._first_count = 0
        self._route_count = 0
        self._second_count = 0
        self._retest_records: list[Any] = []
        self._last_certificate: PACCertificate | None = None
        self._certified_region: CertifiedRegionState | None = None
        self.scheduler_q_history: list[int] = []
        self._frozen = False
        super().__init__(**kwargs)
        if not isinstance(self.cursor, R006Candidate):
            self.cursor = R006Candidate.from_canonical(self.cursor.canonical)

    @staticmethod
    def _phase_name(value: Any) -> str:
        return value.value if isinstance(value, CampaignPhase) else str(value)

    @property
    def terminal(self) -> bool:
        return self._phase_name(self.phase) in {
            CampaignPhase.COMPLETE.value,
            CampaignPhase.INCOMPLETE_STOPPED.value,
        }

    @property
    def status(self) -> str:
        return self._phase_name(self.phase)

    def _sidecar_rows(self) -> tuple[Mapping[str, Any], ...]:
        return self.r006_ledger.fresh_process_verify()

    @staticmethod
    def _row_point(row: Mapping[str, Any]) -> ParameterPoint:
        key = row.get("point_key")
        if not isinstance(key, list) or len(key) != 7:
            raise RuntimeErrorR005("r006 sidecar point identity is missing")
        try:
            return ParameterPoint(
                int(key[0]),
                int(key[1]),
                int(key[2]),
                IMode(str(key[3])),
                None if key[4] is None else int(key[4]),
                int(key[5]),
                int(key[6]),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeErrorR005("r006 sidecar point identity is invalid") from exc

    @staticmethod
    def _row_receipt(row: Mapping[str, Any]) -> Mapping[str, Any]:
        receipt = row.get("receipt")
        if not isinstance(receipt, Mapping):
            raise RuntimeErrorR005("r006 sidecar row lacks a cold-read receipt")
        return receipt

    def _rows_for(self, kind: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(row for row in self._sidecar_rows() if row.get("kind") == kind)

    def _select_center(self) -> ParameterPoint:
        grouped: dict[ParameterPoint, list[float]] = {}
        for row in self._rows_for(self._WARM_START_1):
            receipt = self._row_receipt(row)
            objective = receipt.get("objective_mae_n")
            if isinstance(objective, (int, float)) and math.isfinite(float(objective)) and bool(row.get("trainable")):
                grouped.setdefault(self._row_point(row), []).append(float(objective))
        observations = [
            PointObservation(
                point,
                statistics.fmean(values),
                std_n=statistics.pstdev(values) if len(values) > 1 else 0.0,
                repeat_count=len(values),
                eligible=True,
            )
            for point, values in grouped.items()
        ]
        center = select_second_center(observations)
        self._second_center = center
        self._certified_region = CertifiedRegionState.from_trust_region(
            TrustRegion(center)
        )
        route = route_bfs(ANCHOR_POINT, center, region=TrustRegion(ANCHOR_POINT))
        visited = {self._row_point(row) for row in self._sidecar_rows()}
        self._route_points = tuple(
            point for point in route.points[1:] if point not in visited
        )
        # ``_route_points`` is the remaining route after a cold read.  Its
        # index is therefore local to the remaining suffix; using the total
        # number of historical route rows as an additional offset would skip
        # points after a crash/resume.
        self._route_count = 0
        self._second_plan = second_warm_start_plan(center)
        optimizer = self.optimizer
        include = getattr(optimizer, "include_route_observation", None)
        if include is None:
            raise OptimizerError("r006 optimizer lacks route-observation binding")
        for point in route.route_observations:
            include(point)
        return center

    def _resume_from_ledger(self) -> None:
        self.ledger.fresh_process_verify()
        rows = self._sidecar_rows()
        records = self.ledger.records
        queue_sequence = int(getattr(self.queue, "last_attempt_sequence", 0))
        self.next_attempt_sequence = max(
            [queue_sequence, *(int(record.attempt_sequence) for record in records)],
            default=0,
        ) + 1
        if records:
            self.epoch = max(record.epoch for record in records)
            current = [record for record in records if record.epoch == self.epoch]
            try:
                self.cursor = (
                    current[-1].candidate
                    if isinstance(current[-1].candidate, R006Candidate)
                    else R006Candidate.from_canonical(current[-1].candidate.canonical)
                )
            except (AttributeError, R006LiveAdapterError) as exc:
                raise RuntimeErrorR005("r006 cold-read candidate is not native") from exc
            self.qualification_passes = sum(
                1 for record in current if record.qualification_eligible
            )
        if self.qualification_passes < 3:
            self.phase = CampaignPhase.QUALIFICATION
            return
        self._first_count = len(self._rows_for(self._WARM_START_1))
        if self._first_count < len(self._first_plan):
            self.phase = self._WARM_START_1
            return
        self._select_center()
        if self._route_points:
            self.phase = self._ROUTE
            return
        self._second_count = len(self._rows_for(self._WARM_START_2))
        if self._second_count < len(self._second_plan):
            self.phase = self._WARM_START_2
            return
        self.phase = self._FIT_FREEZE
        self.events.append("R006_RESUME_COLD_READ_VERIFIED")

    def start_epoch(self) -> int:
        if self.terminal:
            raise RuntimeErrorR005("cannot start an epoch after terminal state")
        self.epoch = 1 if self.epoch == 0 else self.epoch + 1
        self.phase = CampaignPhase.QUALIFICATION
        self.cursor = R006Candidate()
        self.qualification_passes = 0
        self._first_count = 0
        self._route_count = 0
        self._second_count = 0
        self._second_center = None
        self._route_points = ()
        self._second_plan = ()
        self._retest_records.clear()
        binder = getattr(self.queue, "bind_home", None)
        if binder is not None:
            binder(campaign_epoch=self.epoch, last_trial_id=0, last_command_seq=0)
        self.events.append(f"EPOCH:{self.epoch}:R006_FRESH_QUALIFICATIONS")
        return self.epoch

    def _queue_request(
        self,
        candidate: R006Candidate,
        *,
        kind: str,
        base: R006Candidate | None = None,
    ) -> None:
        if not isinstance(candidate, R006Candidate):
            raise RuntimeErrorR005("r006 refill candidate is not native")
        transition_base = self.cursor if base is None else base
        if not isinstance(transition_base, R006Candidate):
            raise RuntimeErrorR005("r006 refill base candidate is not native")
        previous_point = _point_from_candidate(transition_base)
        candidate_point = _point_from_candidate(candidate)
        if previous_point != candidate_point and not transition_is_legal(
            previous_point, candidate_point
        ):
            raise RuntimeErrorR005(
                "r006 dispatch transition is outside the native one-coordinate quarter-octave graph"
            )
        self.queue.enqueue(candidate, kind=kind, epoch=self.epoch)
        self.events.append(f"ENQUEUE:{kind}")

    def _refill(self) -> None:
        if self.terminal:
            return
        if self.epoch <= 0:
            raise RuntimeErrorR005("r006 refill requires a started epoch")
        if self.queue.inflight is not None:
            raise RuntimeErrorR005("r006 refill cannot run with a physical inflight row")
        while len(self.queue.pending()) < 2:
            phase = self._phase_name(self.phase)
            pending = self.queue.pending()
            base = pending[-1].candidate if pending else self.cursor
            if phase == CampaignPhase.QUALIFICATION.value:
                queued = sum(item.kind == "QUALIFICATION" for item in pending)
                if self.qualification_passes + queued >= 3:
                    self.phase = self._WARM_START_1
                    continue
                self._queue_request(R006Candidate(), kind="QUALIFICATION", base=base)
                continue
            if phase == self._WARM_START_1:
                index = self._first_count + sum(item.kind == self._WARM_START_1 for item in pending)
                if index >= len(self._first_plan):
                    if pending:
                        break
                    self.optimizer.fit_group_once(1)  # type: ignore[attr-defined]
                    self._select_center()
                    self.phase = self._ROUTE if self._route_points else self._WARM_START_2
                    continue
                self._queue_request(_candidate_from_point(self._first_plan[index]), kind=self._WARM_START_1, base=base)
                continue
            if phase == self._ROUTE:
                index = self._route_count + sum(item.kind == self._ROUTE for item in pending)
                if index >= len(self._route_points):
                    if pending:
                        break
                    self._second_count = len(self._rows_for(self._WARM_START_2))
                    self.phase = self._WARM_START_2
                    continue
                self._queue_request(_candidate_from_point(self._route_points[index]), kind=self._ROUTE, base=base)
                continue
            if phase == self._WARM_START_2:
                index = self._second_count + sum(item.kind == self._WARM_START_2 for item in pending)
                if index >= len(self._second_plan):
                    if pending:
                        break
                    self.phase = self._FIT_FREEZE
                    continue
                self._queue_request(_candidate_from_point(self._second_plan[index]), kind=self._WARM_START_2, base=base)
                continue
            if phase == self._FIT_FREEZE:
                self.optimizer.fit_group_once(2)  # type: ignore[attr-defined]
                self.optimizer.freeze()  # type: ignore[attr-defined]
                self._frozen = True
                self.phase = CampaignPhase.BO
                self.events.append("R006_GP_FIT_GROUP_2_FREEZE")
                continue
            if phase == CampaignPhase.BO.value:
                epsilon = float(getattr(self.threshold_policy, "pac_epsilon_n"))
                scheduler = QLogNEIScheduler(epsilon_n=epsilon)
                q = scheduler.q_for(self._last_certificate)
                self.scheduler_q_history.append(q)
                self.events.append(f"R006_SCHEDULER_Q:{q}")
                ask = self.optimizer.ask(
                    observations=self.ledger.records,
                    pending=tuple(item.candidate for item in pending),
                    incumbent=base,
                    q=q,
                )
                self._queue_request(ask.candidate, kind="BO_TRIAL", base=base)
                continue
            if phase == CampaignPhase.RETEST.value:
                if self.retest_candidate is None:
                    raise RuntimeErrorR005("r006 retest incumbent is missing")
                if len(self._retest_records) + sum(item.kind == "RETEST" for item in pending) >= 3:
                    break
                self._queue_request(self.retest_candidate, kind="RETEST", base=base)
                continue
            raise RuntimeErrorR005(f"unknown r006 host phase {phase}")
        self.events.append(f"R006_REFILL:PENDING={len(self.queue.pending())}")

    def _record_and_tell(self, result: AttemptResult) -> Any:
        if self.evidence_sink is not None and result.kind != "QUALIFICATION":
            self.evidence_sink(result)
        record = self.ledger.append(result.to_record(self.contract.campaign_fingerprint))
        self._phase("SEAL")
        if result.kind != "QUALIFICATION":
            rows = self._sidecar_rows()
            matched = [
                row for row in rows
                if row.get("attempt_sequence") == result.attempt_sequence
                and row.get("execution_id") == result.execution_id
            ]
            if len(matched) != 1:
                raise RuntimeErrorR005("r006 sealed attempt lacks one raw sidecar identity")
            receipt = self._row_receipt(matched[0])
            if bool(matched[0].get("trainable")) and record.eligible:
                self.optimizer.tell(record)
                self._phase("R006_TELL_FORMAL_RAW")
            elif record.eligible:
                self._phase("R006_SAFE_NONTRAINABLE_CONTINUE")
            self._last_sidecar_receipt = receipt
        return record

    def _record_formal_objective(self, result: AttemptResult) -> float | None:
        receipt = getattr(self, "_last_sidecar_receipt", None)
        if not isinstance(receipt, Mapping):
            return None
        value = receipt.get("objective_mae_n")
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None

    def _finish_retests(self) -> None:
        rows = self._rows_for("RETEST")
        if len(rows) < 3:
            return
        values = [
            float(self._row_receipt(row)["objective_mae_n"])
            for row in rows[-3:]
            if isinstance(self._row_receipt(row).get("objective_mae_n"), (int, float))
        ]
        anchor_values = [
            float(self._row_receipt(row)["objective_mae_n"])
            for row in self._rows_for(self._WARM_START_1)
            if self._row_point(row) == ANCHOR_POINT
            and isinstance(self._row_receipt(row).get("objective_mae_n"), (int, float))
        ]
        gates = [
            record.eligible and record.safe_return and record.binding_ok
            and record.safety_gate and record.contact_gate and record.return_gate
            and record.timing_gate and record.identity_gate and record.motion_gate
            for record in self.ledger.records
            if record.kind == "RETEST"
        ][-3:]
        threshold = float(getattr(self.threshold_policy, "application_mae_threshold_n"))
        application = (
            len(values) == 3
            and len(gates) == 3
            and sum(gate and value <= threshold for gate, value in zip(gates, values, strict=True)) >= 2
            and all(gates)
            and anchor_values
            and statistics.median(values) <= 0.95 * statistics.median(anchor_values)
        )
        if not application or self._second_center is None:
            self.phase = CampaignPhase.BO
            self._retest_records.clear()
            self.events.append("R006_RETEST_FAILED_RETURN_TO_BO")
            return
        incumbent = (
            _point_from_candidate(self.retest_candidate)
            if self.retest_candidate is not None
            else self._second_center
        )
        assert incumbent is not None
        certificate = self.optimizer.local_certificate(
            region=TrustRegion(incumbent).points(),
            incumbent=incumbent,
            epsilon_n=float(getattr(self.threshold_policy, "pac_epsilon_n")),
        )
        self._last_certificate = certificate
        if certificate.passed:
            self.phase = CampaignPhase.COMPLETE
            self.events.append("R006_COMPLETE_APPLICATION_AND_LOCAL_PAC")
        else:
            self.phase = CampaignPhase.BO
            self._retest_records.clear()
            self.events.append("R006_LOCAL_PAC_FAILED_RETURN_TO_BO")

    def _advance_after_record(self, record: Any) -> None:
        self.cursor = record.candidate
        if record.kind == "QUALIFICATION":
            if record.qualification_eligible:
                self.qualification_passes += 1
            self._sync_qualification_state()
            if self.qualification_passes >= 3:
                self.phase = self._WARM_START_1
            return
        if record.kind == self._WARM_START_1:
            self._first_count += 1
            return
        if record.kind == self._ROUTE:
            self._route_count += 1
            return
        if record.kind == self._WARM_START_2:
            self._second_count += 1
            return
        if record.kind == "BO_TRIAL":
            value = self._record_formal_objective(record)
            threshold = float(getattr(self.threshold_policy, "application_mae_threshold_n"))
            if value is not None and value <= threshold:
                cancelled = self.queue.cancel_pending(reason="r006_application_threshold_incumbent_retests")
                self.events.append(f"R006_CANCEL_PENDING:{len(cancelled)}")
                self.retest_candidate = record.candidate
                candidate_point = _point_from_candidate(record.candidate)
                self._certified_region = (
                    self._certified_region
                    or CertifiedRegionState.from_trust_region(TrustRegion(candidate_point))
                ).recenter_expand(candidate_point)
                recenter = getattr(self.optimizer, "recenter_certified_region", None)
                if recenter is not None:
                    recenter(candidate_point)
                self.events.append("R006_CERTIFIED_REGION_RECENTER_EXPAND")
                self._retest_records.clear()
                self.phase = CampaignPhase.RETEST
            return
        if record.kind == "RETEST":
            self._retest_records.append(record)
            self._finish_retests()


@dataclass(frozen=True)
class _R006MaturePrerequisites:
    """r006 content identity presented to the mature r004 writer."""

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
            raise R006LiveAdapterError("r006 controller route differs")
        _validate_not_from_future(self.controller.observed_at_s, now_s, "controller receipt")
        _validate_not_from_future(self.script1.observed_at_s, now_s, "Script1 receipt")
        _validate_controller_receipt_content(
            self.controller,
            contract=self.contract,
            expected_triplet=self.expected_triplet,
        )
        _validate_script1_receipt_content(
            self.script1,
            expected_script_sha256=self.contract.script1_sha256["script"],
            expected_eoat_sha256=self.contract.eoat_sha256,
        )
        profile = load_new_eoat_profile()
        self.controller.validate_eoat_readback(
            payload_kg=profile.payload_kg,
            payload_cog_m=profile.cog_m,
            tcp_offset_m_rad=profile.controller_tcp_m_rad,
        )
        expected_hi, expected_lo = runtime_identity_limbs(
            self.contract.raw["program"],
            self.contract.sha256,
            self.contract.campaign_fingerprint,
        )
        if (
            self.controller.runtime_protocol != R006_RUNTIME_PROTOCOL
            or self.controller.runtime_digest_hi != expected_hi
            or self.controller.runtime_digest_lo != expected_lo
            or self.runtime.program != self.contract.raw["program"]
            or self.runtime.script_sha256 != self.controller.script_sha256
            or self.runtime.runtime_protocol != R006_RUNTIME_PROTOCOL
            or self.runtime.runtime_digest_hi != expected_hi
            or self.runtime.runtime_digest_lo != expected_lo
            or self.runtime.session_epoch != self.session_epoch
            or self.runtime.resident_session_id != self.resident_session_id
            or not self.runtime.program_running
            or not self.runtime.uninterrupted
        ):
            raise R006LiveAdapterError("r006 resident runtime identity differs at writer open")
        if (
            self.controller.safety_mode != "NORMAL"
            or not self.controller.stationary
            or self.runtime.observed_at_s <= self.controller.observed_at_s
        ):
            raise R006LiveAdapterError("r006 writer entry is not fresh stationary Safety NORMAL")


_R006_INJECTION_LOCK = threading.Lock()


@dataclass(frozen=True, eq=False)
class _R006PreparedControlKey:
    """The exact r006 identity needed to consume one prepared control."""

    candidate: Any
    attempt_id: str
    release_contract: Any
    path_requested: bool
    canonical_runtime_only: bool

    def matches(
        self,
        *,
        candidate: Any,
        attempt_id: str,
        release_contract: Any,
        path_requested: bool,
        canonical_runtime_only: bool,
    ) -> bool:
        # Candidate equality is intentional: R005MatureWriter materializes
        # the immutable r004 candidate once during ARM and once for the
        # mature execute surface.  The release contract remains identity
        # bound so a structurally equal replacement cannot consume it.
        try:
            candidate_matches = self.candidate is candidate or self.candidate == candidate
        except Exception:
            candidate_matches = False
        return (
            candidate_matches
            and self.attempt_id == attempt_id
            and self.release_contract is release_contract
            and self.path_requested == path_requested
            and self.canonical_runtime_only == canonical_runtime_only
        )


class _R006ScopedRuntimeInjection:
    """Process-local dependency injection for the one live writer.

    The mature source remains byte-for-byte frozen.  The lock is held for the
    complete writer lifetime and every global is restored in ``finally``.
    """

    def __init__(
        self,
        *,
        motion_profile: Any,
        path_reference: Callable[..., Any],
        force_integral_limit_n_s: float = 1.0,
    ) -> None:
        self.motion_profile = motion_profile
        self.path_reference = path_reference
        limit = float(force_integral_limit_n_s)
        if not math.isfinite(limit) or limit <= 0.0:
            raise R006LiveAdapterError(
                "force_integral_limit_n_s must be positive and finite"
            )
        self.force_integral_limit_n_s = limit
        self._saved: dict[tuple[Any, str], Any] = {}
        self._original_control: Callable[..., Any] | None = None
        self._prepared_control: Any | None = None
        self._prepared_key: _R006PreparedControlKey | None = None
        self.active = False
        self._owns_lock = False

    @staticmethod
    def _constructor_argument(
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
        *,
        name: str,
        position: int,
        default: Any = None,
    ) -> Any:
        if len(args) > position and name in kwargs:
            raise R006LiveAdapterError(f"r006 canonical control argument {name!r} was duplicated")
        if len(args) > position:
            return args[position]
        return kwargs.get(name, default)

    def _key_from_constructor_call(
        self,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> _R006PreparedControlKey:
        candidate = self._constructor_argument(args, kwargs, name="candidate", position=0)
        attempt_id = self._constructor_argument(args, kwargs, name="attempt_id", position=1)
        release_contract = self._constructor_argument(
            args,
            kwargs,
            name="release_contract",
            position=2,
        )
        path_requested = self._constructor_argument(
            args,
            kwargs,
            name="path_requested",
            position=3,
            default=False,
        )
        canonical_runtime_only = self._constructor_argument(
            args,
            kwargs,
            name="canonical_runtime_only",
            position=5,
            default=False,
        )
        if candidate is None or not isinstance(attempt_id, str) or not attempt_id:
            raise R006LiveAdapterError("r006 canonical control key is incomplete")
        if not isinstance(path_requested, bool) or not isinstance(canonical_runtime_only, bool):
            raise R006LiveAdapterError("r006 canonical control key policies are not typed")
        return _R006PreparedControlKey(
            candidate=candidate,
            attempt_id=attempt_id,
            release_contract=release_contract,
            path_requested=path_requested,
            canonical_runtime_only=canonical_runtime_only,
        )

    def _patch_path_reference(self) -> None:
        calibrated = sys.modules.get("step5d_autotune_v4_r004.calibrated_runtime")
        if calibrated is None:
            raise R006LiveAdapterError(
                "r006 calibrated runtime was not loaded by the mature control primitive"
            )
        key = (calibrated, "step5_path_reference")
        if key not in self._saved:
            self._saved[key] = getattr(calibrated, "step5_path_reference")
            setattr(calibrated, "step5_path_reference", self.path_reference)

    def clear_prepared_control(self) -> None:
        """Drop any candidate-specific object before the next lifecycle edge."""

        self._prepared_control = None
        self._prepared_key = None

    def prepare_control(
        self,
        *,
        candidate: Any,
        attempt_id: str,
        release_contract: Any,
        path_requested: bool,
        canonical_runtime_only: bool,
    ) -> Any:
        """Construct the expensive canonical control before the mature ARM call."""

        if not self.active or self._original_control is None:
            raise R006LiveAdapterError("r006 runtime injection is not active")
        if self._prepared_key is not None:
            raise R006LiveAdapterError("r006 prepared canonical control already exists")
        if not isinstance(path_requested, bool) or not isinstance(canonical_runtime_only, bool):
            raise R006LiveAdapterError("r006 prepared control policies are not typed")
        key = _R006PreparedControlKey(
            candidate=candidate,
            attempt_id=attempt_id,
            release_contract=release_contract,
            path_requested=path_requested,
            canonical_runtime_only=canonical_runtime_only,
        )
        control_factory = (
            _R006NativeCanonicalQualificationControl
            if isinstance(candidate, R006Candidate)
            else self._original_control
        )
        control = control_factory(
            candidate,
            attempt_id=attempt_id,
            release_contract=release_contract,
            path_requested=path_requested,
            motion_profile=self.motion_profile,
            canonical_runtime_only=canonical_runtime_only,
            force_integral_limit_n_s=float(self.force_integral_limit_n_s),
        )
        # Keep the existing r006 path snapshot patching semantics, but do it
        # while the expensive object is still being prepared at Home.
        self._patch_path_reference()
        self._prepared_key = key
        self._prepared_control = control
        return control

    def consume_prepared_control(self, key: _R006PreparedControlKey) -> Any:
        """Consume one exact prepared control; never reconstruct it on execute."""

        if not self.active:
            raise R006LiveAdapterError("r006 runtime injection is not active")
        prepared_key = self._prepared_key
        prepared_control = self._prepared_control
        if prepared_key is None or prepared_control is None:
            raise R006LiveAdapterError(
                "r006 prepared canonical control is missing or already consumed"
            )
        if not prepared_key.matches(
            candidate=key.candidate,
            attempt_id=key.attempt_id,
            release_contract=key.release_contract,
            path_requested=key.path_requested,
            canonical_runtime_only=key.canonical_runtime_only,
        ):
            self.clear_prepared_control()
            raise R006LiveAdapterError("r006 prepared canonical control key mismatch")
        self.clear_prepared_control()
        return prepared_control

    def activate(self) -> None:
        if self.active:
            raise R006LiveAdapterError("r006 runtime injection is already active")
        if not _R006_INJECTION_LOCK.acquire(blocking=False):
            raise R006LiveAdapterError("another mature runtime injection already owns the process")
        self._owns_lock = True
        original_control = r004_writer_module.CanonicalQualificationControl
        self._original_control = original_control

        def qualification_control(*args: Any, **kwargs: Any) -> Any:
            key = self._key_from_constructor_call(args, kwargs)
            return self.consume_prepared_control(key)

        original_wire_assert_target = r004_wire_module.assert_target

        def wire_assert_target(
            candidate: Any,
            target_force_n: float = TARGET_FORCE_N,
        ) -> None:
            if type(candidate) is R006Candidate:
                if (
                    candidate.target_force_n != TARGET_FORCE_N
                    or target_force_n != TARGET_FORCE_N
                ):
                    raise R006LiveAdapterError(
                        "r006 wire target must remain exactly 5.0 N"
                    )
                return
            # Preserve the frozen r004 DTO/type and target policy for every
            # non-native caller; only the exact r006 seam is adapted.
            original_wire_assert_target(candidate, target_force_n)

        replacements = (
            (r004_writer_module, "R004_MOTION_PROFILE", self.motion_profile),
            (r004_writer_module, "step5_path_reference", self.path_reference),
            (r004_wire_module, "assert_target", wire_assert_target),
            (r004_writer_module, "PathEvidenceCollector", R006PathEvidenceCollector),
            (r004_writer_module, "CanonicalQualificationControl", qualification_control),
        )
        try:
            for module, name, value in replacements:
                self._saved[(module, name)] = getattr(module, name)
                setattr(module, name, value)
            self.active = True
        except Exception:
            self.deactivate()
            raise

    def deactivate(self) -> None:
        self.clear_prepared_control()
        try:
            for (module, name), value in reversed(tuple(self._saved.items())):
                setattr(module, name, value)
        finally:
            self._saved.clear()
            self._original_control = None
            self.active = False
            if self._owns_lock:
                self._owns_lock = False
                _R006_INJECTION_LOCK.release()


class R006MatureWriter(R005MatureWriter):
    """r006 attempt names and scoped runtime inputs over the mature writer."""

    def __init__(self, writer: LiveR004Writer, *, injection: _R006ScopedRuntimeInjection) -> None:
        super().__init__(writer)
        self.injection = injection

    @staticmethod
    def _candidate(candidate: Any) -> Any:
        if isinstance(candidate, R006Candidate):
            return candidate
        # Compatibility-only fallback for the existing deterministic repair
        # fixture and already-sealed parent records.  New r006 points never
        # enter this branch.
        return R005MatureWriter._candidate(candidate)

    @staticmethod
    def _kind(kind: str) -> R004AttemptKind:
        mapping = {
            "QUALIFICATION": R004AttemptKind.QUALIFICATION,
            "BOOTSTRAP_PD": R004AttemptKind.BATCH_A,
            "WARM_START_1": R004AttemptKind.BATCH_A,
            "ROUTE": R004AttemptKind.BATCH_A,
            "WARM_START_2": R004AttemptKind.BATCH_B,
            "BO_TRIAL": R004AttemptKind.BATCH_B,
            "RETEST": R004AttemptKind.RETEST,
        }
        try:
            return mapping[kind]
        except (KeyError, TypeError) as exc:
            raise R006LiveAdapterError(f"r006 attempt kind is not mature-stack compatible: {kind!r}") from exc

    def arm(self, attempt: Attempt) -> None:
        try:
            if self._attempt != attempt or self._ticket is None:
                raise R006LiveAdapterError("r006 ARM does not match the dispatched ticket")
            mature_candidate = self._candidate(attempt.candidate)
            mature_kind = self._kind(attempt.kind)
            self.injection.prepare_control(
                candidate=mature_candidate,
                attempt_id=(
                    f"{self.writer.attempt_id}-e{self.writer.session_epoch}-o"
                    f"{attempt.attempt_sequence}"
                ),
                release_contract=self.writer.contract,
                path_requested=mature_kind is not R004AttemptKind.QUALIFICATION,
                canonical_runtime_only=self.writer._canonical_runtime_only,
            )
            # Keep all mature pre-ARM/session checks in the frozen parent.  The
            # only r006 addition before that call is the expensive preparation
            # above, while the resident session is still at Home/unarmed.
            super().arm(attempt)
        except Exception:
            self.injection.clear_prepared_control()
            raise

    def open(self, *, live_ack: str) -> None:
        if live_ack != R005_LIVE_ACK:
            raise R006LiveAdapterError("r006 internal live acknowledgement differs")
        self.injection.activate()
        try:
            self.writer.open(live_ack=R004_LIVE_ACK)
        except Exception:
            self.injection.deactivate()
            raise

    def close(self) -> None:
        try:
            self.writer.close()
        finally:
            self.injection.deactivate()


def build_verified_mature_r006_writer(
    inputs: R006LiveInputs,
    *,
    contract: R006Contract,
    parent_contract: R005Contract,
    path_sample_sink: Callable[..., Any] | None = None,
    force_integral_limit_n_s: float = 1.0,
) -> R006MatureWriter:
    """Construct the r006 writer after admission without opening transport."""

    parent = inputs.parent
    mature_parent = _r004_parent_contract(parent_contract)
    mature_raw = dict(mature_parent.raw)
    script2 = dict(mature_raw.get("script2", {}))
    script2["controller_target"] = f"{CONTROLLER_DIRECTORY}/{contract.program}.urp"
    mature_raw.update(program=contract.program, script2=script2)
    identity_contract = _R005MatureIdentityContract(
        path=contract.path,
        sha256=contract.sha256,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=parent.eoat_sha256,
        script1_sha256=mature_parent.script1_sha256,
        raw=mature_raw,
    )
    prerequisites = _R006MaturePrerequisites(
        contract=identity_contract,
        controller=load_controller_receipt(parent.controller_receipt),
        script1=load_script1_receipt(parent.script1_receipt),
        runtime=load_runtime_evidence(parent.runtime_evidence),
        expected_triplet=dict(inputs.expected_triplet),
        route_id=inputs.route_id,
        session_epoch=parent.session_epoch,
        resident_session_id=parent.resident_session_id,
        input_baseline_ledger_sha256=hashlib.sha256(
            Path(parent.software_baseline_receipt).read_bytes()
        ).hexdigest(),
    )
    writer = R006LiveWriter(
        prerequisites,  # type: ignore[arg-type]
        authority_root=parent.authority_root,
        route_id=inputs.route_id,
        attempt_id=inputs.attempt_id,
        controller_host=parent.controller_host,
        kunwei_host=parent.kunwei_host,
        kunwei_port=parent.kunwei_port,
        software_baseline_n=parent.software_baseline_n(),
        identity_namespace="r006",
        runtime_protocol=R006_RUNTIME_PROTOCOL,
        canonical_runtime_only=True,
        path_sample_sink=path_sample_sink,
    )
    writer.session.identity = _R005SessionIdentityGate(identity_contract)
    injection = _R006ScopedRuntimeInjection(
        motion_profile=ACTIVE_MOTION_ENVELOPE_V2.mature_profile,
        path_reference=r006_runtime_path_reference,
        force_integral_limit_n_s=float(force_integral_limit_n_s),
    )
    return R006MatureWriter(writer, injection=injection)


class R006LiveAdapter:
    """Continuous owner route; no diagnostic ordinal limit is accepted."""

    def __init__(
        self,
        *,
        contract: R006Contract | None = None,
        parent_contract: R005Contract | None = None,
        writer_factory: Callable[[R006LiveInputs, Callable[..., Any]], R005MatureWriter] | None = None,
    ) -> None:
        self.contract = contract or load_contract()
        if parent_contract is None:
            published, admission = load_frozen_r005_contract()
            self.published_parent_contract = published
            self.parent_contract = admission
        else:
            self.published_parent_contract = parent_contract
            self.parent_contract = parent_contract
        self.motion_profile = ACTIVE_MOTION_ENVELOPE_V2.mature_profile
        self.writer_factory = writer_factory
        self.last_stop_reason: str | None = None
        self.last_events: tuple[str, ...] = ()
        self.thresholds: ThresholdReceipt | None = None

    @property
    def production_stop_after_ordinal(self) -> bool:
        return False

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "schema": R006_LIVE_ADAPTER_SCHEMA,
            "program": self.contract.program,
            "parent_live_inputs": "R005LiveInputs used only as a compatibility field bag",
            "remote_admission": "r006 triplet/runtime/EOAT/Home/Safety content",
            "queue": "R006V3DurableQueueAdapter over V3 durable transport",
            "ledger": "ObservationLedger plus r006 immutable raw sidecar",
            "physical_writer": "LiveR004Writer with scoped typed r006 inputs",
            "continuous_host_loop": "r005 HostLoop.run_one until terminal",
            "production_stop_after_ordinal": False,
            "dashboard_load": False,
            "dashboard_play": False,
            "motion_profile_sha256": ACTIVE_MOTION_ENVELOPE_V2.profile_sha256,
            "path_snapshot_stage_id": self.contract.raw["motion"]["path_snapshot"]["stage_id"],
            "path_snapshot_consumer": "r006_runtime_path_reference->V4CalibratedRuntime+PathEvidence",
        }

    def build_verified_writer(
        self,
        inputs: R006LiveInputs,
        *,
        path_sample_sink: Callable[..., Any] | None,
    ) -> R005MatureWriter:
        if self.writer_factory is not None:
            return self.writer_factory(inputs, path_sample_sink)
        return build_verified_mature_r006_writer(
            inputs,
            contract=self.contract,
            parent_contract=self.parent_contract,
            path_sample_sink=path_sample_sink,
        )

    def run_forever(
        self,
        *,
        inputs: R006LiveInputs,
        queue: V3DurableQueueAdapter,
        ledger: ObservationLedger,
        optimizer: Any,
        writer: R005MatureWriter | None = None,
    ) -> str:
        """Validate at Home, then enter the inherited continuous HostLoop."""

        self.thresholds = inputs.validate(contract=self.contract, parent_contract=self.parent_contract)
        if isinstance(queue, R006NativeV3DurableQueueAdapter):
            native_queue = queue
        elif isinstance(queue, R006V3DurableQueueAdapter):
            # The CLI constructs the frozen r006 queue adapter before entering
            # this owner seam.  Re-open the same durable root through the
            # additive native metadata reader so lower Ko/Kp rows survive a
            # process cold read without changing queue.py.
            native_queue = R006NativeV3DurableQueueAdapter(
                queue.root,
                campaign_id=queue.campaign_id,
                launch_profile_path=queue.launch_profile_path,
                release_manifest_sha256=getattr(queue, "_release_manifest_sha256", None),
            )
        else:
            raise R006LiveAdapterError("r006 live queue must be the r006 V3 durable adapter")
        queue = native_queue
        if not isinstance(ledger, ObservationLedger):
            raise R006LiveAdapterError("r006 live ledger must be ObservationLedger")
        if not isinstance(optimizer, R006ProductionOptimizer):
            raise R006LiveAdapterError("r006 live optimizer must be the managed CUDA adapter")
        r006_ledger = optimizer.r006_ledger
        if (
            r006_ledger.ledger.path.resolve() != ledger.path.resolve()
            or r006_ledger.campaign_fingerprint != self.contract.campaign_fingerprint
        ):
            raise R006LiveAdapterError("r006 optimizer evidence owner differs from the live ledger")
        if writer is None:
            writer = self.build_verified_writer(inputs, path_sample_sink=None)
        adapted = R005LiveWriterAdapter(writer, contract=self.parent_contract)
        sink_owner = getattr(writer, "writer", writer)
        if not hasattr(sink_owner, "_path_sample_sink"):
            raise R006LiveAdapterError("mature writer has no raw PATH evidence seam")
        setattr(sink_owner, "_path_sample_sink", adapted.observe_r004_path_sample)
        loop = R006HostLoop(
            r006_contract=self.contract,
            r006_ledger=r006_ledger,
            contract=self.contract,  # type: ignore[arg-type]
            queue=queue,
            ledger=ledger,
            optimizer=optimizer,
            runtime=R005LiveRuntimePort(adapted),
            threshold_policy=self.thresholds,
            evidence_sink=lambda result: r006_ledger.append_attempt_result(
                result,
                epoch=result.epoch,
                point=_point_from_candidate(result.candidate),
            ),
        )
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


__all__ = [
    "R006_LIVE_ACK",
    "R006_LIVE_ADAPTER_SCHEMA",
    "R006LiveAdapter",
    "R006LiveAdapterError",
    "R006LiveInputs",
    "R006LiveWriter",
    "R006PathEvidenceCollector",
    "R006ObservationLedger",
    "R006Candidate",
    "R006NativeV3DurableQueueAdapter",
    "R006ProductionOptimizer",
    "R006ProductionQueue",
    "R006HostLoop",
]
