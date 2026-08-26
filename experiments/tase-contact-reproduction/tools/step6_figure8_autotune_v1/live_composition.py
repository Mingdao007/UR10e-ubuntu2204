"""Live-safe Figure-eight composition over the mature R006/R013 seams.

The module contains no transport, Dashboard, or robot side effects.  It owns
only the profile-specific primitives which must differ from the cycloid:

* the immutable Figure-eight reference and planar frame;
* a 60 s full-coverage evidence collector;
* a minimally widened 10 mm/s tangential profile (the analytic peak is
  4.47213595 mm/s); and
* hard-ellipse-first plus soft CBF filtering in Figure-eight error axes.

All normal/contact/Home primitives remain owned by the mature writer.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from step5d_autotune_v4_r004.path_reference import PATH_STAGE_ID
from step5d_autotune_v4_r004.contracts import (
    HOME_ORIENTATION_TOLERANCE_RAD,
    HOME_POSITION_TOLERANCE_M,
    HOME_Q_TOLERANCE_RAD,
)
from step5d_autotune_v4_r006.live_adapter import (
    R006HomeBindingV1,
    R006HomeStartReceiptV1,
    R006PathEvidenceCollector,
)
from step5d_autotune_v4_r006.motion_profile import ACTIVE_MOTION_ENVELOPE_V2
from step5d_autotune_v4_r004.transport import FreshFrameWaitPolicyV1
from step5d_eoat_profiles import EOATProfile, load_new_eoat_profile
from step5d_autotune_v4_r012.path_cbf_live import (
    R012_HARD_TUBE_AXES_M,
    R012_SOFT_CBF_AXES_M,
    R012GuardStackOutcome,
    R012GuardStop,
    R012PathCbfLiveFilter,
    R012PathCbfOutcome,
    R012PathGuardStack,
)
from step5d_autotune_v4_r012.safety_filter import (
    SafetyFilterResult,
    filter_path_error_twist,
)
from step5d_autotune_v4_r013.path_context import (
    FIGURE8_ALONG_AMPLITUDE_M,
    FIGURE8_ALONG_OMEGA_RAD_S,
    FIGURE8_BIN_WIDTH_S,
    FIGURE8_DURATION_S,
    FIGURE8_FORMAL_BIN_COUNT,
    FIGURE8_FULL_BIN_COUNT,
    FIGURE8_FORMAL_START_S,
    FIGURE8_LATERAL_AMPLITUDE_M,
    FIGURE8_LATERAL_OMEGA_RAD_S,
    FIGURE8_MAX_SPEED_M_S,
    FigureEightPathProviderV1,
    PlanarBasisReceiptV1,
)


FIGURE8_CALIBRATION_HOME_POSE = (
    0.4620551816,
    0.1778825964,
    0.0345,
    3.120752062,
    0.0,
    0.068626833,
)
# Compatibility name for offline geometry helpers.  This is deliberately the
# calibration-only pose; a live campaign must materialize a contact-derived
# Home receipt and may not use this alias as its final Home authority.
FIGURE8_HOME_POSE = FIGURE8_CALIBRATION_HOME_POSE
FIGURE8_ALONG_BASE = (0.0049336434, 0.9999878295, 0.0)
FIGURE8_LATERAL_BASE = (-0.9999878295, 0.0049336434, 0.0)
FIGURE8_TANGENTIAL_CAP_M_S = 0.010
FIGURE8_ANALYTIC_PEAK_SPEED_M_S = math.hypot(
    FIGURE8_ALONG_AMPLITUDE_M * FIGURE8_ALONG_OMEGA_RAD_S,
    FIGURE8_LATERAL_AMPLITUDE_M * FIGURE8_LATERAL_OMEGA_RAD_S,
)
FIGURE8_CALIBRATION_HOME_Z_M = 0.0345
FIGURE8_FINAL_HOME_OFFSET_M = 0.013525311
FIGURE8_CALIBRATION_ACQUISITIONS = 3
FIGURE8_CALIBRATION_HOME_PROFILE_ID = (
    "step6.autotune/figure8-calibration-only-home-v1"
)
FIGURE8_FINAL_HOME_PROFILE_ID = "step6.autotune/figure8-contact-derived-home-v1"
FIGURE8_HOME_PROFILE_ID = FIGURE8_CALIBRATION_HOME_PROFILE_ID
FIGURE8_HOME_START_SCHEMA = "step6.autotune/figure8-home-start-receipt-v1"
FIGURE8_HOME_CALIBRATION_SCHEMA = (
    "step6.autotune/figure8-home-calibration-receipt-v1"
)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class FigureEightHomeProfileV1:
    """EOAT-bound calibration or contact-derived Figure-eight Home."""

    eoat_profile_id: str
    eoat_profile_sha256: str
    home_profile_id: str = FIGURE8_CALIBRATION_HOME_PROFILE_ID
    pose: tuple[float, float, float, float, float, float] = (
        FIGURE8_CALIBRATION_HOME_POSE
    )
    home_calibration_receipt_sha256: str | None = None
    position_tolerance_m: float = HOME_POSITION_TOLERANCE_M
    orientation_tolerance_rad: float = HOME_ORIENTATION_TOLERANCE_RAD
    joint_tolerance_rad: float = HOME_Q_TOLERANCE_RAD

    def __post_init__(self) -> None:
        if self.home_profile_id not in {
            FIGURE8_CALIBRATION_HOME_PROFILE_ID,
            FIGURE8_FINAL_HOME_PROFILE_ID,
        }:
            raise ValueError("Figure-eight Home profile identity differs")
        pose = tuple(float(value) for value in self.pose)
        if (
            len(pose) != 6
            or not all(math.isfinite(value) for value in pose)
            or pose[:2] != FIGURE8_CALIBRATION_HOME_POSE[:2]
            or pose[3:] != FIGURE8_CALIBRATION_HOME_POSE[3:]
            or not 0.028 <= pose[2] <= 0.038
        ):
            raise ValueError("Figure-eight Home pose differs from the frozen XY/orientation frame")
        if self.home_profile_id == FIGURE8_CALIBRATION_HOME_PROFILE_ID:
            if pose != FIGURE8_CALIBRATION_HOME_POSE or self.home_calibration_receipt_sha256 is not None:
                raise ValueError("Figure-eight calibration-only Home is not exact")
        else:
            receipt_sha = self.home_calibration_receipt_sha256
            if (
                not isinstance(receipt_sha, str)
                or len(receipt_sha) != 64
                or any(character not in "0123456789abcdef" for character in receipt_sha)
            ):
                raise ValueError("Figure-eight final Home lacks its calibration receipt SHA-256")
        object.__setattr__(self, "pose", pose)
        if not isinstance(self.eoat_profile_id, str) or not self.eoat_profile_id:
            raise ValueError("Figure-eight Home EOAT profile id is missing")
        if (
            not isinstance(self.eoat_profile_sha256, str)
            or len(self.eoat_profile_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.eoat_profile_sha256)
        ):
            raise ValueError("Figure-eight Home EOAT SHA-256 is invalid")
        if (
            self.position_tolerance_m != HOME_POSITION_TOLERANCE_M
            or self.orientation_tolerance_rad != HOME_ORIENTATION_TOLERANCE_RAD
            or self.joint_tolerance_rad != HOME_Q_TOLERANCE_RAD
        ):
            raise ValueError("Figure-eight Home tolerances differ from the mature route")

    @classmethod
    def from_eoat(
        cls,
        profile: EOATProfile,
        *,
        pose: Sequence[float] = FIGURE8_CALIBRATION_HOME_POSE,
        home_calibration_receipt_sha256: str | None = None,
    ) -> "FigureEightHomeProfileV1":
        final = home_calibration_receipt_sha256 is not None
        return cls(
            eoat_profile_id=profile.profile_id,
            eoat_profile_sha256=profile.profile_sha256,
            home_profile_id=(
                FIGURE8_FINAL_HOME_PROFILE_ID
                if final
                else FIGURE8_CALIBRATION_HOME_PROFILE_ID
            ),
            pose=tuple(float(value) for value in pose),
            home_calibration_receipt_sha256=home_calibration_receipt_sha256,
        )


@dataclass(frozen=True)
class FigureEightHomeReferenceV1:
    """Exact Figure-eight Cartesian Home plus the canary-verified IK branch."""

    pose: tuple[float, float, float, float, float, float] = (
        FIGURE8_CALIBRATION_HOME_POSE
    )
    q: tuple[float, float, float, float, float, float] | None = None

    def __post_init__(self) -> None:
        pose = tuple(float(value) for value in self.pose)
        if (
            len(pose) != 6
            or not all(math.isfinite(value) for value in pose)
            or pose[:2] != FIGURE8_CALIBRATION_HOME_POSE[:2]
            or pose[3:] != FIGURE8_CALIBRATION_HOME_POSE[3:]
            or not 0.028 <= pose[2] <= 0.038
        ):
            raise ValueError("Figure-eight Home reference pose differs")
        object.__setattr__(self, "pose", pose)
        if self.q is not None:
            q = tuple(float(value) for value in self.q)
            if len(q) != 6 or not all(math.isfinite(value) for value in q):
                raise ValueError("Figure-eight Home q must contain six finite values")
            object.__setattr__(self, "q", q)


def figure8_home_profile(
    eoat_profile: EOATProfile | None = None,
    *,
    pose: Sequence[float] = FIGURE8_CALIBRATION_HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
) -> FigureEightHomeProfileV1:
    return FigureEightHomeProfileV1.from_eoat(
        load_new_eoat_profile() if eoat_profile is None else eoat_profile,
        pose=pose,
        home_calibration_receipt_sha256=home_calibration_receipt_sha256,
    )


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        return tuple(
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError(f"Figure-eight calibration evidence is unreadable: {path}") from exc


def derive_figure8_home_calibration_receipt(
    *,
    calibration_run_dir: Path,
) -> dict[str, Any]:
    """Derive final Home from exactly three sealed non-BO contact acquisitions.

    The contact instant is bracketed by the last SEARCH sample and first
    BASELINE sample.  The higher Z is retained, which is the conservative side
    for a Home-clearance calculation.  Nothing from PATH/BO is admitted here.
    """

    run_dir = Path(calibration_run_dir).resolve()
    ledger_path = run_dir / "r006-physical-observations.jsonl"
    search_path = run_dir / "r008-state20-search-trace.jsonl"
    baseline_path = run_dir / "r013-state21-baseline-trace.jsonl"
    controller_path = run_dir / "controller_receipt.json"
    home_start_path = run_dir / "home_start_receipt.json"
    ledger_rows = _read_jsonl(ledger_path)
    search_rows = _read_jsonl(search_path)
    baseline_rows = _read_jsonl(baseline_path)
    try:
        controller = json.loads(controller_path.read_text(encoding="utf-8"))
        home_start = json.loads(home_start_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("Figure-eight calibration identity artifacts are unreadable") from exc

    qualifications = [
        row
        for row in ledger_rows
        if row.get("record_type") == "observation"
    ]
    if len(qualifications) != FIGURE8_CALIBRATION_ACQUISITIONS:
        raise ValueError("Figure-eight Home calibration requires exactly three observations")
    expected_ordinals = tuple(range(1, FIGURE8_CALIBRATION_ACQUISITIONS + 1))
    if tuple(int(row.get("attempt_sequence", -1)) for row in qualifications) != expected_ordinals:
        raise ValueError("Figure-eight Home calibration ordinals are not exactly 1..3")
    required_true = (
        "binding_ok",
        "contact_gate",
        "qualification_passed",
        "return_gate",
        "safe_return",
        "safety_gate",
        "sealed",
        "timing_gate",
    )
    for row in qualifications:
        if (
            row.get("kind") != "QUALIFICATION"
            or row.get("objective") is not None
            or row.get("force_objective") is not None
            or not all(row.get(name) is True for name in required_true)
        ):
            raise ValueError("Figure-eight Home calibration includes a non-qualified or trainable row")

    def grouped(rows: Sequence[Mapping[str, Any]]) -> dict[int, list[Mapping[str, Any]]]:
        result: dict[int, list[Mapping[str, Any]]] = {}
        for row in rows:
            ordinal = int(row.get("attempt_ordinal", -1))
            if ordinal in expected_ordinals:
                result.setdefault(ordinal, []).append(row)
        return result

    search_by_ordinal = grouped(search_rows)
    baseline_by_ordinal = grouped(baseline_rows)
    contacts: list[dict[str, Any]] = []
    for ordinal in expected_ordinals:
        search = search_by_ordinal.get(ordinal, [])
        baseline = baseline_by_ordinal.get(ordinal, [])
        if not search or not baseline:
            raise ValueError("Figure-eight Home calibration lacks a SEARCH/BASELINE transition")
        search_pose = tuple(float(value) for value in search[-1]["tcp_pose_m_rad"])
        baseline_pose = tuple(float(value) for value in baseline[0]["tcp_pose_m_rad"])
        if len(search_pose) != 6 or len(baseline_pose) != 6:
            raise ValueError("Figure-eight Home calibration contact pose is incomplete")
        bracket_m = abs(search_pose[2] - baseline_pose[2])
        if bracket_m > 0.001:
            raise ValueError("Figure-eight Home calibration contact bracket exceeds 1 mm")
        contacts.append(
            {
                "attempt_sequence": ordinal,
                "epoch": int(qualifications[ordinal - 1]["epoch"]),
                "run_kind": "NON_BO",
                "search_last_z_m": search_pose[2],
                "baseline_first_z_m": baseline_pose[2],
                "contact_confirm_z_m": max(search_pose[2], baseline_pose[2]),
                "transition_bracket_m": bracket_m,
                "qualification_row_sha256": str(qualifications[ordinal - 1]["row_sha256"]),
            }
        )
    contact_z = tuple(float(row["contact_confirm_z_m"]) for row in contacts)
    final_z = max(contact_z) + FIGURE8_FINAL_HOME_OFFSET_M
    final_pose = (
        *FIGURE8_CALIBRATION_HOME_POSE[:2],
        final_z,
        *FIGURE8_CALIBRATION_HOME_POSE[3:],
    )
    if not 0.028 <= final_z <= 0.038:
        raise ValueError("Figure-eight derived final Home is outside the reviewed no-contact workspace")
    triplet = {
        role: str(controller.get(f"{role}_sha256", ""))
        for role in ("script", "txt", "urp")
    }
    if any(len(value) != 64 for value in triplet.values()):
        raise ValueError("Figure-eight Home calibration package identity is incomplete")
    if (
        home_start.get("home_profile_id") != FIGURE8_CALIBRATION_HOME_PROFILE_ID
        or tuple(home_start.get("home_pose", ())) != FIGURE8_CALIBRATION_HOME_POSE
        or home_start.get("calibration_only") is not True
    ):
        raise ValueError("Figure-eight Home calibration did not start from calibration-only Home")
    body = {
        "schema": FIGURE8_HOME_CALIBRATION_SCHEMA,
        "version": 1,
        "passed": True,
        "source_run_dir": str(run_dir),
        "source_ledger_path": str(ledger_path),
        "source_ledger_sha256": _file_sha256(ledger_path),
        "source_search_trace_path": str(search_path),
        "source_search_trace_sha256": _file_sha256(search_path),
        "source_baseline_trace_path": str(baseline_path),
        "source_baseline_trace_sha256": _file_sha256(baseline_path),
        "source_controller_triplet_sha256": triplet,
        "source_home_start_receipt_sha256": _file_sha256(home_start_path),
        "calibration_home_pose": list(FIGURE8_CALIBRATION_HOME_POSE),
        "contact_acquisition_count": FIGURE8_CALIBRATION_ACQUISITIONS,
        "contact_acquisitions": contacts,
        "contact_confirm_z_m": list(contact_z),
        "final_home_offset_m": FIGURE8_FINAL_HOME_OFFSET_M,
        "final_home_rule": "max(contact_confirm_z_m)+0.013525311 m",
        "final_home_pose": list(final_pose),
        "home_profile_id": FIGURE8_FINAL_HOME_PROFILE_ID,
        "launchable_after_final_package_canary_and_trial_admission": True,
    }
    return {**body, "receipt_sha256": _canonical_sha256(body)}


def load_figure8_home_calibration_receipt(path: Path) -> dict[str, Any]:
    receipt_path = Path(path).resolve()
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("Figure-eight Home calibration receipt is unreadable") from exc
    if not isinstance(value, Mapping) or value.get("schema") != FIGURE8_HOME_CALIBRATION_SCHEMA:
        raise ValueError("Figure-eight Home calibration receipt schema differs")
    derived = derive_figure8_home_calibration_receipt(
        calibration_run_dir=Path(str(value.get("source_run_dir", "")))
    )
    if dict(value) != derived:
        raise ValueError("Figure-eight Home calibration receipt differs from cold source evidence")
    return derived


def derive_figure8_home_start_receipt(
    *,
    canary_evidence_path: Path,
    frame_receipt_path: Path,
    raw_trace_path: Path,
    home_calibration_receipt_path: Path | None = None,
    calibration_only: bool = False,
    eoat_profile: EOATProfile | None = None,
) -> dict[str, Any]:
    """Cold-derive the mature writer's Home-start evidence from the canary."""

    evidence_path = Path(canary_evidence_path).resolve()
    frame_path = Path(frame_receipt_path).resolve()
    trace_path = Path(raw_trace_path).resolve()
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        frame = json.loads(frame_path.read_text(encoding="utf-8"))
        trace_rows = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("Figure-eight Home-start source evidence is unreadable") from exc
    calibration = None
    if calibration_only:
        if home_calibration_receipt_path is not None:
            raise ValueError("calibration-only Home cannot bind a final calibration receipt")
        expected_home_pose = FIGURE8_CALIBRATION_HOME_POSE
        home_profile_id = FIGURE8_CALIBRATION_HOME_PROFILE_ID
    else:
        if home_calibration_receipt_path is None:
            raise ValueError("final Figure-eight Home requires a contact calibration receipt")
        calibration = load_figure8_home_calibration_receipt(
            home_calibration_receipt_path
        )
        expected_home_pose = tuple(float(value) for value in calibration["final_home_pose"])
        home_profile_id = FIGURE8_FINAL_HOME_PROFILE_ID
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("passed") is not True
        or evidence.get("contact_executed") is not False
        or evidence.get("force_control_executed") is not False
        or not isinstance(frame, Mapping)
        or frame.get("live_canary_passed") is not True
        or tuple(frame.get("home_pose", ())) != expected_home_pose
        or not trace_rows
    ):
        raise ValueError("Figure-eight Home-start source did not pass the no-contact contract")
    expected_calibration_sha = (
        None if calibration is None else str(calibration["receipt_sha256"])
    )
    if frame.get("home_calibration_receipt_sha256") != expected_calibration_sha:
        raise ValueError(
            "Figure-eight no-contact canary Home calibration authority differs"
        )
    expected_trace_sha = str(frame.get("source_raw_trace_sha256", ""))
    if _file_sha256(trace_path) != expected_trace_sha:
        raise ValueError("Figure-eight Home-start raw trace hash differs")
    if evidence.get("package_sha256") != frame.get("source_package_sha256"):
        raise ValueError("Figure-eight Home-start package identity differs")
    final = trace_rows[-1]
    final_pose = tuple(float(value) for value in final.get("actual_TCP_pose", ()))
    final_q = tuple(float(value) for value in final.get("actual_q", ()))
    if len(final_pose) != 6 or len(final_q) != 6:
        raise ValueError("Figure-eight Home-start terminal state is incomplete")
    final_home = evidence.get("final_home")
    if not isinstance(final_home, Mapping) or tuple(final_home.get("actual_pose", ())) != final_pose:
        raise ValueError("Figure-eight Home-start terminal pose differs from canary receipt")
    postflight = evidence.get("postflight")
    if not isinstance(postflight, Mapping) or "NORMAL" not in str(postflight.get("safetymode", "")):
        raise ValueError("Figure-eight Home-start final Safety state differs")
    try:
        observed_at_s = datetime.fromisoformat(str(evidence["captured_at"])).timestamp()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Figure-eight Home-start timestamp is invalid") from exc
    eoat = load_new_eoat_profile() if eoat_profile is None else eoat_profile
    package = frame.get("source_package_sha256")
    if not isinstance(package, Mapping):
        raise ValueError("Figure-eight Home-start package hashes are incomplete")
    body = {
        "schema": FIGURE8_HOME_START_SCHEMA,
        "version": 1,
        "passed": True,
        "home_profile_id": home_profile_id,
        "home_pose": list(expected_home_pose),
        "calibration_only": bool(calibration_only),
        "home_calibration_receipt_path": (
            None
            if home_calibration_receipt_path is None
            else str(Path(home_calibration_receipt_path).resolve())
        ),
        "home_calibration_receipt_sha256": (
            None if calibration is None else str(calibration["receipt_sha256"])
        ),
        "source_evidence_path": str(evidence_path),
        "source_evidence_sha256": _file_sha256(evidence_path),
        "source_frame_path": str(frame_path),
        "source_frame_sha256": _file_sha256(frame_path),
        "source_raw_trace_path": str(trace_path),
        "source_raw_trace_sha256": expected_trace_sha,
        "script_sha256": str(package.get("script", "")),
        "observed_at_s": observed_at_s,
        "final_pose": list(final_pose),
        "final_q": list(final_q),
        "stationary": True,
        "safety_mode": "NORMAL",
        "eoat_identity_sha256": eoat.profile_sha256,
    }
    return {**body, "receipt_sha256": _canonical_sha256(body)}


def load_figure8_home_binding(path: Path) -> R006HomeBindingV1:
    """Cold-read and rederive a Figure-eight Home binding before transport."""

    receipt_path = Path(path).resolve()
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("Figure-eight Home-start receipt is unreadable") from exc
    if not isinstance(value, Mapping) or value.get("schema") != FIGURE8_HOME_START_SCHEMA:
        raise ValueError("Figure-eight Home-start receipt schema differs")
    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if value.get("receipt_sha256") != _canonical_sha256(body):
        raise ValueError("Figure-eight Home-start receipt hash differs")
    derived = derive_figure8_home_start_receipt(
        canary_evidence_path=Path(str(value.get("source_evidence_path", ""))),
        frame_receipt_path=Path(str(value.get("source_frame_path", ""))),
        raw_trace_path=Path(str(value.get("source_raw_trace_path", ""))),
        home_calibration_receipt_path=(
            None
            if value.get("home_calibration_receipt_path") is None
            else Path(str(value["home_calibration_receipt_path"]))
        ),
        calibration_only=value.get("calibration_only") is True,
    )
    if dict(value) != derived:
        raise ValueError("Figure-eight Home-start receipt differs from cold source evidence")
    typed = R006HomeStartReceiptV1(
        receipt_sha256=str(value["receipt_sha256"]),
        script_sha256=str(value["script_sha256"]),
        observed_at_s=float(value["observed_at_s"]),
        final_pose=tuple(value["final_pose"]),
        final_q=tuple(value["final_q"]),
        stationary=value.get("stationary") is True,
        safety_mode=str(value.get("safety_mode", "")),
        eoat_identity_sha256=str(value["eoat_identity_sha256"]),
        home_profile_id=str(value["home_profile_id"]),
    )
    return R006HomeBindingV1(
        profile=figure8_home_profile(
            pose=tuple(value["home_pose"]),
            home_calibration_receipt_sha256=value.get(
                "home_calibration_receipt_sha256"
            ),
        ),
        reference_type=FigureEightHomeReferenceV1,
        entry_receipt=typed,
    )


def figure8_path_provider(
    *,
    anchor_pose: Sequence[float] = FIGURE8_CALIBRATION_HOME_POSE,
) -> FigureEightPathProviderV1:
    """Build the path provider at the exact Home bound to this lineage.

    The XY/orientation frame is fixed, while final Home Z is contact-derived.
    Keeping the anchor explicit prevents the correction runtime and campaign
    fingerprint from silently retaining the calibration-only Z.
    """

    return FigureEightPathProviderV1(
        anchor_pose_base=tuple(float(value) for value in anchor_pose),
        basis_receipt=PlanarBasisReceiptV1(
            along_base=FIGURE8_ALONG_BASE,
            lateral_base=FIGURE8_LATERAL_BASE,
        ),
    )


_FIGURE8_PROVIDER = figure8_path_provider()


def figure8_runtime_path_reference(
    stage_id: str,
    pose_xy: tuple[float, float],
    elapsed_s: float,
) -> Mapping[str, Any]:
    """Return the exact mapping consumed by the mature calibrated runtime."""

    if stage_id != PATH_STAGE_ID:
        raise ValueError("Figure-eight runtime path stage differs")
    if len(tuple(pose_xy)) != 2:
        raise ValueError("Figure-eight pose_xy must contain two values")
    pose = tuple(float(value) for value in pose_xy)
    if not all(math.isfinite(value) for value in pose):
        raise ValueError("Figure-eight pose_xy must be finite")
    time_s = min(max(float(elapsed_s), 0.0), FIGURE8_DURATION_S)
    sample = _FIGURE8_PROVIDER.sample(time_s)
    desired = sample.desired_pose_base[:2]
    velocity = sample.desired_twist_base[:2]
    return {
        "stage_id": PATH_STAGE_ID,
        "path_id": sample.path_id,
        "progress": sample.normalized_progress,
        "path_time_s": sample.path_time_s,
        "phase_rad": sample.phase_rad,
        "desired_xy": desired,
        "desired_velocity_xy": velocity,
        "path_error_xy": (desired[0] - pose[0], desired[1] - pose[1]),
        "local": {
            "path_time_s": sample.path_time_s,
            "phase_rad": sample.phase_rad,
            "normalized_progress": sample.normalized_progress,
            "speed_m_s": sample.scalar_speed_m_s,
            "signed_acceleration_m_s2": sample.signed_tangential_acceleration_m_s2,
            "signed_curvature_m_inv": sample.signed_planar_curvature_m_inv,
        },
        "path_identity": sample.identity_receipt.as_dict(),
    }


def figure8_motion_profile() -> Any:
    """Clone the typed mature profile and widen only the tangential cap."""

    profile = copy.copy(ACTIVE_MOTION_ENVELOPE_V2.mature_profile)
    object.__setattr__(profile, "xy_path_speed_m_s", FIGURE8_TANGENTIAL_CAP_M_S)
    if (
        float(profile.total_linear_cap_m_s) < FIGURE8_TANGENTIAL_CAP_M_S
        or float(profile.qdot_cap_rad_s) <= 0.0
        or float(profile.host_slew_rad_s2) <= 0.0
    ):
        raise ValueError("Figure-eight mature motion profile cannot contain the analytic path")
    return profile


def figure8_motion_profile_receipt() -> dict[str, Any]:
    parent = ACTIVE_MOTION_ENVELOPE_V2.as_dict
    payload = {
        "schema": "step6.autotune/figure8-motion-envelope-v1",
        "version": 1,
        "parent_profile_sha256": ACTIVE_MOTION_ENVELOPE_V2.profile_sha256,
        "path_identity_sha256": _FIGURE8_PROVIDER.identity_receipt.sha256,
        "analytic_peak_speed_m_s": FIGURE8_ANALYTIC_PEAK_SPEED_M_S,
        "tangential_cap_m_s": FIGURE8_TANGENTIAL_CAP_M_S,
        "unchanged_parent_caps": {
            key: value
            for key, value in parent["active_path_caps"].items()
            if key != "xy_tangential_m_s"
        },
    }
    return {**payload, "receipt_sha256": _canonical_sha256(payload)}


def figure8_fresh_frame_wait_policy() -> FreshFrameWaitPolicyV1:
    return FreshFrameWaitPolicyV1.v5()


class FigureEightPathEvidenceCollectorV1(R006PathEvidenceCollector):
    """Mature 550-bin envelope over the independent 60 s/600-bin raw sink."""

    # The mature AttemptEvidence contract is fixed at the formal 550-bin
    # envelope.  Figure-eight's complete 600-bin raw path remains owned by
    # make_metric_result(_path_samples) and is not routed through this field.
    REQUIRED_BINS = FIGURE8_FORMAL_BIN_COUNT
    REQUIRED_DURATION_S = FIGURE8_DURATION_S


def _base_to_path_xy(value: Sequence[float]) -> tuple[float, float]:
    basis = _FIGURE8_PROVIDER.basis_receipt
    along = basis.along_base
    lateral = basis.lateral_base
    return (
        float(value[0]) * along[0] + float(value[1]) * along[1],
        float(value[0]) * lateral[0] + float(value[1]) * lateral[1],
    )


def _path_to_base_xy(value: Sequence[float]) -> tuple[float, float]:
    basis = _FIGURE8_PROVIDER.basis_receipt
    along = basis.along_base
    lateral = basis.lateral_base
    return (
        float(value[0]) * along[0] + float(value[1]) * lateral[0],
        float(value[0]) * along[1] + float(value[1]) * lateral[1],
    )


def _reference_error_path(
    actual_tcp_pose: Sequence[float],
    path_time_s: float,
    *,
    path_reference: Callable[[str, tuple[float, float], float], Mapping[str, Any]] = (
        figure8_runtime_path_reference
    ),
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    if len(actual_tcp_pose) < 2:
        raise ValueError("Figure-eight PATH pose is incomplete")
    reference = path_reference(
        PATH_STAGE_ID,
        (float(actual_tcp_pose[0]), float(actual_tcp_pose[1])),
        float(path_time_s),
    )
    error_base = (
        float(actual_tcp_pose[0]) - float(reference["desired_xy"][0]),
        float(actual_tcp_pose[1]) - float(reference["desired_xy"][1]),
    )
    return (
        _base_to_path_xy(error_base),
        _base_to_path_xy(reference["desired_velocity_xy"]),
        tuple(reference["desired_xy"]),
    )


@dataclass
class FigureEightPathCbfLiveFilterV1(R012PathCbfLiveFilter):
    """The reviewed R012 QP evaluated in the Figure-eight moving frame."""

    path_reference: Callable[
        [str, tuple[float, float], float], Mapping[str, Any]
    ] = figure8_runtime_path_reference

    def apply(
        self,
        desired_twist: Sequence[float],
        *,
        mode: str,
        actual_tcp_pose: Sequence[float],
        path_time_s: float,
    ) -> R012PathCbfOutcome:
        twist = tuple(float(desired_twist[index]) for index in range(6))
        if str(mode).strip().lower() != "path":
            return R012PathCbfOutcome(twist, False, "active", None)  # type: ignore[arg-type]
        try:
            if not math.isfinite(float(path_time_s)):
                raise ValueError("invalid Figure-eight PATH time")
            error_path, reference_velocity_path, _reference_xy = _reference_error_path(
                actual_tcp_pose,
                path_time_s,
                path_reference=self.path_reference,
            )
            nominal_path = _base_to_path_xy(twist[:2])
            path_twist = nominal_path + twist[2:]
            axes = self.config.tightened_axes_m
            rho = sum((error_path[index] / axes[index]) ** 2 for index in range(2))
            if rho < self.config.engage_deadband:
                self.previous_path_command_m_s = nominal_path
                result = SafetyFilterResult(
                    True,
                    nominal_path,
                    1.0 - rho,
                    (),
                    0,
                    0.0,
                    0.0,
                    None,
                    error_xy_m=error_path,
                )
                return R012PathCbfOutcome(twist, False, "active", result)  # type: ignore[arg-type]
            actual_path = error_path
            reference_path = (0.0, 0.0)
            filtered_path, result = filter_path_error_twist(
                actual_path,
                reference_path,
                reference_velocity_path,
                path_twist,
                self.previous_path_command_m_s,
                state_age_s=0.0,
                config=self.config,
            )
        except Exception:
            self.previous_path_command_m_s = (0.0, 0.0)
            fallback = (0.0, 0.0) + twist[2:]
            result = SafetyFilterResult(
                False,
                (0.0, 0.0),
                None,
                (),
                0,
                0.0,
                0.0,
                "invalid_figure8_live_input",
            )
            return R012PathCbfOutcome(fallback, True, "active", result)  # type: ignore[arg-type]
        if result.valid:
            self.previous_path_command_m_s = tuple(result.command_m_s)
            base_xy = _path_to_base_xy(filtered_path[:2])
        else:
            self.previous_path_command_m_s = (0.0, 0.0)
            base_xy = (0.0, 0.0)
        output = base_xy + tuple(filtered_path[2:])
        applied = (not result.valid) or result.intervention_norm_m_s > 0.0
        return R012PathCbfOutcome(output, applied, "active", result)  # type: ignore[arg-type]


@dataclass
class FigureEightPathGuardStackV1(R012PathGuardStack):
    """Hard outer ellipse first, then Figure-eight-relative soft CBF."""

    soft_filter: FigureEightPathCbfLiveFilterV1 = field(
        default_factory=FigureEightPathCbfLiveFilterV1
    )
    path_reference: Callable[
        [str, tuple[float, float], float], Mapping[str, Any]
    ] = figure8_runtime_path_reference

    def __post_init__(self) -> None:
        super().__post_init__()
        self.soft_filter.path_reference = self.path_reference

    def apply(
        self,
        desired_twist: Sequence[float],
        *,
        mode: str,
        actual_tcp_pose: Sequence[float],
        path_time_s: float,
        state_age_s: float = 0.0,
    ) -> R012GuardStackOutcome:
        twist = tuple(float(desired_twist[index]) for index in range(6))
        if str(mode).strip().lower() != "path":
            soft = self.soft_filter.apply(
                twist,
                mode=mode,
                actual_tcp_pose=actual_tcp_pose,
                path_time_s=path_time_s,
            )
            return R012GuardStackOutcome(twist, soft, None, False)
        try:
            age = float(state_age_s)
            if (
                not math.isfinite(age)
                or age < 0.0
                or age > self.soft_filter.config.max_state_age_s
            ):
                raise ValueError("stale Figure-eight PATH state")
            error_path, _reference_velocity, _reference_xy = _reference_error_path(
                actual_tcp_pose,
                path_time_s,
                path_reference=self.path_reference,
            )
            hard_value = sum(
                (error_path[index] / R012_HARD_TUBE_AXES_M[index]) ** 2
                for index in range(2)
            )
        except Exception as exc:
            soft = R012PathCbfOutcome(twist, True, "active", None)
            outcome = R012GuardStackOutcome(
                (0.0, 0.0, twist[2], twist[3], twist[4], twist[5]),
                soft,
                None,
                True,
                f"figure8_hard_guard_fail_closed:{exc}",
            )
            raise R012GuardStop(outcome)
        if hard_value > 1.0:
            soft = R012PathCbfOutcome(twist, False, "active", None)
            outcome = R012GuardStackOutcome(
                (0.0, 0.0, twist[2], twist[3], twist[4], twist[5]),
                soft,
                hard_value,
                True,
                "figure8_hard_outer_ellipse_breach",
            )
            raise R012GuardStop(outcome)
        soft = self.soft_filter.apply(
            twist,
            mode=mode,
            actual_tcp_pose=actual_tcp_pose,
            path_time_s=path_time_s,
        )
        return R012GuardStackOutcome(
            soft.desired_twist, soft, hard_value, False
        )


def live_composition_receipt() -> dict[str, Any]:
    payload = {
        "schema": "step6.autotune/figure8-live-composition-v1",
        "version": 1,
        "path_identity": _FIGURE8_PROVIDER.identity_receipt.as_dict(),
        "duration_s": FIGURE8_DURATION_S,
        "full_bin_count": FIGURE8_FULL_BIN_COUNT,
        "formal_window_s": [FIGURE8_FORMAL_START_S, FIGURE8_DURATION_S],
        "formal_bin_count": FIGURE8_FORMAL_BIN_COUNT,
        "theoretical_max_speed_m_s": FIGURE8_MAX_SPEED_M_S,
        "home_rule": {
            "calibration_home_z_m": FIGURE8_CALIBRATION_HOME_Z_M,
            "final_home_offset_m": FIGURE8_FINAL_HOME_OFFSET_M,
            "final_home_rule": "max(contact_confirm_z_m)+0.013525311 m",
            "calibration_acquisitions": FIGURE8_CALIBRATION_ACQUISITIONS,
        },
        "soft_axes_m": list(R012_SOFT_CBF_AXES_M),
        "hard_axes_m": list(R012_HARD_TUBE_AXES_M),
        "guard_order": ["hard_outer_ellipse", "soft_cbf_qp"],
        "motion_profile": figure8_motion_profile_receipt(),
        "fresh_frame_wait_policy": figure8_fresh_frame_wait_policy().as_dict(),
    }
    return {**payload, "receipt_sha256": _canonical_sha256(payload)}


__all__ = [
    "FIGURE8_ALONG_BASE",
    "FIGURE8_ANALYTIC_PEAK_SPEED_M_S",
    "FIGURE8_CALIBRATION_HOME_POSE",
    "FIGURE8_CALIBRATION_ACQUISITIONS",
    "FIGURE8_CALIBRATION_HOME_Z_M",
    "FIGURE8_DURATION_S",
    "FIGURE8_FINAL_HOME_OFFSET_M",
    "FIGURE8_FULL_BIN_COUNT",
    "FIGURE8_HOME_POSE",
    "FIGURE8_CALIBRATION_HOME_PROFILE_ID",
    "FIGURE8_FINAL_HOME_PROFILE_ID",
    "FIGURE8_HOME_CALIBRATION_SCHEMA",
    "FIGURE8_HOME_PROFILE_ID",
    "FIGURE8_HOME_START_SCHEMA",
    "FIGURE8_LATERAL_BASE",
    "FIGURE8_TANGENTIAL_CAP_M_S",
    "FigureEightPathCbfLiveFilterV1",
    "FigureEightHomeProfileV1",
    "FigureEightHomeReferenceV1",
    "FigureEightPathEvidenceCollectorV1",
    "FigureEightPathGuardStackV1",
    "figure8_motion_profile",
    "figure8_home_profile",
    "derive_figure8_home_start_receipt",
    "derive_figure8_home_calibration_receipt",
    "load_figure8_home_calibration_receipt",
    "load_figure8_home_binding",
    "figure8_motion_profile_receipt",
    "figure8_fresh_frame_wait_policy",
    "figure8_path_provider",
    "figure8_runtime_path_reference",
    "live_composition_receipt",
]
