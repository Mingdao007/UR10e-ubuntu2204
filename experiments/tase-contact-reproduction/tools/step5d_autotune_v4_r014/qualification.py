"""Validate the bounded R014 qualification bundle and promote its profile."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .common import R014Error, atomic_write_json, load_json, sha256_file, sha256_value
from .profiles import FORMAL_PROFILE_NAME, ProfileRegistry, StrategyProfile


QUALIFICATION_SCHEMA = "step5d.autotuner-r014/small-qualification-bundle-v1"
INCUMBENT = {
    "force_p_gain": 0.019027313840405524,
    "force_damping": 188.36079701683204,
    "force_i_gain": 0.008610779292198037,
    "normal_filter_tau_s": 0.04375,
    "orientation_ko": 0.05,
    "motion_kp": 2.5226892457611436,
}


@dataclass(frozen=True)
class QualificationResult:
    qualified_profile: StrategyProfile
    bundle_sha256: str
    evidence_sha256: tuple[str, ...]


def _require(value: bool, message: str) -> None:
    if value is not True:
        raise R014Error(message)


def _receipt_files(bundle: Mapping[str, Any], bundle_path: Path) -> tuple[str, ...]:
    evidence = bundle.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise R014Error("qualification evidence manifest is empty")
    identities: list[str] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise R014Error("qualification evidence record is malformed")
        raw_path = Path(str(item.get("path", "")))
        path = raw_path if raw_path.is_absolute() else bundle_path.parent / raw_path
        expected = str(item.get("sha256", ""))
        if not path.is_file() or sha256_file(path) != expected:
            raise R014Error(f"qualification evidence path/hash mismatch: {path}")
        identities.append(expected)
    return tuple(identities)


def validate_small_qualification(
    registry: ProfileRegistry, bundle_path: Path
) -> tuple[dict[str, Any], tuple[str, ...], StrategyProfile]:
    bundle_path = bundle_path.resolve()
    bundle = load_json(bundle_path)
    if bundle.get("schema") != QUALIFICATION_SCHEMA or bundle.get("version") != 1:
        raise R014Error("small qualification bundle schema/version mismatch")
    pending = registry.by_name("finite-time")
    if pending.name != FORMAL_PROFILE_NAME:
        raise R014Error("small qualification is not bound to the formal profile")
    _require(bundle.get("profile_sha256") == pending.sha256, "qualification profile drift")
    _require(bundle.get("host_source_manifest_sha256") == pending.raw["host_source"]["host_source_manifest_sha256"], "qualification host-source drift")
    _require(bundle.get("release_manifest_sha256") == pending.raw["release"]["manifest_sha256"], "qualification release drift")
    _require(bundle.get("fatal_failure") is False, "qualification contains a hard/protective/scope failure")

    timing = bundle.get("formal_timing")
    if not isinstance(timing, Mapping):
        raise R014Error("formal timing receipt is missing")
    for key, samples in (("solver", 10_000), ("full_tick", 30_000), ("safe_hold", 30_000)):
        record = timing.get(key)
        if not isinstance(record, Mapping):
            raise R014Error(f"formal timing record is missing: {key}")
        _require(record.get("passed") is True, f"formal timing failed: {key}")
        _require(record.get("samples") == samples, f"formal timing sample count differs: {key}")

    package = bundle.get("tp_package")
    if not isinstance(package, Mapping):
        raise R014Error("TP package receipt is missing")
    for key in ("uploaded", "read_back", "exact_bytes"):
        _require(package.get(key) is True, f"TP package gate failed: {key}")

    home = bundle.get("home_no_contact_canary")
    if not isinstance(home, Mapping):
        raise R014Error("Home/no-contact receipt is missing")
    for key in ("verified_home", "safety_normal", "no_contact"):
        _require(home.get(key) is True, f"Home/no-contact gate failed: {key}")

    acquisitions = bundle.get("contact_acquisitions")
    if not isinstance(acquisitions, list) or len(acquisitions) != 3:
        raise R014Error("qualification requires exactly three NON_BO contact acquisitions")
    for index, record in enumerate(acquisitions, 1):
        if not isinstance(record, Mapping):
            raise R014Error(f"contact acquisition {index} is malformed")
        _require(record.get("role") == "NON_BO", f"contact acquisition {index} role differs")
        for key in ("qualified", "safe_return_home"):
            _require(record.get(key) is True, f"contact acquisition {index} failed: {key}")

    trials = bundle.get("incumbent_trials")
    if not isinstance(trials, list) or len(trials) != 3:
        raise R014Error("qualification requires exactly three fresh incumbent trials")
    for index, record in enumerate(trials, 1):
        if not isinstance(record, Mapping):
            raise R014Error(f"incumbent trial {index} is malformed")
        _require(record.get("candidate") == INCUMBENT, f"incumbent trial {index} coordinates differ")
        _require(record.get("phase_correction") == "off", f"incumbent trial {index} used phase correction")
        _require(record.get("duration_s") == 60.0, f"incumbent trial {index} duration differs")
        for key in ("exact", "sealed", "qualified", "safe_return_home"):
            _require(record.get(key) is True, f"incumbent trial {index} failed: {key}")
        _require(record.get("hard_safety_veto") is False, f"incumbent trial {index} hit hard veto")

    evidence_sha = _receipt_files(bundle, bundle_path)
    return bundle, evidence_sha, pending


def promote_small_qualification(
    experiment_root: Path, bundle_path: Path
) -> QualificationResult:
    registry = ProfileRegistry(experiment_root)
    bundle, evidence_sha, pending = validate_small_qualification(registry, bundle_path)
    bundle_sha = sha256_file(bundle_path.resolve())
    qualified = dict(pending.raw)
    qualified["qualification"] = {
        "status": "qualified",
        "bundle_path": str(bundle_path.resolve()),
        "bundle_sha256": bundle_sha,
        "receipts": list(evidence_sha),
    }
    identity = sha256_value(qualified)
    path = registry.root / "profiles" / identity / "profile.json"
    atomic_write_json(path, qualified)
    profile = registry.promote(path)
    atomic_write_json(
        registry.root / "qualification/promotion-receipt.json",
        {
            "schema": "step5d.autotuner-r014/profile-promotion-receipt-v1",
            "version": 1,
            "bundle_sha256": bundle_sha,
            "profile_path": str(profile.path),
            "profile_sha256": profile.sha256,
            "previous_profile_sha256": pending.sha256,
        },
    )
    return QualificationResult(
        qualified_profile=profile,
        bundle_sha256=bundle_sha,
        evidence_sha256=evidence_sha,
    )
