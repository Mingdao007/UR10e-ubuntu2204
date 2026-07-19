"""Validated launch-time and trial-boundary parameter overlays for V3.

This module is offline-safe.  It owns parameter mutability and fingerprints;
it does not start a bridge, open a socket, or touch a controller.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .profile import ContractViolation, canonical_json_bytes, contract_sha256, load_contract


LAUNCH_SCHEMA = "step5d.autotune-v3/launch-profile-v1"
OVERLAY_SCHEMA = "step5d.autotune-v3/trial-overlay-v2"
RELEASE_STAGE_ID = "step5d_strict_rnn_autotune_v3"
CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"
TP_PROGRAM_ID = "step5d_strict_rnn_autotune_v3"
DEFAULT_LAUNCH_PROFILE = (
    Path(__file__).resolve().parents[2]
    / "config/step5/step5d_autotune_v3_launch_profile.json"
)

CONTROL_CANDIDATE_FIELDS = (
    "force_p_gain",
    "force_i_gain",
    "force_damping",
    "orientation_ko",
)
ORIENTATION_KO_LATTICE = (
    0.4,
    0.47568284600108846,
    0.5656854249492381,
    0.6727171322029717,
    0.8,
)


def control_candidate_uid(candidate: Mapping[str, Any]) -> str:
    values: dict[str, float] = {}
    for field in CONTROL_CANDIDATE_FIELDS:
        value = candidate[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractViolation(f"{field} must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ContractViolation(f"{field} must be finite")
        values[field] = numeric
    material = {
        "schema": "step5d.autotune-v3/control-candidate/v2",
        **values,
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


OVERLAY_FIELDS = (
    *CONTROL_CANDIDATE_FIELDS,
    "control_candidate_uid",
    "execution_profile_id",
    "step5d_preload_filtered_min_n",
    "step5d_preload_filtered_max_n",
    "step5d_preload_raw_min_n",
    "step5d_preload_raw_max_n",
    "step5d_preload_force_norm_max_n",
    "step5d_preload_hold_s",
    "step5d_preload_timeout_s",
)
_DEFAULT_OVERLAY_INPUT: dict[str, Any] = {
    "force_p_gain": 0.001,
    "force_i_gain": 0.00001,
    "force_damping": 7.0,
    "orientation_ko": 0.4,
    "execution_profile_id": "nf050-slew050-a050",
    "step5d_preload_filtered_min_n": 7.5,
    "step5d_preload_filtered_max_n": 14.0,
    "step5d_preload_raw_min_n": 7.0,
    "step5d_preload_raw_max_n": 15.0,
    "step5d_preload_force_norm_max_n": 25.0,
    "step5d_preload_hold_s": 0.1,
    "step5d_preload_timeout_s": 10.0,
}
DEFAULT_OVERLAY: dict[str, Any] = {
    **_DEFAULT_OVERLAY_INPUT,
    "control_candidate_uid": control_candidate_uid(_DEFAULT_OVERLAY_INPUT),
}
OVERLAY_FLAGS = {
    "force_p_gain": "--step5d-autotune-force-p",
    "force_i_gain": "--step5d-autotune-force-i",
    "force_damping": "--step5d-autotune-force-damping",
    "step5d_preload_filtered_min_n": "--step5d-preload-filtered-min-n",
    "step5d_preload_filtered_max_n": "--step5d-preload-filtered-max-n",
    "step5d_preload_raw_min_n": "--step5d-preload-raw-min-n",
    "step5d_preload_raw_max_n": "--step5d-preload-raw-max-n",
    "step5d_preload_force_norm_max_n": "--step5d-preload-force-norm-max-n",
    "step5d_preload_hold_s": "--step5d-preload-hold-s",
    "step5d_preload_timeout_s": "--step5d-preload-timeout-s",
}
EXECUTION_PROFILE_FLAGS = {
    "--step5d-autotune-normal-rate-rad-s",
    "--step5d-autotune-host-slew-rad-s2",
    "--step5d-autotune-speedj-acceleration-rad-s2",
}
RUNTIME_FLAGS = {
    "--step5d-autotune-campaign-epoch",
    "--step5d-autotune-trial-id",
    "--step5d-autotune-command",
    "--step5d-autotune-candidate-token",
    "--step5d-autotune-execution-profile-id",
    "--step5d-autotune-command-sequence",
    "--step5d-autotune-command-mailbox",
    "--output-dir",
}
CONTRACT_BOUND_FLAGS = {
    "--target-force-n",
    "--normal-axis",
    "--normal-sign",
    "--bridge-profile",
    "--bridge-mode",
    "--bridge-path-shape",
    "--bridge-normal-command-sign",
    "--bridge-orientation-wx-sign",
    "--bridge-orientation-wy-sign",
    "--bridge-normal-filter-tau-s",
    "--step5d-stage25-control-mode",
    "--step5d-qdot-limit-rad-s",
}
RAW_UPPER_LIMITS = {
    "--max-normal-force-n": 60.0,
    "--max-force-norm-n": 100.0,
    "--max-torque-norm-nm": 3.0,
    "--bridge-line-speed-m-s": 0.003,
    "--bridge-motion-limit-m-s": 0.004,
    "--bridge-total-linear-limit-m-s": 0.006,
    "--bridge-normal-velocity-limit-m-s": 0.003,
    "--bridge-angular-limit-rad-s": 0.05,
    "--step5c-qdot-limit-rad-s": 0.15,
}


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractViolation(f"duplicate launch-profile JSON key {key!r}")
        result[key] = value
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ContractViolation(f"{name} must be finite")
    return result


def _sha256(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractViolation(f"{name} must be a lowercase SHA-256")
    return value


def _flag_rows(contract: Mapping[str, Any]) -> dict[str, list[Any]]:
    return {row[0]: list(row) for row in contract["cli_arguments"]}


def launch_mutable_flags(contract: Mapping[str, Any]) -> frozenset[str]:
    rows = _flag_rows(contract)
    excluded = (
        CONTRACT_BOUND_FLAGS
        | set(OVERLAY_FLAGS.values())
        | EXECUTION_PROFILE_FLAGS
        | RUNTIME_FLAGS
    )
    return frozenset(
        flag for flag, row in rows.items() if len(row) == 2 and flag not in excluded
    )


def _validate_launch_override(flag: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ContractViolation(f"launch override {flag} must be a scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractViolation(f"launch override {flag} must be finite")
    text = str(value)
    if not text or text.startswith("--") or "\x00" in text or "\n" in text:
        raise ContractViolation(f"launch override {flag} has an unsafe value")
    if flag in RAW_UPPER_LIMITS:
        numeric = _finite(flag, value)
        if numeric < 0.0 or numeric > RAW_UPPER_LIMITS[flag]:
            raise ContractViolation(
                f"launch override {flag} exceeds its hard ceiling {RAW_UPPER_LIMITS[flag]}"
            )
    return text


@dataclass(frozen=True)
class LaunchProfile:
    document: Mapping[str, Any]
    launch_overrides: Mapping[str, str]
    trial_overlay_policy: Mapping[str, Mapping[str, Any]]
    fingerprint: str


def load_launch_profile(
    path: Path = DEFAULT_LAUNCH_PROFILE,
    *,
    contract: Mapping[str, Any] | None = None,
) -> LaunchProfile:
    payload_contract = dict(contract or load_contract())
    if path.is_symlink() or not path.is_file():
        raise ContractViolation(f"launch profile must be a real regular file: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ContractViolation(f"non-finite launch-profile constant {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractViolation(f"cannot load launch profile: {exc}") from exc
    required = {
        "schema",
        "release_stage_id",
        "control_profile_id",
        "tp_program_id",
        "control_contract_sha256",
        "moving_sphere_reference_sha256",
        "launch_overrides",
        "trial_overlay_policy",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ContractViolation("launch profile fields differ")
    expected_identity = {
        "schema": LAUNCH_SCHEMA,
        "release_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "tp_program_id": TP_PROGRAM_ID,
    }
    for key, expected in expected_identity.items():
        if payload[key] != expected:
            raise ContractViolation(f"launch profile {key} differs")
    _sha256(payload["control_contract_sha256"], name="control_contract_sha256")
    _sha256(
        payload["moving_sphere_reference_sha256"],
        name="moving_sphere_reference_sha256",
    )
    if payload["control_contract_sha256"] != contract_sha256(payload_contract):
        raise ContractViolation("launch profile control contract binding differs")
    mutable = launch_mutable_flags(payload_contract)
    raw_overrides = payload["launch_overrides"]
    if not isinstance(raw_overrides, dict) or not set(raw_overrides).issubset(mutable):
        unknown = sorted(set(raw_overrides or {}) - mutable) if isinstance(raw_overrides, dict) else []
        raise ContractViolation(f"launch profile contains non-launch-mutable flags: {unknown}")
    overrides = {
        flag: _validate_launch_override(flag, value)
        for flag, value in raw_overrides.items()
    }
    policy = payload["trial_overlay_policy"]
    if not isinstance(policy, dict) or set(policy) != set(OVERLAY_FIELDS):
        raise ContractViolation("trial overlay policy fields or order differ")
    for field in OVERLAY_FIELDS:
        rule = policy[field]
        if field == "execution_profile_id":
            if not isinstance(rule, dict) or set(rule) != {"allowed"}:
                raise ContractViolation("execution profile overlay policy differs")
            allowed = rule["allowed"]
            if (
                not isinstance(allowed, list)
                or not allowed
                or len(allowed) != len(set(allowed))
                or any(not isinstance(value, str) or not value for value in allowed)
            ):
                raise ContractViolation("execution profile allowlist is invalid")
        elif field == "orientation_ko":
            if not isinstance(rule, dict) or set(rule) != {"allowed"}:
                raise ContractViolation("orientation_ko overlay policy differs")
            if rule["allowed"] != list(ORIENTATION_KO_LATTICE):
                raise ContractViolation("orientation_ko lattice differs")
        elif field == "control_candidate_uid":
            if rule != {"derived": "sha256"}:
                raise ContractViolation("control candidate UID policy differs")
        else:
            if not isinstance(rule, dict) or set(rule) != {"min", "max"}:
                raise ContractViolation(f"trial overlay policy differs for {field}")
            lower = _finite(f"{field}.min", rule["min"])
            upper = _finite(f"{field}.max", rule["max"])
            if lower > upper:
                raise ContractViolation(f"trial overlay range is inverted for {field}")
    document = dict(payload)
    fingerprint = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
    profile = LaunchProfile(document, overrides, policy, fingerprint)
    normalize_trial_overlay(DEFAULT_OVERLAY, profile=profile)
    return profile


def _execution_profile(profile_id: str) -> Any:
    from step5d_autotune_contract import NORMAL_FILTER_PROFILES

    matches = [profile for profile in NORMAL_FILTER_PROFILES if profile.profile_id == profile_id]
    if len(matches) != 1:
        raise ContractViolation(f"unknown execution_profile_id {profile_id!r}")
    return matches[0]


def normalize_trial_overlay(
    overlay: Mapping[str, Any] | None = None,
    *,
    profile: LaunchProfile,
) -> dict[str, Any]:
    raw = DEFAULT_OVERLAY if overlay is None else overlay
    allowed_shapes = (
        set(OVERLAY_FIELDS),
        set(OVERLAY_FIELDS) - {"control_candidate_uid"},
    )
    if not isinstance(raw, Mapping) or set(raw) not in allowed_shapes:
        raise ContractViolation("trial overlay fields or order differ")
    result: dict[str, Any] = {}
    for field in OVERLAY_FIELDS:
        rule = profile.trial_overlay_policy[field]
        if field == "control_candidate_uid":
            expected_control_uid = control_candidate_uid(result)
            supplied_control_uid = raw.get(
                "control_candidate_uid", expected_control_uid
            )
            if supplied_control_uid != expected_control_uid:
                raise ContractViolation(
                    "trial control_candidate_uid differs from parameters"
                )
            result[field] = expected_control_uid
            continue
        value = raw[field]
        if field == "execution_profile_id":
            if not isinstance(value, str) or value not in rule["allowed"]:
                raise ContractViolation("trial execution profile is not launch-authorized")
            _execution_profile(value)
            result[field] = value
            continue
        if field == "orientation_ko":
            numeric = _finite(field, value)
            matches = [
                allowed
                for allowed in ORIENTATION_KO_LATTICE
                if math.isclose(numeric, allowed, rel_tol=0.0, abs_tol=1e-12)
            ]
            if len(matches) != 1:
                raise ContractViolation("trial orientation_ko is outside its lattice")
            result[field] = matches[0]
            continue
        numeric = _finite(field, value)
        if numeric < float(rule["min"]) or numeric > float(rule["max"]):
            raise ContractViolation(f"trial overlay {field} is outside the launch envelope")
        result[field] = numeric
    from step5d_autotune_contract import ForceCandidate

    try:
        ForceCandidate(
            force_p_gain=result["force_p_gain"],
            force_i_gain=result["force_i_gain"],
            force_damping=result["force_damping"],
        )
    except ValueError as exc:
        raise ContractViolation(f"trial force candidate is invalid: {exc}") from exc
    if result["step5d_preload_filtered_min_n"] > result["step5d_preload_filtered_max_n"]:
        raise ContractViolation("filtered preload minimum exceeds maximum")
    if result["step5d_preload_raw_min_n"] > result["step5d_preload_raw_max_n"]:
        raise ContractViolation("raw preload minimum exceeds maximum")
    if result["step5d_preload_force_norm_max_n"] < max(
        result["step5d_preload_filtered_max_n"],
        result["step5d_preload_raw_max_n"],
    ):
        raise ContractViolation("preload force-norm maximum is below an axis maximum")
    return result


def control_candidate_coordinates(overlay: Mapping[str, Any]) -> tuple[float, ...]:
    """Return P/I/damping/K physical coordinates in log2 space."""

    from step5d_autotune_contract import ForceCandidate

    candidate = ForceCandidate(
        force_p_gain=float(overlay["force_p_gain"]),
        force_i_gain=float(overlay["force_i_gain"]),
        force_damping=float(overlay["force_damping"]),
    )
    return (
        candidate.log2_p,
        candidate.log2_i,
        candidate.log2_damping,
        math.log2(float(overlay["orientation_ko"]) / ORIENTATION_KO_LATTICE[0]),
    )


def is_control_candidate_step(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    before = control_candidate_coordinates(previous)
    after = control_candidate_coordinates(current)
    changed = [
        abs(right - left)
        for left, right in zip(before, after, strict=True)
        if not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)
    ]
    return len(changed) == 1 and math.isclose(
        changed[0], 0.25, rel_tol=0.0, abs_tol=1e-9
    )


def overlay_fingerprint(profile: LaunchProfile, overlay: Mapping[str, Any]) -> str:
    normalized = normalize_trial_overlay(overlay, profile=profile)
    material = {
        "schema": OVERLAY_SCHEMA,
        "launch_profile_fingerprint": profile.fingerprint,
        "overlay": normalized,
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def comparison_profile_fingerprint(
    profile: LaunchProfile, overlay: Mapping[str, Any]
) -> str:
    normalized = normalize_trial_overlay(overlay, profile=profile)
    material = {
        "schema": "step5d.autotune-v3/comparison-profile-v1",
        "launch_profile_fingerprint": profile.fingerprint,
        "execution_profile_id": normalized["execution_profile_id"],
        "preload": {
            key: normalized[key]
            for key in OVERLAY_FIELDS
            if key.startswith("step5d_preload_")
        },
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _replace_flag(argv: list[str], flag: str, value: str) -> None:
    try:
        index = argv.index(flag)
    except ValueError as exc:
        raise ContractViolation(f"governed argv lacks {flag}") from exc
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise ContractViolation(f"governed argv shape differs for {flag}")
    argv[index + 1] = value


def apply_profile_to_argv(
    argv: Sequence[str],
    *,
    profile: LaunchProfile,
    overlay: Mapping[str, Any] | None = None,
) -> list[str]:
    result = list(argv)
    for flag, value in profile.launch_overrides.items():
        _replace_flag(result, flag, value)
    normalized = normalize_trial_overlay(overlay, profile=profile)
    for field, flag in OVERLAY_FLAGS.items():
        _replace_flag(result, flag, str(normalized[field]))
    execution = _execution_profile(normalized["execution_profile_id"])
    for flag, value in (
        ("--step5d-autotune-normal-rate-rad-s", execution.normal_max_rate_rad_s),
        ("--step5d-autotune-host-slew-rad-s2", execution.host_qdot_slew_rad_s2),
        (
            "--step5d-autotune-speedj-acceleration-rad-s2",
            execution.tp_speedj_accel_rad_s2,
        ),
    ):
        _replace_flag(result, flag, str(value))
    return result


@dataclass(frozen=True)
class CachedMailboxValue:
    identity: tuple[int, int, int, int]
    value: Any


class IdentityCachedMailbox:
    """Cache an immutable mailbox decode while its file identity is unchanged."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.path = delegate.path
        self._cached: CachedMailboxValue | None = None

    @staticmethod
    def _identity(path: Path) -> tuple[int, int, int, int]:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContractViolation("command mailbox must be a singly-linked regular file")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def read_latest(self) -> Any:
        try:
            identity = self._identity(self.path)
        except FileNotFoundError:
            self._cached = None
            return None
        if self._cached is not None and self._cached.identity == identity:
            return self._cached.value
        value = self.delegate.read_latest()
        after = self._identity(self.path)
        if identity != after:
            raise ContractViolation("command mailbox changed across cached decode")
        self._cached = CachedMailboxValue(after, value)
        return value

    def send_command(self, *args: Any, **kwargs: Any) -> Any:
        result = self.delegate.send_command(*args, **kwargs)
        self._cached = None
        return result
