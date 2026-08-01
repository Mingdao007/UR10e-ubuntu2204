"""Strict, side-effect-free contracts for the independent 5 N V4 lineage."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
R001_CONTRACT = EXPERIMENT_ROOT / "config/step5d/autotune_v4_r001.json"
R002_CONTRACT = EXPERIMENT_ROOT / "config/step5d/autotune_v4_r002.json"
R003_CONTRACT = EXPERIMENT_ROOT / "config/step5d/autotune_v4_r003.json"
DEFAULT_CONTRACT = R003_CONTRACT
SCHEMA = "step5d.autotune-v4/release-contract-v2"
LEGACY_SCHEMA = "step5d.autotune-v4/release-contract-v1"
LINEAGE = "step5d_strict_rnn_autotune_v4"
PROGRAM_R001 = "step5d_strict_rnn_autotune_v4_r001"
PROGRAM_R002 = "step5d_strict_rnn_autotune_v4_r002"
PROGRAM_R003 = "step5d_strict_rnn_autotune_v4_r003"
PROGRAM = PROGRAM_R003
TARGET_FORCE_N = 5.0
GENTLE_CONTACT_SPEED_M_S = 0.0002
GENTLE_CONTACT_ACCEL_M_S2 = 0.005
GENTLE_CONTACT_NORMAL_N = 0.5
GENTLE_CONTACT_FORCE_NORM_N = 0.7
GENTLE_CONTACT_MAX_TRAVEL_M = 0.025
GENTLE_CONTACT_TIMEOUT_S = 90.0
P_ANCHOR = 0.0003535533906
I_ON_ANCHOR = 0.00001
D_ANCHOR = 28.0
TAU_ANCHOR = 0.35
KO_ANCHOR = 0.1
KP_ANCHOR = 1.5
NAMED_DIMENSIONS = (
    "log2(P)",
    "log2(D)",
    "log2(tau)",
    "log2(I_on)",
    "I_off",
    "log2(Ko)",
    "log2(Kp)",
)
I_GRID = (-1.0, -0.75, -0.5, -0.25, 0.0)
R002_INHERIT_SCOPE = (
    "isolation",
    "robot_model_binding",
    "entry",
    "baseline_5n",
    "runtime",
    "bo",
    "eligibility",
    "promotion",
    "live_activation",
)
# The canonical stage table is intentionally updated for the isolated r004
# source closure.  Keep the legacy r003 contract readable without rewriting
# its frozen config/artifact bytes when the only table change is the staged,
# inactive V4 addendum.
LEGACY_R003_STAGE_TABLE_SHA256 = "0a1372a5dd0220268a4e304f02f3f666bd7df402a9056a566b8048131a10f81c"


class V4ContractError(RuntimeError):
    """The independent V4 contract or candidate is invalid."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise V4ContractError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise V4ContractError(f"{role} must be finite")
    return result


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V4ContractError(f"{role} must be a lowercase SHA-256")
    return value


def _resolve(path_text: Any, role: str) -> Path:
    if not isinstance(path_text, str) or not path_text:
        raise V4ContractError(f"{role} path is invalid")
    path = Path(path_text)
    if not path.is_absolute():
        path = EXPERIMENT_ROOT / path
    return path.resolve()


def _require_hash(
    path_text: Any, digest: Any, role: str, *, local_only: bool = False
) -> Path:
    path = _resolve(path_text, role)
    expected = _digest(digest, f"{role} digest")
    if local_only and EXPERIMENT_ROOT not in path.parents:
        raise V4ContractError(f"{role} source must remain inside the experiment root")
    actual = None if path.is_symlink() or not path.is_file() else _sha256(path)
    if actual != expected and not (
        role == "path_table"
        and expected == LEGACY_R003_STAGE_TABLE_SHA256
        and _is_isolated_r004_stage_table_addendum(path)
    ):
        raise V4ContractError(f"{role} source binding differs")
    return path


def _is_isolated_r004_stage_table_addendum(path: Path) -> bool:
    """Allow only the bounded staged-row replacement required by r004 docs."""

    if path.is_symlink() or not path.is_file() or path.name != "step5_stage_table.json":
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    stages = document.get("stages")
    if not isinstance(stages, list):
        return False
    by_id = {
        item.get("id"): item
        for item in stages
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    v3 = by_id.get("step5d_strict_rnn_autotune_v3")
    v4 = by_id.get("step5d_strict_rnn_autotune_v4")
    if not isinstance(v3, dict) or not isinstance(v4, dict):
        return False
    v3_binding = v3.get("current_binding", {})
    v4_binding = v4.get("current_binding", {})
    v4_source = v4.get("source_binding", {})
    return (
        v3.get("active") is True
        and v3_binding.get("is_current") is True
        and v3_binding.get("program") == "step5d_strict_rnn_autotune_v3"
        and v3_binding.get("current_stage_pointer") == "config/current_stage.json"
        and v4.get("active") is False
        and v4.get("blocked") is True
        and v4_binding.get("is_current") is False
        and v4_binding.get("program") == "step5d_strict_rnn_autotune_v4_r004"
        and v4_binding.get("current_stage_pointer")
        == "config/step5d/lineage_selector_v4_r004.json"
        and v4_source.get("release_contract")
        == "config/step5d/autotune_v4_r004.json"
    )


def _close(actual: float, expected: float, role: str, tolerance: float = 1e-12) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance):
        raise V4ContractError(f"{role} differs: expected={expected} actual={actual}")


def _within(value: float, bounds: tuple[float, float], role: str) -> None:
    if value < bounds[0] - 1e-12 or value > bounds[1] + 1e-12:
        raise V4ContractError(f"{role} is outside bounds {bounds}")


@dataclass(frozen=True)
class V4Candidate:
    """Seven physical fields; target remains immutable and non-optimized."""

    force_p_gain: float = P_ANCHOR
    force_i_gain: float = 0.0
    force_damping: float = D_ANCHOR
    normal_filter_tau_s: float = TAU_ANCHOR
    orientation_ko: float = KO_ANCHOR
    motion_kp: float = KP_ANCHOR
    target_force_n: float = TARGET_FORCE_N

    def __post_init__(self) -> None:
        values = {
            field: _finite(getattr(self, field), field)
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
        _within(values["force_p_gain"], (0.00025, 0.0005946035575), "P")
        _within(values["force_damping"], (19.7989899, 39.5979797), "D")
        _within(values["normal_filter_tau_s"], (0.2474873734, 0.4949747468), "tau")
        _within(values["orientation_ko"], (0.1, 0.2), "Ko")
        _within(values["motion_kp"], (1.5, 3.0), "Kp")
        i = values["force_i_gain"]
        if i != 0.0 and not any(
            math.isclose(i, I_ON_ANCHOR * (2.0**k), rel_tol=0.0, abs_tol=1e-14)
            for k in I_GRID
        ):
            raise V4ContractError("I must be off or on the fixed 0.25-octave grid")
        _close(values["target_force_n"], TARGET_FORCE_N, "target_force_n")
        for field, value in values.items():
            object.__setattr__(self, field, value)

    @property
    def i_off(self) -> bool:
        return self.force_i_gain == 0.0

    @property
    def canonical_physical(self) -> dict[str, float]:
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
    def candidate_uid(self) -> str:
        encoded = json.dumps(
            self.canonical_physical,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class V4Contract:
    path: Path
    sha256: str
    eoat_sha256: str
    model_hashes: Mapping[str, str]
    campaign_fingerprint: str
    raw: Mapping[str, Any]


def _fingerprint(document: Mapping[str, Any]) -> str:
    material = {
        "schema": document["schema"],
        "lineage": document["lineage"],
        "program": document["program"],
        "revision": document["revision"],
        "fingerprint": document["fingerprint"],
        "robot_model_binding": document["robot_model_binding"],
        "runtime": document["runtime"],
        "bo": document["bo"],
        "eligibility": document["eligibility"],
        "policy_binding": document.get("policy_binding"),
        "wire": document.get("wire"),
    }
    return hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_contract(path: Path = DEFAULT_CONTRACT) -> V4Contract:
    if path.is_symlink() or not path.is_file():
        raise V4ContractError(f"V4 contract must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V4ContractError(f"V4 contract is unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise V4ContractError("V4 contract must be an object")
    is_legacy = document.get("schema") == LEGACY_SCHEMA
    revision = document.get("revision")
    if not is_legacy:
        inheritance = document.get("inherits")
        if not isinstance(inheritance, dict):
            raise V4ContractError("V4 inheritance binding is missing")
        base_path = _require_hash(
            inheritance.get("path"),
            inheritance.get("sha256"),
            "V4 r001 base",
            local_only=True,
        )
        base = json.loads(base_path.read_text(encoding="utf-8"))
        scope = inheritance.get("scope")
        if tuple(scope or ()) != R002_INHERIT_SCOPE:
            raise V4ContractError("V4 inheritance scope is invalid")
        inherited = {key: base[key] for key in scope}
        document = _merge(inherited, document)
        revision = document.get("revision")
    expected_program = {
        1: PROGRAM_R001,
        2: PROGRAM_R002,
        3: PROGRAM_R003,
    }.get(1 if is_legacy else revision)
    if (
        document.get("schema") not in (SCHEMA, LEGACY_SCHEMA)
        or document.get("lineage") != LINEAGE
        or expected_program is None
        or document.get("program") != expected_program
        or document.get("revision") != (1 if is_legacy else revision)
        or (not is_legacy and revision not in (2, 3))
    ):
        raise V4ContractError("V4 release identity differs")
    if not is_legacy:
        if document.get("status") != "offline_candidate_live_blocked":
            raise V4ContractError("V4 candidate must remain inactive")
        activation = document.get("live_activation")
        if not isinstance(activation, dict) or not str(
            activation.get("current_status", "")
        ).startswith("BLOCKED_"):
            raise V4ContractError("V4 activation is not machine-blocked")
        if revision == 3 and activation.get("r006_canary_required") is not False:
            raise V4ContractError("V4 r003 must not require standalone r006 canary")
        if revision == 2 and activation.get("r006_canary_required") is not True:
            raise V4ContractError("V4 r002 historical activation still requires r006")
    isolation = document.get("isolation")
    fingerprint = document.get("fingerprint")
    binding = document.get("robot_model_binding")
    runtime = document.get("runtime")
    bo = document.get("bo")
    if not all(
        isinstance(value, dict)
        for value in (isolation, fingerprint, binding, runtime, bo)
    ):
        raise V4ContractError("V4 contract sections are missing")
    if not is_legacy:
        policy = document.get("policy_binding")
        wire = document.get("wire")
        if not isinstance(policy, dict) or not isinstance(wire, dict):
            raise V4ContractError("V4 policy/wire binding is missing")
        for role in (
            "composition",
            "baseline_ledger",
            "force_search_core",
            "eoat_profiles",
        ):
            _require_hash(
                policy.get(f"{role}_path"),
                policy.get(f"{role}_sha256"),
                f"V4 {role}",
                local_only=True,
            )
        if (
            policy.get("invariant_envelope_replaceable") is not False
            or policy.get("missing_non_safety_provider_behavior")
            != "zero_qdot_no_motion"
            or wire.get("layout_code") != 605
            or wire.get("baseline_qualification_owner")
            != "BaselineQualificationLedger"
            or wire.get("baseline_success_terminal_stage") != 22
        ):
            raise V4ContractError("V4 primitive seam contract differs")
        if revision == 3:
            contact = document.get("contact_acquisition")
            if not isinstance(contact, dict):
                raise V4ContractError("V4 r003 contact acquisition contract is missing")
            if (
                _finite(contact.get("search_speed_m_s"), "search speed")
                != GENTLE_CONTACT_SPEED_M_S
                or _finite(contact.get("search_acceleration_m_s2"), "search accel")
                != GENTLE_CONTACT_ACCEL_M_S2
                or _finite(contact.get("positive_normal_load_n"), "contact normal")
                != GENTLE_CONTACT_NORMAL_N
                or _finite(contact.get("force_norm_n"), "contact force norm")
                != GENTLE_CONTACT_FORCE_NORM_N
                or _finite(contact.get("max_travel_m"), "contact travel")
                != GENTLE_CONTACT_MAX_TRAVEL_M
                or _finite(contact.get("timeout_s"), "contact timeout")
                != GENTLE_CONTACT_TIMEOUT_S
                or contact.get("standalone_canary_prerequisite") is not False
                or contact.get("frame") != "base"
                or contact.get("axis") != "negative_z"
            ):
                raise V4ContractError("V4 r003 gentle contact acquisition differs")

    if isolation.get("v3_lineage") != "step5d_strict_rnn_autotune_v3" or any(
        isolation.get(field) is not False
        for field in (
            "v3_current_pointer_mutation_allowed",
            "v3_observations_import_allowed",
            "v3_gp_import_allowed",
            "v3_incumbent_import_allowed",
        )
    ):
        raise V4ContractError("V3/V4 isolation contract differs")
    eoat_path = _require_hash(
        fingerprint.get("eoat_contract_path"),
        fingerprint.get("eoat_contract_sha256"),
        "EOAT",
        local_only=True,
    )
    del eoat_path
    if (
        _finite(fingerprint.get("target_force_n"), "fingerprint target")
        != TARGET_FORCE_N
        or fingerprint.get("wrench_authority") != "kunwei_only"
        or fingerprint.get("ur_builtin_force_allowed") is not False
    ):
        raise V4ContractError("V4 campaign fingerprint semantics differ")
    model_hashes: dict[str, str] = {}
    model_roles = [
        "ur_xacro",
        "calibration_yaml",
        "jacobian_gate",
        "kinematics_solver",
        "strict_rnn",
        "contact_semantics",
    ]
    if revision == 3:
        model_roles.extend(
            (
                "calibrated_runtime",
                "path_reference",
                "path_table",
                "strict_rnn_gate",
            )
        )
    for prefix in model_roles:
        _require_hash(
            binding.get(f"{prefix}_path"),
            binding.get(f"{prefix}_sha256"),
            prefix,
        )
        if prefix in {
            "ur_xacro",
            "calibration_yaml",
            "jacobian_gate",
            "kinematics_solver",
            "strict_rnn",
            "contact_semantics",
        }:
            model_hashes[prefix] = _digest(
                binding.get(f"{prefix}_sha256"), f"{prefix} digest"
            )
    if runtime != {
        "dt_mode": "monotonic_actual",
        "startup_increment_count": 2,
        "startup_window_s": 0.25,
        "minimum_rate_hz": 75.0,
        "p99_gap_max_s": 0.02,
        "maximum_gap_exclusive_s": 0.08,
        "nonpositive_dt_stop": True,
        "cartesian_total_speed_cap_m_s": 0.0005,
        "normal_speed_cap_m_s": 0.00035,
        "tangential_speed_cap_m_s": 0.00035,
        "angular_speed_cap_rad_s": 0.05,
        "qdot_cap_rad_s": 0.15,
        "speedj_acceleration_rad_s2": 2.5,
        "stopj_acceleration_rad_s2": 2.5,
        "jacobian_gate_before_register_write": True,
    }:
        raise V4ContractError("V4 monotonic runtime contract differs")
    if tuple(bo.get("physical_fields", ())) != (
        "force_p_gain",
        "force_i_gain",
        "force_damping",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
        "target_force_n",
    ):
        raise V4ContractError("V4 physical field order differs")
    if tuple(bo.get("named_dimensions", ())) != NAMED_DIMENSIONS:
        raise V4ContractError("V4 named 7D order differs")
    anchor = bo.get("anchor")
    if not isinstance(anchor, dict):
        raise V4ContractError("V4 anchor is missing")
    V4Candidate(**anchor)
    if _finite(bo.get("first_release_force_damping_fixed"), "release D") != D_ANCHOR:
        raise V4ContractError("first V4 release must keep D=28")
    return V4Contract(
        path=path,
        sha256=_sha256(path),
        eoat_sha256=_digest(
            fingerprint["eoat_contract_sha256"], "EOAT contract digest"
        ),
        model_hashes=model_hashes,
        campaign_fingerprint=_fingerprint(document),
        raw=document,
    )


def encode_named7d(candidate: V4Candidate) -> tuple[float, ...]:
    """Encode the typed composite I-off batch schema into named 7D."""
    if candidate.i_off:
        log2_i_on = 0.0
        i_off = 1.0
    else:
        log2_i_on = math.log2(candidate.force_i_gain / I_ON_ANCHOR)
        i_off = 0.0
    return (
        math.log2(candidate.force_p_gain / P_ANCHOR),
        math.log2(candidate.force_damping / D_ANCHOR),
        math.log2(candidate.normal_filter_tau_s / TAU_ANCHOR),
        log2_i_on,
        i_off,
        math.log2(candidate.orientation_ko / KO_ANCHOR),
        math.log2(candidate.motion_kp / KP_ANCHOR),
    )


def decode_named7d(
    coordinates: Sequence[float], *, target_force_n: float = TARGET_FORCE_N
) -> V4Candidate:
    """Decode with a second immutable-target assertion."""
    if len(coordinates) != 7:
        raise V4ContractError("V4 optimizer coordinate must be named 7D")
    values = tuple(_finite(value, NAMED_DIMENSIONS[index]) for index, value in enumerate(coordinates))
    log2_p, log2_d, log2_tau, log2_i_on, i_off, log2_ko, log2_kp = values
    if i_off not in (0.0, 1.0):
        raise V4ContractError("I_off must be the typed categorical value 0 or 1")
    if i_off == 1.0:
        if log2_i_on != 0.0:
            raise V4ContractError("I-off canonical log2(I_on) must be zero")
        force_i_gain = 0.0
    else:
        if not any(math.isclose(log2_i_on, k, abs_tol=1e-12) for k in I_GRID):
            raise V4ContractError("I-on log2 coordinate is outside its fixed grid")
        force_i_gain = I_ON_ANCHOR * (2.0**log2_i_on)
    _close(_finite(target_force_n, "decoded target_force_n"), TARGET_FORCE_N, "decoded target_force_n")
    return V4Candidate(
        force_p_gain=P_ANCHOR * (2.0**log2_p),
        force_i_gain=force_i_gain,
        force_damping=D_ANCHOR * (2.0**log2_d),
        normal_filter_tau_s=TAU_ANCHOR * (2.0**log2_tau),
        orientation_ko=KO_ANCHOR * (2.0**log2_ko),
        motion_kp=KP_ANCHOR * (2.0**log2_kp),
        target_force_n=target_force_n,
    )


def assert_runtime_target(candidate: V4Candidate, runtime_target_force_n: float) -> None:
    """Third immutable-target assertion at the runtime boundary."""
    _close(candidate.target_force_n, TARGET_FORCE_N, "candidate runtime target")
    _close(
        _finite(runtime_target_force_n, "runtime target_force_n"),
        TARGET_FORCE_N,
        "runtime target_force_n",
    )


def changed_physical_coordinates(
    previous: V4Candidate, current: V4Candidate
) -> tuple[str, ...]:
    """Treat typed I off->on as one physical transition, never two."""
    changed: list[str] = []
    for field in (
        "force_p_gain",
        "force_i_gain",
        "force_damping",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
        "target_force_n",
    ):
        if not math.isclose(
            getattr(previous, field),
            getattr(current, field),
            rel_tol=0.0,
            abs_tol=1e-14,
        ):
            changed.append(field)
    return tuple(changed)


def validate_live_transition(
    previous: V4Candidate, current: V4Candidate
) -> tuple[str, ...]:
    changed = changed_physical_coordinates(previous, current)
    if len(changed) != 1:
        raise V4ContractError("each live transition must change exactly one physical coordinate")
    field = changed[0]
    if field == "target_force_n":
        raise V4ContractError("target_force_n cannot change")
    before = getattr(previous, field)
    after = getattr(current, field)
    if field == "force_i_gain" and (before == 0.0 or after == 0.0):
        nonzero = after if before == 0.0 else before
        if not math.isclose(nonzero, I_ON_ANCHOR * 0.5, abs_tol=1e-14):
            raise V4ContractError("I off/on transition must enter or leave at k=-1")
        return changed
    if abs(math.log2(after / before)) > 0.25 + 1e-12:
        raise V4ContractError("live transition exceeds 0.25 octave")
    return changed


__all__ = [
    "DEFAULT_CONTRACT",
    "D_ANCHOR",
    "GENTLE_CONTACT_ACCEL_M_S2",
    "GENTLE_CONTACT_FORCE_NORM_N",
    "GENTLE_CONTACT_MAX_TRAVEL_M",
    "GENTLE_CONTACT_NORMAL_N",
    "GENTLE_CONTACT_SPEED_M_S",
    "GENTLE_CONTACT_TIMEOUT_S",
    "I_GRID",
    "I_ON_ANCHOR",
    "KO_ANCHOR",
    "KP_ANCHOR",
    "LINEAGE",
    "NAMED_DIMENSIONS",
    "P_ANCHOR",
    "PROGRAM",
    "PROGRAM_R001",
    "PROGRAM_R002",
    "PROGRAM_R003",
    "R001_CONTRACT",
    "R002_CONTRACT",
    "R002_INHERIT_SCOPE",
    "R003_CONTRACT",
    "TARGET_FORCE_N",
    "TAU_ANCHOR",
    "V4Candidate",
    "V4Contract",
    "V4ContractError",
    "assert_runtime_target",
    "changed_physical_coordinates",
    "decode_named7d",
    "encode_named7d",
    "load_contract",
    "validate_live_transition",
]
