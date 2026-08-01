"""Typed and hash-bound contracts for the isolated V4 r004 source closure."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
R004_CONTRACT = ROOT / "config/step5d/autotune_v4_r004.json"
PROGRAM = "step5d_strict_rnn_autotune_v4_r004"
LINEAGE = "step5d_strict_rnn_autotune_v4"
SCRIPT1_PROGRAM = "step5d_autotune_start_hover_r001"
SCRIPT1_TARGET_POSE = (
    0.487834547,
    0.129337053,
    0.033000000,
    3.120752062,
    0.000000000,
    0.068626833,
)
TARGET_FORCE_N = 5.0
D_ANCHOR = 28.0
P_ANCHOR = 0.0003535533906
I_ON_ANCHOR = 0.00001
TAU_ANCHOR = 0.35
KO_ANCHOR = 0.1
KP_ANCHOR = 1.5
I_GRID = (-1.0, -0.75, -0.5, -0.25, 0.0)
MAX_ATTEMPTS = 16
TRANSFER_FLOOR_Z_M = 0.062863519
RETRACT_MIN_M = 0.005
HOME_POSITION_TOLERANCE_M = 0.001
HOME_ORIENTATION_TOLERANCE_RAD = 0.01
HOME_Q_TOLERANCE_RAD = 0.02
PACKET_STALE_S = 0.080
PRE_LATCH_TIMEOUT_S = 20.0
POST_LATCH_TIMEOUT_S = 20.0
CONTROLLER_READBACK_MAX_AGE_S = 300.0
SCRIPT1_RECEIPT_MAX_AGE_S = 120.0
RUNTIME_PROTOCOL = 606004


class R004ContractError(RuntimeError):
    """A typed r004 contract or source binding is invalid."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R004ContractError(f"{role} must be a lowercase SHA-256")
    return value


def runtime_identity_limbs(
    program: str, contract_sha256: str, campaign_fingerprint: str
) -> tuple[int, int]:
    """Map the bound r004 identity to two non-negative 31-bit output limbs."""

    if not isinstance(program, str) or not program:
        raise R004ContractError("runtime program identity is invalid")
    material = f"{program}|{contract_sha256}|{campaign_fingerprint}"
    value = hashlib.sha256(material.encode("ascii")).hexdigest()
    return int(value[:8], 16) & 0x7FFFFFFF, int(value[8:16], 16) & 0x7FFFFFFF


def finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R004ContractError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise R004ContractError(f"{role} must be finite")
    return result


def _local_path(value: Any, role: str) -> Path:
    if not isinstance(value, str) or not value:
        raise R004ContractError(f"{role} path is invalid")
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if ROOT not in path.parents:
        raise R004ContractError(f"{role} must stay inside the experiment root")
    return path


def _require_local_hash(path_value: Any, hash_value: Any, role: str) -> Path:
    path = _local_path(path_value, role)
    expected = digest(hash_value, f"{role} digest")
    if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
        raise R004ContractError(f"{role} source binding differs")
    return path


@dataclass(frozen=True)
class Candidate:
    """The seven physical fields; target force is never a search dimension."""

    force_p_gain: float = P_ANCHOR
    force_i_gain: float = 0.0
    force_damping: float = D_ANCHOR
    normal_filter_tau_s: float = TAU_ANCHOR
    orientation_ko: float = KO_ANCHOR
    motion_kp: float = KP_ANCHOR
    target_force_n: float = TARGET_FORCE_N

    def __post_init__(self) -> None:
        values = {
            field: finite(getattr(self, field), field)
            for field in (
                "force_p_gain",
                "force_i_gain",
                "force_damping",
                "normal_filter_tau_s",
                "orientation_ko",
                "motion_kp",
                "target_force_n",
            )
        }
        bounds = {
            "force_p_gain": (0.00025, 0.0005946035575),
            "force_damping": (19.7989899, 39.5979797),
            "normal_filter_tau_s": (0.2474873734, 0.4949747468),
            "orientation_ko": (0.1, 0.2),
            "motion_kp": (1.5, 3.0),
        }
        for field, (lo, hi) in bounds.items():
            if not lo - 1e-12 <= values[field] <= hi + 1e-12:
                raise R004ContractError(f"{field} is outside bounded campaign range")
        if values["force_i_gain"] != 0.0 and not any(
            math.isclose(
                values["force_i_gain"], I_ON_ANCHOR * (2.0**power), abs_tol=1e-14
            )
            for power in I_GRID
        ):
            raise R004ContractError("force_i_gain is outside the fixed typed grid")
        if not math.isclose(values["target_force_n"], TARGET_FORCE_N, abs_tol=1e-12):
            raise R004ContractError("target_force_n is immutable at 5 N")
        for field, value in values.items():
            object.__setattr__(self, field, value)

    @property
    def canonical(self) -> dict[str, float]:
        return {
            "force_p_gain": self.force_p_gain,
            "force_i_gain": self.force_i_gain,
            "force_damping": self.force_damping,
            "normal_filter_tau_s": self.normal_filter_tau_s,
            "orientation_ko": self.orientation_ko,
            "motion_kp": self.motion_kp,
            "target_force_n": self.target_force_n,
        }

    @property
    def uid(self) -> str:
        return sha256_bytes(
            json.dumps(
                self.canonical, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        )


@dataclass(frozen=True)
class R004Contract:
    path: Path
    sha256: str
    campaign_fingerprint: str
    eoat_sha256: str
    script1_sha256: Mapping[str, str]
    raw: Mapping[str, Any]


def _campaign_fingerprint(document: Mapping[str, Any]) -> str:
    material = {
        "schema": document["schema"],
        "lineage": document["lineage"],
        "program": document["program"],
        "revision": document["revision"],
        "fingerprint": document["fingerprint"],
        "wire": document["wire"],
        "timing": document["timing"],
        "campaign": document["campaign"],
        "acceptance": document["acceptance"],
    }
    return sha256_bytes(
        json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


def load_contract(path: Path = R004_CONTRACT) -> R004Contract:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R004ContractError(f"r004 contract must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R004ContractError(f"r004 contract is unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise R004ContractError("r004 contract must be an object")
    if (
        document.get("schema") != "step5d.autotune-v4/release-contract-v3"
        or document.get("lineage") != LINEAGE
        or document.get("program") != PROGRAM
        or document.get("revision") != 4
        or document.get("status") != "offline_candidate_live_blocked"
    ):
        raise R004ContractError("r004 release identity differs")
    fingerprint = document.get("fingerprint")
    wire = document.get("wire")
    timing = document.get("timing")
    campaign = document.get("campaign")
    acceptance = document.get("acceptance")
    if not all(isinstance(section, dict) for section in (fingerprint, wire, timing, campaign, acceptance)):
        raise R004ContractError("r004 contract sections are missing")
    if (
        fingerprint.get("target_force_n") != TARGET_FORCE_N
        or fingerprint.get("damping") != D_ANCHOR
        or fingerprint.get("wrench_authority") != "kunwei_only"
        or fingerprint.get("ur_builtin_force_allowed") is not False
        or fingerprint.get("sensor_zero_tare_or_config_allowed") is not False
    ):
        raise R004ContractError("r004 physical invariants differ")
    if wire.get("schema") != "step5d.autotune-v4/register-wire-v3" or wire.get("layout_tag") != 606:
        raise R004ContractError("r004 wire layout differs")
    if wire.get("session_commands") != {"HOLD": 0, "ARM": 1, "COMPLETE": 2, "STOP": 3}:
        raise R004ContractError("r004 session command mapping differs")
    if campaign.get("total_logical_attempts") != MAX_ATTEMPTS:
        raise R004ContractError("r004 campaign length differs")
    if campaign.get("qualification") != 3 or campaign.get("batch_a") != 5 or campaign.get("batch_b") != 5 or campaign.get("retest") != 3:
        raise R004ContractError("r004 campaign order bounds differ")
    if timing.get("packet_max_equal_age_s") != PACKET_STALE_S or timing.get("pre_latch_timeout_s") != PRE_LATCH_TIMEOUT_S or timing.get("post_latch_timeout_s") != POST_LATCH_TIMEOUT_S:
        raise R004ContractError("r004 timing bounds differ")
    if acceptance.get("path_coverage_bins") != 550 or acceptance.get("effective_rate_hz_min") != 75:
        raise R004ContractError("r004 acceptance shape differs")
    script1 = document.get("script1")
    script2 = document.get("script2")
    eoat = fingerprint.get("eoat_contract")
    if not isinstance(script1, dict) or not isinstance(script2, dict) or not isinstance(eoat, dict):
        raise R004ContractError("r004 Script 1 or EOAT binding is missing")
    authority = document.get("authority")
    if (
        not isinstance(authority, dict)
        or authority.get("implementation") != "tools/step5d_bridge_authority.py"
        or authority.get("resource_id") != "step5d-bridge-writer"
        or authority.get("same_resource_as_v3_and_v4") is not True
        or authority.get("r004_attempt_id_and_route_id_required") is not True
        or authority.get("lifecycle")
        != ["begin", "AuthorityFence.assert_active", "revoke", "AuthorityFence.assert_revoked"]
    ):
        raise R004ContractError("r004 canonical authority binding differs")
    if (
        script2.get("program") != PROGRAM
        or script2.get("controller_target")
        != f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"
        or script2.get("play_captures_home_once") is not True
        or script2.get("fault_stays_stopped") is not True
        or script2.get("fault_auto_home") is not False
    ):
        raise R004ContractError("r004 Script 2 resident identity differs")
    script1_hashes = {
        suffix: digest(script1.get(f"{suffix}_sha256"), f"Script 1 {suffix}")
        for suffix in ("script", "txt", "urp")
    }
    for suffix, value in script1_hashes.items():
        _require_local_hash(script1.get(f"{suffix}_path"), value, f"Script 1 {suffix}")
    eoat_path = _require_local_hash(eoat.get("path"), eoat.get("sha256"), "V4 EOAT")
    del eoat_path
    anchor = campaign.get("anchor")
    if not isinstance(anchor, dict):
        raise R004ContractError("r004 anchor is missing")
    Candidate(**anchor)
    if document.get("architecture", {}).get("capability_dag") == document.get("architecture", {}).get("runtime_feedback_loop"):
        raise R004ContractError("capability DAG and runtime feedback loop must be distinct")
    return R004Contract(
        path=path,
        sha256=sha256_file(path),
        campaign_fingerprint=_campaign_fingerprint(document),
        eoat_sha256=digest(eoat.get("sha256"), "V4 EOAT"),
        script1_sha256=script1_hashes,
        raw=document,
    )


def assert_target(candidate: Candidate, target_force_n: float = TARGET_FORCE_N) -> None:
    if not isinstance(candidate, Candidate):
        raise R004ContractError("candidate must be typed")
    if not math.isclose(candidate.target_force_n, TARGET_FORCE_N, abs_tol=1e-12) or not math.isclose(float(target_force_n), TARGET_FORCE_N, abs_tol=1e-12):
        raise R004ContractError("target force must remain 5 N")


__all__ = [
    "Candidate",
    "CONTROLLER_READBACK_MAX_AGE_S",
    "D_ANCHOR",
    "HOME_ORIENTATION_TOLERANCE_RAD",
    "HOME_POSITION_TOLERANCE_M",
    "HOME_Q_TOLERANCE_RAD",
    "I_GRID",
    "I_ON_ANCHOR",
    "KO_ANCHOR",
    "KP_ANCHOR",
    "LINEAGE",
    "MAX_ATTEMPTS",
    "PACKET_STALE_S",
    "P_ANCHOR",
    "PRE_LATCH_TIMEOUT_S",
    "PROGRAM",
    "R004_CONTRACT",
    "R004Contract",
    "R004ContractError",
    "RUNTIME_PROTOCOL",
    "SCRIPT1_PROGRAM",
    "SCRIPT1_RECEIPT_MAX_AGE_S",
    "SCRIPT1_TARGET_POSE",
    "TARGET_FORCE_N",
    "TRANSFER_FLOOR_Z_M",
    "assert_target",
    "digest",
    "load_contract",
    "sha256_bytes",
    "sha256_file",
    "runtime_identity_limbs",
]
