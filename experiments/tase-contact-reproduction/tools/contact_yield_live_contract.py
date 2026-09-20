"""Typed native TP/Home contract for the yield live entry.

This is not the R004 RNN contract with the program name rewritten.  Identity
is step5d_contact_six_qp_v1 / step5d_contact_home_v1: protocol 618001, output
32=606006, 33=20, 34=618001, qdot 0.05, raw 20 N / 2 Nm, and the corrected
Home attitude.  SHA-derived receipt limbs remain the software contract;
physical wire echoes use the readable (20, 618001) pair.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from contact_yield_method_registry import CONTACT_PROGRAM, HOME_PROGRAM
from contact_yield_protocol import PERIOD_S
from step5d_autotune_v4_r004.contracts import (
    HOME_ORIENTATION_TOLERANCE_RAD,
    HOME_POSITION_TOLERANCE_M,
    HOME_Q_TOLERANCE_RAD,
    runtime_identity_limbs,
)
from step5d_autotune_v4_r006.live_adapter import (
    R006HomeBindingV1,
    R006HomeStartReceiptV1,
    R006LiveAdapterError,
)
from step5d_eoat_profiles import load_new_eoat_profile


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT_PATH = EXPERIMENT_ROOT / "config" / "yield_live_contract_v1.json"
PACKAGE_DIR = EXPERIMENT_ROOT / "programs" / "step5" / "step5d" / "contact-six-qp"
SCHEMA = "yield-live-contract-v1"
RUNTIME_PROTOCOL = 606006
RUNTIME_REVISION = 20
RUNTIME_EXTENSION = 618001
READABLE_RUNTIME_IDENTITY = (RUNTIME_REVISION, RUNTIME_EXTENSION)
HOME_PROFILE_ID = "yield-live-entry/contact-home-v1"


class YieldLiveContractError(RuntimeError):
    """Native live contract or package identity failed closed."""


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise YieldLiveContractError(f"package file is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise YieldLiveContractError(f"{name} must be an object")
    return value


def _finite6(value: Any, name: str) -> tuple[float, float, float, float, float, float]:
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise YieldLiveContractError(f"{name} must be a length-6 finite vector") from exc
    if len(vector) != 6 or not all(math.isfinite(item) for item in vector):
        raise YieldLiveContractError(f"{name} must be a length-6 finite vector")
    return vector  # type: ignore[return-value]


def load_live_contract_document(path: Path | str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else DEFAULT_CONTRACT_PATH
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YieldLiveContractError(f"native live contract is unreadable: {exc}") from exc
    payload = dict(_require_mapping(document, "native live contract"))
    if payload.get("schema") != SCHEMA:
        raise YieldLiveContractError("native live contract schema differs")
    if payload.get("program") != CONTACT_PROGRAM:
        raise YieldLiveContractError("native live contract program is not step5d_contact_six_qp_v1")
    if payload.get("home_program") != HOME_PROGRAM:
        raise YieldLiveContractError("native live contract Home program is not step5d_contact_home_v1")
    if int(payload.get("protocol")) != RUNTIME_EXTENSION:
        raise YieldLiveContractError("native live contract protocol is not 618001")
    if int(payload.get("runtime_protocol")) != RUNTIME_PROTOCOL:
        raise YieldLiveContractError("native live contract runtime protocol is not 606006")
    if int(payload.get("runtime_revision")) != RUNTIME_REVISION:
        raise YieldLiveContractError("native live contract revision is not 19")
    if int(payload.get("runtime_extension_protocol")) != RUNTIME_EXTENSION:
        raise YieldLiveContractError("native live contract extension protocol is not 618001")
    identity = tuple(int(item) for item in payload.get("readable_runtime_identity") or ())
    if identity != READABLE_RUNTIME_IDENTITY:
        raise YieldLiveContractError("native readable runtime identity is not (20, 618001)")
    task = _require_mapping(payload.get("task"), "task")
    if not math.isclose(float(task.get("period_s")), PERIOD_S, rel_tol=0.0, abs_tol=1e-12):
        raise YieldLiveContractError("native live contract period is not the formal PATH period")
    if float(task.get("normal_force_n")) != 5.0:
        raise YieldLiveContractError("native live contract force is not 5 N")
    guards = _require_mapping(payload.get("guards"), "guards")
    if (
        float(guards.get("raw_force_n")) != 20.0
        or float(guards.get("raw_torque_nm")) != 2.0
        or float(guards.get("qdot_cap_rad_s")) != 0.05
        or float(guards.get("search_speed_m_s")) != 0.0002
        or float(guards.get("search_travel_max_m")) != 0.015
    ):
        raise YieldLiveContractError("native live contract guards differ")
    return payload


def package_triplet() -> dict[str, str]:
    return {
        role: _sha256_file(PACKAGE_DIR / f"{CONTACT_PROGRAM}.{role}")
        for role in ("script", "txt", "urp")
    }


def home_script_sha256() -> str:
    return _sha256_file(PACKAGE_DIR / f"{HOME_PROGRAM}.script")


@dataclass(frozen=True)
class YieldLiveIdentityContract:
    path: Path
    sha256: str
    campaign_fingerprint: str
    eoat_sha256: str
    script1_sha256: Mapping[str, str]
    raw: Mapping[str, Any]
    readable_runtime_identity: tuple[int, int]
    home_pose: tuple[float, float, float, float, float, float]
    triplet: Mapping[str, str]

    @property
    def program(self) -> str:
        return str(self.raw["program"])


def load_identity_contract(path: Path | str | None = None) -> YieldLiveIdentityContract:
    config_path = Path(path) if path is not None else DEFAULT_CONTRACT_PATH
    document = load_live_contract_document(config_path)
    eoat = load_new_eoat_profile()
    triplet = package_triplet()
    home_sha = home_script_sha256()
    home_pose = _finite6(document["home_pose_m_rad"], "home_pose_m_rad")
    raw = {
        "program": CONTACT_PROGRAM,
        "script2": {
            "controller_target": document["controller_target"],
            "program": CONTACT_PROGRAM,
            "fixed_home_pose_m_rad": list(home_pose),
        },
        "live_boundary": dict(document["live_boundary"]),
        "invariants": {"eoat_profile": {"sha256": eoat.profile_sha256}},
        "guards": dict(document["guards"]),
        "task": dict(document["task"]),
        "runtime_protocol": RUNTIME_PROTOCOL,
        "readable_runtime_identity": list(READABLE_RUNTIME_IDENTITY),
        "identity_projection": document["identity_projection"],
    }
    sha256 = _sha256_file(config_path)
    fingerprint = _sha256_json(
        {
            "program": CONTACT_PROGRAM,
            "home_program": HOME_PROGRAM,
            "protocol": RUNTIME_EXTENSION,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "readable_runtime_identity": list(READABLE_RUNTIME_IDENTITY),
            "triplet": triplet,
            "home_script": home_sha,
            "home_pose": list(home_pose),
        }
    )
    return YieldLiveIdentityContract(
        path=config_path,
        sha256=sha256,
        campaign_fingerprint=fingerprint,
        eoat_sha256=eoat.profile_sha256,
        script1_sha256={"script": home_sha},
        raw=raw,
        readable_runtime_identity=READABLE_RUNTIME_IDENTITY,
        home_pose=home_pose,
        triplet=triplet,
    )


def software_identity_limbs(contract: YieldLiveIdentityContract) -> tuple[int, int]:
    return runtime_identity_limbs(
        contract.raw["program"], contract.sha256, contract.campaign_fingerprint
    )


@dataclass(frozen=True)
class ContactHomeReferenceV1:
    pose: tuple[float, float, float, float, float, float]
    q: tuple[float, float, float, float, float, float] | None = None

    def __post_init__(self) -> None:
        pose = _finite6(self.pose, "contact Home pose")
        object.__setattr__(self, "pose", pose)
        if self.q is not None:
            object.__setattr__(self, "q", _finite6(self.q, "contact Home q"))


@dataclass(frozen=True)
class ContactHomeProfileV1:
    eoat_profile_id: str
    eoat_profile_sha256: str
    pose: tuple[float, float, float, float, float, float]
    home_profile_id: str = HOME_PROFILE_ID
    position_tolerance_m: float = HOME_POSITION_TOLERANCE_M
    orientation_tolerance_rad: float = HOME_ORIENTATION_TOLERANCE_RAD
    joint_tolerance_rad: float = HOME_Q_TOLERANCE_RAD

    def __post_init__(self) -> None:
        object.__setattr__(self, "pose", _finite6(self.pose, "contact Home profile pose"))
        if self.home_profile_id != HOME_PROFILE_ID:
            raise YieldLiveContractError("contact Home profile identity differs")
        if (
            self.position_tolerance_m != HOME_POSITION_TOLERANCE_M
            or self.orientation_tolerance_rad != HOME_ORIENTATION_TOLERANCE_RAD
            or self.joint_tolerance_rad != HOME_Q_TOLERANCE_RAD
        ):
            raise YieldLiveContractError("contact Home tolerances differ from the mature gates")


def contact_home_binding(
    *,
    contract: YieldLiveIdentityContract,
    final_pose: Sequence[float],
    final_q: Sequence[float],
    observed_at_s: float,
    receipt_sha256: str,
) -> R006HomeBindingV1:
    pose = _finite6(final_pose, "Home receipt pose")
    if abs(pose[0] - contract.home_pose[0]) > HOME_POSITION_TOLERANCE_M:
        raise YieldLiveContractError("Home receipt X differs from the contact Home")
    if abs(pose[1] - contract.home_pose[1]) > HOME_POSITION_TOLERANCE_M:
        raise YieldLiveContractError("Home receipt Y differs from the contact Home")
    if abs(pose[2] - contract.home_pose[2]) > HOME_POSITION_TOLERANCE_M:
        raise YieldLiveContractError("Home receipt Z differs from the contact Home")
    q = _finite6(final_q, "Home receipt q")
    eoat = load_new_eoat_profile()
    profile = ContactHomeProfileV1(
        eoat_profile_id=eoat.profile_id,
        eoat_profile_sha256=eoat.profile_sha256,
        pose=contract.home_pose,
    )
    entry = R006HomeStartReceiptV1(
        receipt_sha256=receipt_sha256,
        script_sha256=contract.script1_sha256["script"],
        observed_at_s=float(observed_at_s),
        final_pose=pose,
        final_q=q,
        stationary=True,
        safety_mode="NORMAL",
        eoat_identity_sha256=eoat.profile_sha256,
        home_profile_id=HOME_PROFILE_ID,
    )
    try:
        return R006HomeBindingV1(
            profile=profile,
            reference_type=ContactHomeReferenceV1,
            entry_receipt=entry,
        )
    except R006LiveAdapterError as exc:
        raise YieldLiveContractError(str(exc)) from exc
