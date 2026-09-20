#!/usr/bin/env python3
"""Bounded development mechanism diagnostic. Not validation, not training.

Explicit create / run-next / status / report. One native attempt at a time.
Main owns integration and any later full-period execution. Zero training or
reserved-validation budget; no freeze is manufactured.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import numpy as np

from contact_yield_controller import YieldSettings
from contact_yield_metrics import compare_pair, summarize_trial
from contact_yield_protocol import PERIOD_S, protocol
from yield_contact_tuner import FT_V1_FIXED, MSFC_IDENTITY_METRIC, YieldContactTuner
from yield_fair_campaign import (
    _execution_identities as training_execution_identities,
    _observer_sha256 as training_observer_sha256,
    _source_hashes as training_source_hashes,
)
from yield_fair_selection import (
    OBSERVER_V3,
    FULLSTATE_LIMIT,
    METHODS as FAIR_METHODS,
    YieldFairContract,
    YieldFairMemberResult,
    YieldFairPairResult,
    YieldFairSelectionError,
    _finite,
    _load_series,
    _mapping,
    _native_failed,
    _objective_components,
    _require_full_cycle_grid,
    _require_fullstate,
    _require_identities,
    contract_from_config,
)
from yield_validation_selection import (
    ARM_ACTUAL_METHOD,
    RESOLUTION_SETTINGS,
    arm_law_parameters,
)


STATE_SCHEMA = "ur10e.yield-mechanism-development-state-v1"
MANIFEST_SCHEMA = "ur10e.yield-mechanism-development-manifest-v1"
PROTOCOL_SCHEMA = "ur10e.yield-mechanism-development-protocol-v1"
OUTCOME_SCHEMA = "ur10e.yield-mechanism-development-outcome-v1"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FAIR_CONFIG = EXPERIMENT_ROOT / "config" / "yield_fair_campaign_v1.json"
ARMS = ("SFC", "SFC_RADIAL", "DSFC", "MSFC", "MSFC_IDENTITY")
SELECTED_UNITS = (0, 4)
CONDITIONS = ("nominal", "disturbed")
CAMPAIGN_KIND = "diagnostic_seed"
TOTAL_TRIALS = 60
CLAIM_SCOPE = (
    "bounded DEVELOPMENT mechanism diagnostic on the already observed stiff_low_mu "
    "surface; not reserved validation, not a training debit, no freeze, no winner"
)
DATA_SELECTION = {
    "units": [0, 4],
    "source": "YieldContactTuner.shared_mechanical_triples",
    "unit0": "first shared initial triple",
    "unit4": (
        "chosen because memory adversity was observed on this shared initial unit "
        "in the training development table; not a holdout and not an independent cell"
    ),
    "not_holdout": True,
    "not_incumbent_freeze": True,
    "viewed_training_table": True,
}
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class YieldMechanismDevelopmentError(ValueError):
    """Invalid development-diagnostic command, binding, or slot state."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical(dict(payload)).encode("utf-8")
    tmp = path.with_name(f"{path.name}.partial.{os.getpid()}.{threading.get_ident()}")
    tmp.write_bytes(encoded)
    try:
        os.link(tmp, path)
    except FileExistsError as error:
        raise FileExistsError(f"refusing to overwrite immutable path: {path}") from error
    finally:
        tmp.unlink(missing_ok=True)
    digest = _sha256_bytes(path.read_bytes())
    if digest != _sha256_bytes(encoded):
        raise YieldMechanismDevelopmentError("artifact digest drifted during write")
    return digest


def _thread_lock(output_root: Path) -> threading.Lock:
    key = str(output_root)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _exclusive(output_root: Path):
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / "campaign.lock"
    thread_lock = _thread_lock(output_root)
    thread_lock.acquire()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        thread_lock.release()


def _default_runner(**kwargs: Any) -> dict[str, Any]:
    from contact_yield_runner import run_closed_loop
    return run_closed_loop(**kwargs)


def _load_fair_config_raw(experiment_root: Path) -> tuple[dict[str, Any], bytes]:
    path = experiment_root / "config" / "yield_fair_campaign_v1.json"
    raw = path.read_bytes()
    root = json.loads(raw.decode("utf-8"))
    if not isinstance(root, Mapping):
        raise YieldMechanismDevelopmentError("fair campaign config must be an object")
    return dict(root), raw


def _load_fair_config(experiment_root: Path) -> dict[str, Any]:
    config, _ = _load_fair_config_raw(experiment_root)
    return config


def _open_tuner(experiment_root: Path, config: Mapping[str, Any]) -> YieldContactTuner:
    return YieldContactTuner(
        experiment_root / str(config["tuner_config"]),
        training_cell_id=str(config["training_cell_id"]),
        selection_contract_id=str(config["selection_contract_id"]),
    )


def selected_triples(tuner: YieldContactTuner) -> dict[int, tuple[float, float, float]]:
    triples = tuner.shared_mechanical_triples()
    missing = [index for index in SELECTED_UNITS if index >= len(triples)]
    if missing:
        raise YieldMechanismDevelopmentError(f"tuner initial triples missing units {missing}")
    return {index: tuple(float(x) for x in triples[index]) for index in SELECTED_UNITS}


def bound_arm_parameters(
    tuner: YieldContactTuner,
    arm: str,
    triple: Sequence[float],
) -> dict[str, float]:
    if arm not in ARMS:
        raise YieldMechanismDevelopmentError(f"unknown development arm {arm!r}")
    m, mu, g = (float(triple[0]), float(triple[1]), float(triple[2]))
    selected = {
        method: tuner._make_candidate(method, m, mu, g).as_dict()
        for method in FAIR_METHODS
    }
    return arm_law_parameters(arm, selected)


def build_schedule(tuner: YieldContactTuner) -> list[dict[str, Any]]:
    triples = selected_triples(tuner)
    slots = []
    index = 0
    for unit in SELECTED_UNITS:
        triple = triples[unit]
        for arm in ARMS:
            parameters = bound_arm_parameters(tuner, arm, triple)
            actual = ARM_ACTUAL_METHOD[arm]
            for resolution_id, dt_s, plant_substeps in RESOLUTION_SETTINGS:
                for condition in CONDITIONS:
                    slots.append({
                        "index": index,
                        "slot_id": f"u{unit}-{arm}-{resolution_id}-{condition}",
                        "pair_id": f"u{unit}-{arm}-{resolution_id}",
                        "unit_index": unit,
                        "arm": arm,
                        "actual_method": actual,
                        "resolution_id": resolution_id,
                        "condition": condition,
                        "controller_dt_s": dt_s,
                        "plant_substeps": plant_substeps,
                        "scenario": "nominal" if condition == "nominal" else "sustained_release_oblique",
                        "m": triple[0],
                        "mu": triple[1],
                        "g": triple[2],
                        "law_parameters": dict(parameters),
                    })
                    index += 1
    if len(slots) != TOTAL_TRIALS:
        raise YieldMechanismDevelopmentError("development schedule must contain exactly 60 slots")
    return slots


def _binary_identity(path: Path | None, *, required: bool) -> tuple[str | None, str | dict[str, str] | None]:
    if path is None:
        return None, None
    resolved = Path(path).expanduser().resolve()
    if required and not resolved.is_file() and not resolved.is_dir():
        raise YieldMechanismDevelopmentError(f"production binary missing: {resolved}")
    if resolved.is_file():
        return str(resolved), _file_sha256(resolved)
    if resolved.is_dir():
        hashed = {str(item): _file_sha256(item) for item in sorted(resolved.rglob("*.so")) if item.is_file()}
        return str(resolved), hashed
    return str(resolved), None


def _resolve_binaries(
    experiment_root: Path,
    config: Mapping[str, Any],
    *,
    qp_library: Path | str | None,
    native_laws_root: Path | str | None,
) -> dict[str, Any]:
    runtime = dict(config.get("runtime") or {})
    qp_default = experiment_root / str(runtime.get("qp_library") or "build/contact-qp/libcontact_qp.so")
    laws_default = experiment_root / str(runtime.get("native_laws_root") or "build/contact-six-laws")
    qp_path, qp_sha = _binary_identity(Path(qp_library) if qp_library is not None else qp_default, required=True)
    if not qp_path or not Path(qp_path).is_file() or not isinstance(qp_sha, str) or not _is_sha256(qp_sha):
        raise YieldMechanismDevelopmentError("qp library identity is unbound")
    laws_path, laws_sha = _binary_identity(
        Path(native_laws_root) if native_laws_root is not None else laws_default, required=True,
    )
    if not laws_path or not Path(laws_path).is_dir() or not isinstance(laws_sha, dict) or not laws_sha:
        raise YieldMechanismDevelopmentError("native laws identity is unbound")
    return {
        "qp_library": qp_path,
        "qp_library_sha256": qp_sha,
        "native_laws_root": laws_path,
        "native_laws_sha256": laws_sha,
        "explicit_qp_library": qp_library is not None,
        "explicit_native_laws_root": native_laws_root is not None,
    }


def _verify_binary(path_text: str | None, expected: str | dict[str, str] | None, *, kind: str) -> None:
    if not path_text:
        if expected is not None:
            raise YieldMechanismDevelopmentError(f"{kind} identity drifted")
        return
    path = Path(path_text)
    if expected is None:
        if path.exists():
            raise YieldMechanismDevelopmentError(f"{kind} appeared after create; identity unbound")
        return
    if isinstance(expected, str):
        if not path.is_file() or _file_sha256(path) != expected:
            raise YieldMechanismDevelopmentError(f"{kind} identity drifted")
        return
    if not path.is_dir():
        raise YieldMechanismDevelopmentError(f"{kind} identity drifted")
    libraries = {str(item): _file_sha256(item) for item in sorted(path.rglob("*.so")) if item.is_file()}
    if libraries != expected:
        raise YieldMechanismDevelopmentError(f"{kind} identity drifted")


def create_campaign(
    output: Path | str,
    *,
    experiment_root: Path | str | None = None,
    qp_library: Path | str | None = None,
    native_laws_root: Path | str | None = None,
) -> dict[str, Any]:
    output_root = Path(output).expanduser().resolve()
    state_path = output_root / "campaign.json"
    manifest_path = output_root / "manifest.json"
    if state_path.exists() or manifest_path.exists():
        raise FileExistsError(f"output root is immutable: {output_root}")
    experiment_root = Path(experiment_root or EXPERIMENT_ROOT).resolve()
    config, config_raw = _load_fair_config_raw(experiment_root)
    contract = contract_from_config(config)
    if (contract.kappa_xx, contract.kappa_yy) != (0.8, 0.4):
        raise YieldMechanismDevelopmentError("development surface must remain kappa_xx=0.8, kappa_yy=0.4")
    if contract.prior != "approach" or contract.preparation != "cold":
        raise YieldMechanismDevelopmentError("development cell must remain approach/cold")
    if contract.disturbed_scenario != "sustained_release_oblique":
        raise YieldMechanismDevelopmentError("disturbed scenario must remain sustained_release_oblique")
    if not math.isclose(contract.duration_s, 2.0 * math.pi / 0.1, rel_tol=0.0, abs_tol=0.0):
        raise YieldMechanismDevelopmentError("duration must remain the full unwrapped 2*pi/0.1 period")
    tuner = _open_tuner(experiment_root, config)
    slots = build_schedule(tuner)
    observer_sha256 = training_observer_sha256(experiment_root, config)
    source_hashes = training_source_hashes(experiment_root)
    training_identities = training_execution_identities(experiment_root, config)
    binaries = _resolve_binaries(
        experiment_root, config, qp_library=qp_library, native_laws_root=native_laws_root,
    )
    execution_identities = dict(training_identities)
    execution_identities["qp_library"] = binaries["qp_library"]
    execution_identities["qp_library_sha256"] = binaries["qp_library_sha256"]
    execution_identities["native_laws_root"] = binaries["native_laws_root"]
    execution_identities["native_laws_sha256"] = binaries["native_laws_sha256"]
    execution_identities["diagnostic_files"] = {
        "tools/yield_mechanism_development.py": _file_sha256(Path(__file__)),
    }
    manifest_payload = {
        "schema": MANIFEST_SCHEMA,
        "slots": slots,
        "order": "unit,arm,resolution,condition",
        "count": len(slots),
        "immutable": True,
    }
    schedule_sha256 = _sha256_bytes(_canonical(slots).encode("utf-8"))
    manifest_sha256 = _sha256_bytes(_canonical(manifest_payload).encode("utf-8"))
    triples = selected_triples(tuner)
    protocol_payload = {
        "schema": PROTOCOL_SCHEMA,
        "dataset_role": "development",
        "not_validation": True,
        "not_training": True,
        "training_budget": 0,
        "validation_budget": 0,
        "campaign_kind": CAMPAIGN_KIND,
        "data_selection": dict(DATA_SELECTION),
        "units": {
            str(index): {"m": triple[0], "mu": triple[1], "g": triple[2]}
            for index, triple in triples.items()
        },
        "arms": list(ARMS),
        "actual_methods": dict(ARM_ACTUAL_METHOD),
        "resolution_settings": [
            {"id": name, "controller_dt_s": dt, "plant_substeps": sub}
            for name, dt, sub in RESOLUTION_SETTINGS
        ],
        "material": "stiff_low_mu",
        "surface_parameters": {"kappa_xx": 0.8, "kappa_yy": 0.4},
        "prior": "approach",
        "preparation": "cold",
        "observer": dict(OBSERVER_V3),
        "nominal_scenario": "nominal",
        "disturbed_scenario": "sustained_release_oblique",
        "timeline": "full_cycle",
        "duration_s": PERIOD_S,
        "outer": {
            "path_stiffness_n_per_m": contract.path_stiffness_n_per_m,
            "compliance_stiffness_n_per_m": contract.compliance_stiffness_n_per_m,
        },
        "msfc_identity_metric": MSFC_IDENTITY_METRIC,
        "msfc_fixed_except_metric": dict(FT_V1_FIXED["MSFC"]),
        "campaign_config_sha256": _sha256_bytes(config_raw),
        "tuner_config": str(config["tuner_config"]),
        "tuner_config_sha256": tuner.config_sha256,
        "observer_config_sha256": observer_sha256,
        "yield_protocol_sha256": str(protocol()["sha256"]),
        "source_hashes": dict(source_hashes),
        "training_execution_identities": dict(training_identities),
        "execution_identities": execution_identities,
        "manifest_sha256": manifest_sha256,
        "schedule_sha256": schedule_sha256,
        "claim_scope": CLAIM_SCOPE,
        "fullstate_limit": FULLSTATE_LIMIT,
        "no_winner_selection": True,
        "task_tolerances_unchanged": True,
    }
    campaign_protocol_sha256 = _sha256_bytes(_canonical(protocol_payload).encode("utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": STATE_SCHEMA,
        "version": 1,
        "experiment_root": str(experiment_root),
        "output_root": str(output_root),
        "qp_library": binaries["qp_library"],
        "native_laws_root": binaries["native_laws_root"],
        "campaign_protocol": protocol_payload,
        "campaign_protocol_sha256": campaign_protocol_sha256,
        "formal_validation_complete": False,
        "formal_campaign_complete": False,
        "holdout_used_for_tuning": False,
        "training_writes": False,
        "claim_scope": CLAIM_SCOPE,
        "maximum_total_trials": TOTAL_TRIALS,
    }
    _atomic_write_json(state_path, state)
    written_manifest = _atomic_write_json(manifest_path, manifest_payload)
    if written_manifest != manifest_sha256:
        raise YieldMechanismDevelopmentError("manifest identity drifted during write")
    return {
        "schema": STATE_SCHEMA,
        "output_root": str(output_root),
        "campaign_protocol_sha256": campaign_protocol_sha256,
        "created": True,
        "executed_trials": 0,
        "formal_validation_complete": False,
        "dataset_role": "development",
    }


def _load_state(output: Path | str) -> dict[str, Any]:
    output_root = Path(output).expanduser().resolve()
    state_path = output_root / "campaign.json"
    if not state_path.is_file():
        raise YieldMechanismDevelopmentError("campaign state is missing")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != STATE_SCHEMA:
        raise YieldMechanismDevelopmentError("campaign state schema differs")
    return state


def _load_manifest(output_root: Path) -> list[dict[str, Any]]:
    payload = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    slots = payload.get("slots")
    if not isinstance(slots, list) or len(slots) != TOTAL_TRIALS:
        raise YieldMechanismDevelopmentError("development manifest is not the fixed 60-slot schedule")
    return list(slots)


def _require_bound_binaries(state: Mapping[str, Any]) -> None:
    identities = state["campaign_protocol"]["execution_identities"]
    qp_sha = identities.get("qp_library_sha256")
    laws_sha = identities.get("native_laws_sha256")
    if not _is_sha256(qp_sha) or not isinstance(laws_sha, Mapping) or not laws_sha:
        raise YieldMechanismDevelopmentError("production binaries unbound")
    qp_path = state.get("qp_library")
    laws_path = state.get("native_laws_root")
    if not qp_path or not laws_path:
        raise YieldMechanismDevelopmentError("production binaries unbound")
    _verify_binary(qp_path, qp_sha, kind="qp library")
    _verify_binary(laws_path, laws_sha, kind="native laws")


def _verify_schedule(state: Mapping[str, Any], config: Mapping[str, Any]) -> None:
    output_root = Path(state["output_root"])
    manifest_path = output_root / "manifest.json"
    if not manifest_path.is_file():
        raise YieldMechanismDevelopmentError("development manifest is missing")
    protocol_payload = state["campaign_protocol"]
    raw = manifest_path.read_bytes()
    if _sha256_bytes(raw) != protocol_payload.get("manifest_sha256"):
        raise YieldMechanismDevelopmentError("manifest identity drifted")
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise YieldMechanismDevelopmentError("manifest schema differs")
    slots = payload.get("slots")
    if not isinstance(slots, list) or len(slots) != TOTAL_TRIALS:
        raise YieldMechanismDevelopmentError("development manifest is not the fixed 60-slot schedule")
    if _sha256_bytes(_canonical(slots).encode("utf-8")) != protocol_payload.get("schedule_sha256"):
        raise YieldMechanismDevelopmentError("schedule identity drifted")
    tuner = _open_tuner(Path(state["experiment_root"]), config)
    if tuner.config_sha256 != protocol_payload["tuner_config_sha256"]:
        raise YieldMechanismDevelopmentError("tuner config drifted")
    expected = build_schedule(tuner)
    if _canonical(slots) != _canonical(expected):
        raise YieldMechanismDevelopmentError(
            "schedule drifted from bound tuner/arm/resolution protocol"
        )


def _verify_bindings(state: Mapping[str, Any]) -> None:
    experiment_root = Path(state["experiment_root"])
    protocol_payload = state["campaign_protocol"]
    digest = _sha256_bytes(_canonical(dict(protocol_payload)).encode("utf-8"))
    if digest != state.get("campaign_protocol_sha256"):
        raise YieldMechanismDevelopmentError("campaign protocol digest drifted")
    config, config_raw = _load_fair_config_raw(experiment_root)
    if _sha256_bytes(config_raw) != protocol_payload.get("campaign_config_sha256"):
        raise YieldMechanismDevelopmentError("campaign config drifted")
    tuner_path = experiment_root / str(protocol_payload.get("tuner_config") or config["tuner_config"])
    if _file_sha256(tuner_path) != protocol_payload["tuner_config_sha256"]:
        raise YieldMechanismDevelopmentError("tuner config drifted")
    if training_observer_sha256(experiment_root, config) != protocol_payload["observer_config_sha256"]:
        raise YieldMechanismDevelopmentError("observer config drifted")
    if training_source_hashes(experiment_root) != protocol_payload["source_hashes"]:
        raise YieldMechanismDevelopmentError("source identity drifted")
    if protocol()["sha256"] != protocol_payload["yield_protocol_sha256"]:
        raise YieldMechanismDevelopmentError("yield protocol drifted")
    live = training_execution_identities(experiment_root, config)
    stored_training = protocol_payload.get("training_execution_identities")
    if stored_training != live:
        raise YieldMechanismDevelopmentError("execution identity drifted")
    identities = protocol_payload["execution_identities"]
    if identities.get("scientific_files") != live["scientific_files"]:
        raise YieldMechanismDevelopmentError("execution identity drifted")
    if identities.get("native_files") != live["native_files"]:
        raise YieldMechanismDevelopmentError("execution identity drifted")
    if identities.get("diagnostic_files", {}).get("tools/yield_mechanism_development.py") != _file_sha256(Path(__file__)):
        raise YieldMechanismDevelopmentError("diagnostic source drifted")
    _require_bound_binaries(state)
    _verify_schedule(state, config)


def _artifact_path(output_root: Path, slot_id: str) -> Path:
    return output_root / "attempts" / slot_id / "artifact.json"


def _outcome_path(output_root: Path, slot_id: str) -> Path:
    return output_root / "attempts" / slot_id / "outcome.json"


def _inflight_path(output_root: Path) -> Path:
    return output_root / "inflight.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise YieldMechanismDevelopmentError(f"{path} is not an object")
    return dict(payload)


def _terminal(output_root: Path, slot_id: str) -> dict[str, Any] | None:
    return _read_json(_outcome_path(output_root, slot_id))


def _inflight(output_root: Path) -> dict[str, Any] | None:
    return _read_json(_inflight_path(output_root))


def _slot_contract(state: Mapping[str, Any], slot: Mapping[str, Any]) -> YieldFairContract:
    protocol_payload = state["campaign_protocol"]
    experiment_root = Path(state["experiment_root"])
    config, config_raw = _load_fair_config_raw(experiment_root)
    if _sha256_bytes(config_raw) != protocol_payload.get("campaign_config_sha256"):
        raise YieldMechanismDevelopmentError("campaign config drifted")
    tuner_path = experiment_root / str(protocol_payload.get("tuner_config") or config["tuner_config"])
    if _file_sha256(tuner_path) != protocol_payload["tuner_config_sha256"]:
        raise YieldMechanismDevelopmentError("tuner config drifted")
    base = contract_from_config(config)
    outer = protocol_payload["outer"]
    surface = protocol_payload["surface_parameters"]
    return replace(
        base,
        campaign_kind=CAMPAIGN_KIND,
        dt_s=float(slot["controller_dt_s"]),
        plant_substeps=int(slot["plant_substeps"]),
        path_stiffness_n_per_m=float(outer["path_stiffness_n_per_m"]),
        compliance_stiffness_n_per_m=float(outer["compliance_stiffness_n_per_m"]),
        kappa_xx=float(surface["kappa_xx"]),
        kappa_yy=float(surface["kappa_yy"]),
        method=slot["actual_method"] if slot["actual_method"] in FAIR_METHODS else None,
        candidate_parameters=dict(slot["law_parameters"]),
        source_hashes=dict(protocol_payload["source_hashes"]),
        protocol_sha256=protocol_payload["yield_protocol_sha256"],
    )


def _run_kwargs(state: Mapping[str, Any], slot: Mapping[str, Any], contract: YieldFairContract) -> dict[str, Any]:
    _require_bound_binaries(state)
    outer = state["campaign_protocol"]["outer"]
    qp_path = Path(state["qp_library"])
    laws_path = Path(state["native_laws_root"])
    return {
        "method": slot["actual_method"],
        "scenario": slot["scenario"],
        "material": contract.material,
        "duration_s": contract.duration_s,
        "dt_s": contract.dt_s,
        "timeline": "full_cycle",
        "campaign_kind": CAMPAIGN_KIND,
        "preparation": contract.preparation,
        "record_fullstate": True,
        "require_ur10e": True,
        "law_parameters": dict(slot["law_parameters"]),
        "plant_substeps": contract.plant_substeps,
        "estimator_parameters": dict(OBSERVER_V3),
        "surface_parameters": {"kappa_xx": contract.kappa_xx, "kappa_yy": contract.kappa_yy},
        "settings": YieldSettings(
            path_stiffness_n_per_m=float(outer["path_stiffness_n_per_m"]),
            compliance_stiffness_n_per_m=float(outer["compliance_stiffness_n_per_m"]),
        ),
        "qp_library": qp_path,
        "build_root": laws_path,
    }


def _params_match(actual: Mapping[str, Any], expected: Mapping[str, float]) -> bool:
    try:
        if set(actual) != set(expected):
            return False
        return all(_finite(actual[name], name) == expected[name] for name in expected)
    except (TypeError, ValueError, YieldFairSelectionError):
        return False


def inspect_member(
    artifact: Mapping[str, Any],
    contract: YieldFairContract,
    *,
    condition: str,
    arm: str,
    actual_method: str,
) -> YieldFairMemberResult:
    payload = _mapping(artifact, "artifact")
    if condition not in CONDITIONS:
        raise YieldMechanismDevelopmentError("condition must be nominal or disturbed")
    expected_scenario = contract.nominal_scenario if condition == "nominal" else contract.disturbed_scenario
    if payload.get("scenario") != expected_scenario:
        raise YieldMechanismDevelopmentError("configured scenario differs")
    if payload.get("method") == "MSFC_IDENTITY":
        raise YieldMechanismDevelopmentError("raw artifact method must not be relabeled MSFC_IDENTITY")
    if payload.get("method") != actual_method:
        raise YieldMechanismDevelopmentError("artifact method differs from the arm's actual executable method")
    if arm == "SFC_RADIAL" and payload.get("method") != "SFC_RADIAL":
        raise YieldMechanismDevelopmentError("SFC_RADIAL arm must keep actual method SFC_RADIAL")
    if arm == "MSFC_IDENTITY" and payload.get("method") != "MSFC":
        raise YieldMechanismDevelopmentError("MSFC identity arm must keep actual method MSFC")
    if payload.get("campaign_kind") in {"training", "holdout"}:
        raise YieldMechanismDevelopmentError("training/holdout rows cannot enter this development diagnostic")
    if payload.get("campaign_kind") != CAMPAIGN_KIND:
        raise YieldMechanismDevelopmentError("campaign_kind must be diagnostic_seed")
    reported = dict(_mapping(payload.get("metrics"), "metrics")) if isinstance(payload.get("metrics"), Mapping) else {}
    identity = payload.get("identity_payload")
    if isinstance(identity, Mapping) and isinstance(identity.get("parameters"), Mapping):
        if not _params_match(identity["parameters"], dict(contract.candidate_parameters or {})):
            raise YieldMechanismDevelopmentError("actual law parameters differ from the candidate")
        if arm == "MSFC_IDENTITY":
            metric = _finite(identity["parameters"].get("minimum_metric_eigenvalue"), "minimum_metric_eigenvalue")
            if metric != MSFC_IDENTITY_METRIC:
                raise YieldMechanismDevelopmentError("MSFC identity must keep minimum_metric_eigenvalue=1")
        if arm == "MSFC":
            metric = identity["parameters"].get("minimum_metric_eigenvalue")
            if metric == MSFC_IDENTITY_METRIC:
                raise YieldMechanismDevelopmentError("active MSFC must not use the identity metric")
    if _native_failed(payload):
        return YieldFairMemberResult(
            condition=condition,
            valid=False,
            failed=True,
            full_cycle=False,
            nominal_feasible=False if condition == "nominal" else None,
            disturbed_guards_ok=False if condition == "disturbed" else None,
            checks={"failed": True},
            recomputed_metrics={},
            reported_metrics=reported,
            reason="failed member",
        )
    method = actual_method
    bound = replace(contract, method=method if method in FAIR_METHODS else None)
    _require_identities(payload, bound, method=method)
    rows = payload.get("rows")
    times = _require_full_cycle_grid(rows, bound)
    _require_fullstate(payload, bound)
    recomputed = summarize_trial(
        list(rows),
        failed=False,
        failure_message=None,
        scenario=str(payload["scenario"]),
        material=bound.material,
        method=method,
        dt_s=bound.dt_s,
        kinematics_kind=str(payload.get("kinematics_kind")),
        campaign_kind=bound.campaign_kind,
        timeline=bound.timeline,
    )
    load = _load_series(rows, "true_normal_load_n")
    path_rms = recomputed["path_rmse_m"]
    progress = recomputed["progress_ratio"]
    attitude = recomputed["orientation_rmse_rad"]
    load_mae = recomputed["force_mae_n"]
    peak = recomputed["force_peak_n"]
    minimum = float(np.min(load))
    full_cycle = (
        len(times) == len(rows)
        and math.isclose(bound.duration_s, bound.period_s, rel_tol=0.0, abs_tol=1e-12)
        and bound.timeline == "full_cycle"
    )
    if condition == "nominal":
        checks = {
            "path_rms_ok": path_rms <= bound.nominal_path_rms_m_max,
            "progress_ok": progress >= bound.nominal_progress_ratio_min,
            "attitude_rms_ok": attitude <= bound.nominal_attitude_rms_rad_max,
            "load_mae_ok": load_mae <= bound.nominal_load_mae_n_max,
            "load_peak_ok": peak <= bound.nominal_load_peak_n_max,
            "load_min_ok": minimum >= bound.nominal_load_min_n_min,
            "full_cycle": full_cycle,
            "no_failure": True,
        }
        return YieldFairMemberResult(
            condition=condition,
            valid=True,
            failed=False,
            full_cycle=full_cycle,
            nominal_feasible=all(checks[name] is True for name in checks),
            disturbed_guards_ok=None,
            checks=checks,
            recomputed_metrics=recomputed,
            reported_metrics=reported,
        )
    checks = {
        "load_min_ok": minimum >= bound.disturbed_load_min_n_min,
        "load_peak_ok": peak <= bound.disturbed_load_peak_n_max,
        "progress_ok": progress >= bound.disturbed_progress_ratio_min,
        "full_cycle": full_cycle,
        "no_failure": True,
    }
    return YieldFairMemberResult(
        condition=condition,
        valid=True,
        failed=False,
        full_cycle=full_cycle,
        nominal_feasible=None,
        disturbed_guards_ok=all(checks[name] is True for name in checks),
        checks=checks,
        recomputed_metrics=recomputed,
        reported_metrics=reported,
    )


def evaluate_pair(
    nominal: Mapping[str, Any],
    disturbed: Mapping[str, Any],
    contract: YieldFairContract,
    *,
    arm: str,
    actual_method: str,
) -> YieldFairPairResult:
    reported_n = dict(nominal.get("metrics") or {}) if isinstance(nominal.get("metrics"), Mapping) else {}
    reported_d = dict(disturbed.get("metrics") or {}) if isinstance(disturbed.get("metrics"), Mapping) else {}

    def _invalid(reason: str, **fields: Any) -> YieldFairPairResult:
        return YieldFairPairResult(
            valid=False,
            objective=None,
            objective_eligible=False,
            reported_metrics_nominal=reported_n,
            reported_metrics_disturbed=reported_d,
            reported_recovery={
                "eligible": False,
                "reason": "recovery unavailable: pair is not a complete uncensored result",
            },
            reason=reason,
            **fields,
        )

    if nominal.get("scenario") == disturbed.get("scenario"):
        raise YieldMechanismDevelopmentError("pair scenarios must differ")
    for key in ("method", "material", "dt_s", "duration_s", "preparation", "timeline"):
        if nominal.get(key) != disturbed.get(key):
            raise YieldMechanismDevelopmentError(f"mismatched pair {key}")
    n_payload = _mapping(nominal.get("identity_payload"), "nominal identity") if isinstance(nominal.get("identity_payload"), Mapping) else {}
    d_payload = _mapping(disturbed.get("identity_payload"), "disturbed identity") if isinstance(disturbed.get("identity_payload"), Mapping) else {}
    if n_payload.get("parameters") != d_payload.get("parameters"):
        raise YieldMechanismDevelopmentError("mismatched pair candidate parameters")
    nominal_member = inspect_member(nominal, contract, condition="nominal", arm=arm, actual_method=actual_method)
    disturbed_member = inspect_member(disturbed, contract, condition="disturbed", arm=arm, actual_method=actual_method)
    if not nominal_member.valid or not disturbed_member.valid:
        return _invalid(
            "failed member; retained, no performance ranking",
            nominal_feasible=bool(nominal_member.nominal_feasible),
            disturbed_guards_ok=False,
            pair_feasible=False,
            recomputed_metrics_nominal=nominal_member.recomputed_metrics or None,
            recomputed_metrics_disturbed=disturbed_member.recomputed_metrics or None,
            nominal_checks=dict(nominal_member.checks),
            disturbed_checks=dict(disturbed_member.checks),
        )
    try:
        reported_recovery = compare_pair(nominal, disturbed)
    except (TypeError, ValueError, KeyError) as error:
        raise YieldMechanismDevelopmentError(f"recovery computation failed for complete pair: {error}") from error
    components = _objective_components(nominal["rows"], disturbed["rows"], contract)
    nominal_feasible = bool(nominal_member.nominal_feasible)
    guards = bool(disturbed_member.disturbed_guards_ok)
    return YieldFairPairResult(
        valid=True,
        nominal_feasible=nominal_feasible,
        disturbed_guards_ok=guards,
        pair_feasible=bool(nominal_feasible and guards),
        objective=float(components["J"]),
        objective_components=components,
        objective_eligible=True,
        reported_metrics_nominal=reported_n,
        reported_metrics_disturbed=reported_d,
        reported_recovery=reported_recovery,
        recomputed_metrics_nominal=nominal_member.recomputed_metrics,
        recomputed_metrics_disturbed=disturbed_member.recomputed_metrics,
        nominal_checks=dict(nominal_member.checks),
        disturbed_checks=dict(disturbed_member.checks),
    )


def _absolute_from_metrics(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metrics, Mapping):
        return {}
    keys = (
        "force_mae_n", "force_rmse_n", "force_peak_n", "path_rmse_m", "path_peak_m",
        "progress_ratio", "orientation_rmse_rad", "orientation_peak_rad",
        "contact_loss_duration_s", "residual_path_m", "actual_progress_m",
        "reference_progress_m",
    )
    return {key: metrics.get(key) for key in keys if key in metrics}


def _pair_fields(result: YieldFairPairResult) -> dict[str, Any]:
    recovery = dict(result.reported_recovery or {})
    components = dict(result.objective_components or {}) if result.objective_components else {}
    return {
        "pair_complete": result.valid,
        "nominal_feasible": result.nominal_feasible,
        "disturbed_guards_ok": result.disturbed_guards_ok,
        "pair_feasible": result.pair_feasible,
        "objective": result.objective,
        "objective_components": components or None,
        "objective_eligible": result.objective_eligible,
        "reported_recovery": recovery,
        "absolute_descriptors": {
            "J": result.objective,
            **{key: components.get(key) for key in ("Jload", "Jpath", "Jatt") if components},
            "recovery_s": recovery.get("recovery_s"),
            "recovery_eligible": recovery.get("eligible"),
            **{f"nominal_{key}": value for key, value in _absolute_from_metrics(result.recomputed_metrics_nominal).items()},
            **{f"disturbed_{key}": value for key, value in _absolute_from_metrics(result.recomputed_metrics_disturbed).items()},
        },
        "reason": result.reason,
        "descriptive_only": True,
    }


def _write_outcome(output_root: Path, payload: Mapping[str, Any]) -> str:
    return _atomic_write_json(_outcome_path(output_root, payload["slot_id"]), payload)


def _failed_outcome(slot: Mapping[str, Any], digest: str, reason: str, **fields: Any) -> dict[str, Any]:
    payload = {
        "schema": OUTCOME_SCHEMA,
        "slot_id": slot["slot_id"],
        "pair_id": slot["pair_id"],
        "status": "failed",
        "member_valid": False,
        "failed_or_incomplete": True,
        "artifact_sha256": digest,
        "arm": slot["arm"],
        "actual_method": slot["actual_method"],
        "unit_index": slot["unit_index"],
        "resolution_id": slot["resolution_id"],
        "condition": slot["condition"],
        "reason": reason,
        "retained": True,
    }
    payload.update(fields)
    return payload


def _seal_nominal(
    *,
    output_root: Path,
    slot: Mapping[str, Any],
    digest: str,
    artifact: Mapping[str, Any],
    contract: YieldFairContract,
    inspect: Callable[..., Any],
) -> dict[str, Any]:
    try:
        member = inspect(
            artifact, contract, condition="nominal", arm=slot["arm"], actual_method=slot["actual_method"],
        )
    except (YieldMechanismDevelopmentError, YieldFairSelectionError) as error:
        outcome = _failed_outcome(slot, digest, str(error))
        _write_outcome(output_root, outcome)
        return outcome
    if not member.valid:
        outcome = _failed_outcome(
            slot, digest, member.reason or "failed member",
            nominal_feasible=member.nominal_feasible,
            reported_metrics=dict(member.reported_metrics),
            absolute_descriptors=_absolute_from_metrics(member.reported_metrics),
        )
        _write_outcome(output_root, outcome)
        return outcome
    outcome = {
        "schema": OUTCOME_SCHEMA,
        "slot_id": slot["slot_id"],
        "pair_id": slot["pair_id"],
        "status": "complete",
        "member_valid": True,
        "failed_or_incomplete": False,
        "artifact_sha256": digest,
        "arm": slot["arm"],
        "actual_method": slot["actual_method"],
        "unit_index": slot["unit_index"],
        "resolution_id": slot["resolution_id"],
        "condition": slot["condition"],
        "nominal_feasible": member.nominal_feasible,
        "absolute_descriptors": _absolute_from_metrics(member.recomputed_metrics),
        "reported_metrics": dict(member.reported_metrics),
        "recomputed_metrics": dict(member.recomputed_metrics),
        "checks": dict(member.checks),
        "out_of_band": member.nominal_feasible is False,
        "retained": True,
    }
    _write_outcome(output_root, outcome)
    return outcome


def _load_one_artifact(output_root: Path, slot_id: str, *, expected_method: str) -> tuple[dict[str, Any], str]:
    path = _artifact_path(output_root, slot_id)
    if not path.is_file():
        raise YieldMechanismDevelopmentError("sealed artifact missing")
    encoded = path.read_bytes()
    digest = _sha256_bytes(encoded)
    outcome = _terminal(output_root, slot_id)
    if outcome is None or outcome.get("artifact_sha256") != digest:
        raise YieldMechanismDevelopmentError("sealed artifact digest differs; not fabricating success")
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise YieldMechanismDevelopmentError("sealed artifact is not an object")
    if payload.get("method") != expected_method:
        raise YieldMechanismDevelopmentError("existing artifact identity differs; not fabricating success")
    return dict(payload), digest


def _seal_disturbed(
    *,
    output_root: Path,
    slot: Mapping[str, Any],
    digest: str,
    artifact: Mapping[str, Any],
    contract: YieldFairContract,
    inspect: Callable[..., Any],
    pair_eval: Callable[..., Any],
) -> dict[str, Any]:
    nominal_id = f"{slot['pair_id']}-nominal"
    nominal_terminal = _terminal(output_root, nominal_id)
    if nominal_terminal is None:
        outcome = _failed_outcome(slot, digest, "nominal artifact missing", pair_complete=False)
        _write_outcome(output_root, outcome)
        return outcome
    if nominal_terminal.get("status") != "complete" or nominal_terminal.get("member_valid") is not True:
        try:
            member = inspect(
                artifact, contract, condition="disturbed",
                arm=slot["arm"], actual_method=slot["actual_method"],
            )
            member_valid = member.valid
            reported = dict(member.reported_metrics)
            reason = "failed/incomplete nominal; disturbed retained, no performance ranking"
        except (YieldMechanismDevelopmentError, YieldFairSelectionError) as error:
            member_valid = False
            reported = dict(artifact.get("metrics") or {}) if isinstance(artifact.get("metrics"), Mapping) else {}
            reason = str(error)
        outcome = _failed_outcome(
            slot, digest, reason,
            member_valid=member_valid,
            status="failed" if not member_valid else "complete",
            pair_complete=False,
            pair_feasible=False,
            reported_metrics=reported,
            absolute_descriptors=_absolute_from_metrics(reported),
            failed_or_incomplete=True,
        )
        if member_valid:
            outcome["status"] = "complete"
            outcome["member_valid"] = True
            outcome["failed_or_incomplete"] = True
        _write_outcome(output_root, outcome)
        return outcome
    try:
        nominal_artifact, _ = _load_one_artifact(
            output_root, nominal_id, expected_method=slot["actual_method"],
        )
        result = pair_eval(
            nominal_artifact, artifact, contract,
            arm=slot["arm"], actual_method=slot["actual_method"],
        )
    except (YieldMechanismDevelopmentError, YieldFairSelectionError) as error:
        outcome = _failed_outcome(slot, digest, str(error), pair_complete=False)
        _write_outcome(output_root, outcome)
        return outcome
    finally:
        nominal_artifact = None  # drop the pair partner; keep only the current artifact
    if not result.valid:
        outcome = _failed_outcome(slot, digest, result.reason or "failed member", **_pair_fields(result))
        _write_outcome(output_root, outcome)
        return outcome
    outcome = {
        "schema": OUTCOME_SCHEMA,
        "slot_id": slot["slot_id"],
        "pair_id": slot["pair_id"],
        "status": "complete",
        "member_valid": True,
        "failed_or_incomplete": False,
        "artifact_sha256": digest,
        "arm": slot["arm"],
        "actual_method": slot["actual_method"],
        "unit_index": slot["unit_index"],
        "resolution_id": slot["resolution_id"],
        "condition": slot["condition"],
        "out_of_band": result.pair_feasible is False,
        "retained": True,
        **_pair_fields(result),
    }
    _write_outcome(output_root, outcome)
    return outcome


def _verify_sealed_files(output_root: Path, slots: Sequence[Mapping[str, Any]]) -> None:
    for slot in slots:
        terminal = _terminal(output_root, slot["slot_id"])
        if terminal is None:
            continue
        digest = terminal.get("artifact_sha256")
        path = _artifact_path(output_root, slot["slot_id"])
        if not path.is_file():
            raise YieldMechanismDevelopmentError(f"sealed artifact {slot['slot_id']} is missing")
        if not _is_sha256(digest) or _file_sha256(path) != digest:
            raise YieldMechanismDevelopmentError(
                f"sealed artifact {slot['slot_id']} digest differs; not fabricating success"
            )


def _next_slot(output_root: Path, slots: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    inflight = _inflight(output_root)
    if inflight is not None:
        raise YieldMechanismDevelopmentError(
            f"inflight attempt {inflight['slot_id']} must be reconciled; refusing to rerun"
        )
    for slot in slots:
        if _terminal(output_root, slot["slot_id"]) is not None:
            continue
        artifact = _artifact_path(output_root, slot["slot_id"])
        if artifact.exists():
            raise FileExistsError(f"refusing to overwrite existing artifact: {artifact}")
        return dict(slot)
    return None


def campaign_status(output: Path | str) -> dict[str, Any]:
    state = _load_state(output)
    output_root = Path(state["output_root"])
    _verify_bindings(state)
    slots = _load_manifest(output_root)
    _verify_sealed_files(output_root, slots)
    inflight = _inflight(output_root)
    executed = failed = complete = out_of_band = 0
    for slot in slots:
        terminal = _terminal(output_root, slot["slot_id"])
        if terminal is None:
            continue
        executed += 1
        if terminal.get("status") == "failed":
            failed += 1
        elif terminal.get("status") == "complete":
            complete += 1
        if terminal.get("out_of_band") is True:
            out_of_band += 1
    nxt = None
    if inflight is None:
        pending = [slot for slot in slots if _terminal(output_root, slot["slot_id"]) is None]
        nxt = pending[0] if pending else {"complete": True}
    else:
        nxt = {"blocked_by_inflight": inflight["slot_id"]}
    return {
        "schema": STATE_SCHEMA,
        "output_root": str(output_root),
        "inflight": inflight,
        "next": nxt,
        "executed_trials": executed,
        "failed": failed,
        "complete": complete,
        "out_of_band_retained": out_of_band,
        "remaining_slots": TOTAL_TRIALS - executed,
        "formal_validation_complete": False,
        "holdout_used_for_tuning": False,
        "training_budget": 0,
        "validation_budget": 0,
        "claim_scope": CLAIM_SCOPE,
        "maximum_total_trials": TOTAL_TRIALS,
        "dataset_role": "development",
    }


def _clear_inflight(output_root: Path, slot_id: str) -> None:
    path = _inflight_path(output_root)
    current = _read_json(path)
    if current is None:
        return
    if current.get("slot_id") != slot_id:
        raise YieldMechanismDevelopmentError("inflight slot differs")
    path.unlink()


def run_next(
    output: Path | str,
    *,
    runner: Callable[..., Mapping[str, Any]] | None = None,
    inspect: Callable[..., Any] | None = None,
    pair_eval: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state = _load_state(output)
    with _exclusive(Path(state["output_root"])):
        _verify_bindings(state)
        output_root = Path(state["output_root"])
        slots = _load_manifest(output_root)
        _verify_sealed_files(output_root, slots)
        slot = _next_slot(output_root, slots)
        if slot is None:
            return {
                "status": "schedule_complete",
                "executed_trials": campaign_status(output_root)["executed_trials"],
                "formal_validation_complete": False,
                "dataset_role": "development",
            }
        contract = _slot_contract(state, slot)
        execute = runner or _default_runner
        inspect_fn = inspect or inspect_member
        pair_fn = pair_eval or evaluate_pair
        _require_bound_binaries(state)
        inflight_payload = {
            "slot_id": slot["slot_id"],
            "pair_id": slot["pair_id"],
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "condition": slot["condition"],
            "index": slot["index"],
            "registered": True,
        }
        _atomic_write_json(_inflight_path(output_root), inflight_payload)
        kwargs = _run_kwargs(state, slot, contract)
        artifact = execute(**kwargs)
        if not isinstance(artifact, Mapping):
            raise YieldMechanismDevelopmentError("runner must return an artifact object")
        if artifact.get("method") != slot["actual_method"]:
            raise YieldMechanismDevelopmentError("runner artifact method differs; not relabeling")
        if artifact.get("campaign_kind") in {"training", "holdout"}:
            raise YieldMechanismDevelopmentError("training/holdout rows cannot enter this development diagnostic")
        identity = artifact.get("identity_payload")
        if isinstance(identity, Mapping) and isinstance(identity.get("parameters"), Mapping):
            if not _params_match(identity["parameters"], dict(slot["law_parameters"])):
                raise YieldMechanismDevelopmentError("actual law parameters differ from the candidate")
        _verify_bindings(state)
        digest = _atomic_write_json(_artifact_path(output_root, slot["slot_id"]), artifact)
        if slot["condition"] == "nominal":
            sealed = _seal_nominal(
                output_root=output_root, slot=slot, digest=digest, artifact=artifact,
                contract=contract, inspect=inspect_fn,
            )
        else:
            sealed = _seal_disturbed(
                output_root=output_root, slot=slot, digest=digest, artifact=artifact,
                contract=contract, inspect=inspect_fn, pair_eval=pair_fn,
            )
        _clear_inflight(output_root, slot["slot_id"])
        return {
            "slot_id": slot["slot_id"],
            "pair_id": slot["pair_id"],
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "condition": slot["condition"],
            "artifact_path": str(_artifact_path(output_root, slot["slot_id"])),
            "artifact_sha256": digest,
            "formal_validation_complete": False,
            "dataset_role": "development",
            **sealed,
        }


def _numeric_delta(left: Any, right: Any) -> float | None:
    if left is None or right is None or isinstance(left, bool) or isinstance(right, bool):
        return None
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(a) or not math.isfinite(b):
        return None
    return a - b


def campaign_report(output: Path | str) -> dict[str, Any]:
    state = _load_state(output)
    output_root = Path(state["output_root"])
    _verify_bindings(state)
    slots = _load_manifest(output_root)
    _verify_sealed_files(output_root, slots)
    members = []
    pairs: dict[str, dict[str, Any]] = {}
    for slot in slots:
        terminal = _terminal(output_root, slot["slot_id"])
        members.append({
            "slot_id": slot["slot_id"],
            "pair_id": slot["pair_id"],
            "unit_index": slot["unit_index"],
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "resolution_id": slot["resolution_id"],
            "condition": slot["condition"],
            "dt_s": slot["controller_dt_s"],
            "plant_substeps": slot["plant_substeps"],
            "m": slot["m"],
            "mu": slot["mu"],
            "g": slot["g"],
            "outcome": terminal,
            "retained": True if terminal is not None else None,
        })
        bucket = pairs.setdefault(slot["pair_id"], {
            "pair_id": slot["pair_id"],
            "unit_index": slot["unit_index"],
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "resolution_id": slot["resolution_id"],
            "dt_s": slot["controller_dt_s"],
            "plant_substeps": slot["plant_substeps"],
            "m": slot["m"],
            "mu": slot["mu"],
            "g": slot["g"],
            "members": {},
        })
        bucket["members"][slot["condition"]] = terminal
        if slot["condition"] == "disturbed" and terminal is not None:
            bucket["pair_complete"] = terminal.get("pair_complete")
            bucket["pair_feasible"] = terminal.get("pair_feasible")
            bucket["out_of_band"] = terminal.get("out_of_band")
            bucket["objective"] = terminal.get("objective")
            bucket["absolute_descriptors"] = dict(terminal.get("absolute_descriptors") or {})
            bucket["reported_recovery"] = dict(terminal.get("reported_recovery") or {})
            bucket["reason"] = terminal.get("reason")
            bucket["status"] = terminal.get("status")
    arm_matches = []
    for unit in SELECTED_UNITS:
        for resolution_id, dt_s, sub in RESOLUTION_SETTINGS:
            present = []
            for arm in ARMS:
                pair_id = f"u{unit}-{arm}-{resolution_id}"
                row = pairs.get(pair_id)
                if row is not None and row["members"].get("nominal") is not None and row["members"].get("disturbed") is not None:
                    present.append(arm)
            for i, arm_a in enumerate(present):
                for arm_b in present[i + 1:]:
                    left = pairs[f"u{unit}-{arm_a}-{resolution_id}"]
                    right = pairs[f"u{unit}-{arm_b}-{resolution_id}"]
                    keys = sorted(set(left.get("absolute_descriptors") or {}) & set(right.get("absolute_descriptors") or {}))
                    arm_matches.append({
                        "kind": "matched_arm",
                        "unit_index": unit,
                        "resolution_id": resolution_id,
                        "dt_s": dt_s,
                        "plant_substeps": sub,
                        "arm_a": arm_a,
                        "arm_b": arm_b,
                        "descriptor_delta_a_minus_b": {
                            key: _numeric_delta(
                                (left.get("absolute_descriptors") or {}).get(key),
                                (right.get("absolute_descriptors") or {}).get(key),
                            )
                            for key in keys
                        },
                        "failed_or_incomplete": (
                            left.get("pair_complete") is not True or right.get("pair_complete") is not True
                        ),
                        "winner": None,
                    })
    resolution_diffs = []
    names = [item[0] for item in RESOLUTION_SETTINGS]
    by_setting = {name: dt for name, dt, _ in RESOLUTION_SETTINGS}
    for unit in SELECTED_UNITS:
        for arm in ARMS:
            rows = {name: pairs.get(f"u{unit}-{arm}-{name}") for name in names}
            if any(row is None or row["members"].get("nominal") is None or row["members"].get("disturbed") is None for row in rows.values()):
                continue
            base = rows["base"]
            controller = rows["controller_refinement"]
            plant = rows["plant_refinement"]
            keys = sorted(
                set(base.get("absolute_descriptors") or {})
                & set(controller.get("absolute_descriptors") or {})
                & set(plant.get("absolute_descriptors") or {})
            )
            resolution_diffs.append({
                "kind": "resolution_dt_difference",
                "unit_index": unit,
                "arm": arm,
                "dt_s": {name: by_setting[name] for name in names},
                "controller_refinement_minus_base": {
                    key: _numeric_delta(
                        (controller.get("absolute_descriptors") or {}).get(key),
                        (base.get("absolute_descriptors") or {}).get(key),
                    )
                    for key in keys
                },
                "plant_refinement_minus_base": {
                    key: _numeric_delta(
                        (plant.get("absolute_descriptors") or {}).get(key),
                        (base.get("absolute_descriptors") or {}).get(key),
                    )
                    for key in keys
                },
                "winner": None,
            })
    return {
        "schema": "ur10e.yield-mechanism-development-report-v1",
        "output_root": str(output_root),
        "dataset_role": "development",
        "not_validation": True,
        "data_selection": dict(DATA_SELECTION),
        "members": members,
        "pairs": list(pairs.values()),
        "matched_comparisons": arm_matches,
        "resolution_dt_differences": resolution_diffs,
        "no_j_pooling": True,
        "no_ci": True,
        "no_cross_resolution_pooling": True,
        "winner": None,
        "causal_inference": False,
        "formal_validation_complete": False,
        "training_budget": 0,
        "validation_budget": 0,
        "claim_scope": CLAIM_SCOPE,
        "fullstate_limit": FULLSTATE_LIMIT,
        "negatives_retained": True,
        "task_tolerances_unchanged": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    create = sub.add_parser("create", help="write the immutable 60-slot development manifest")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--qp-library", type=Path, default=None)
    create.add_argument("--native-laws-root", type=Path, default=None)
    create.add_argument("--experiment-root", type=Path, default=None)
    status = sub.add_parser("status", help="show finite-schedule progress without launching trials")
    status.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run-next", help="register and run exactly one pending diagnostic slot")
    run.add_argument("--output", type=Path, required=True)
    rep = sub.add_parser("report", help="emit matched comparisons without pooling or a winner")
    rep.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "create":
        result = create_campaign(
            args.output,
            experiment_root=args.experiment_root,
            qp_library=args.qp_library,
            native_laws_root=args.native_laws_root,
        )
    elif args.command == "status":
        result = campaign_status(args.output)
    elif args.command == "run-next":
        result = run_next(args.output)
    elif args.command == "report":
        result = campaign_report(args.output)
    else:
        parser.error("unknown command")
        return 2
    print(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
