#!/usr/bin/env python3
"""Offline reserved-cell validation plumbing. Does not launch validation on import.

Explicit create/status/run-next/reconcile/report only. One native attempt at a
time. Main owns scientific acceptance and any full-period execution. No
training writes and no holdout-to-training path.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import numpy as np

from contact_benchmark_ledger import canonical as ledger_canonical
from contact_yield_protocol import PERIOD_S, Task, protocol
from contact_yield_runner import HOME_PATH
from yield_contact_ledger import YieldContactLedger
from yield_contact_tuner import METHODS as TRAINING_METHODS, YieldContactTuner, YieldContactTunerError
from yield_fair_campaign import _execution_identities as training_execution_identities
from yield_fair_selection import OBSERVER_V3
from yield_validation_selection import (
    ARMS,
    ARM_ACTUAL_METHOD,
    CLAIM_SCOPE,
    DEFAULT_RESERVATION_PATH,
    LOAD_ONSET_S,
    ATTITUDE_ONSET_S,
    JLOAD_SCALE_N,
    JPATH_SCALE_M,
    JATT_SCALE_RAD,
    MSFC_IDENTITY_METRIC,
    RESERVATION_SCHEMA,
    RESOLUTION_SETTINGS,
    TRAINING_FREEZE_SCHEMA,
    YieldValidationContract,
    YieldValidationPairResult,
    YieldValidationSelectionError,
    arm_law_parameters,
    bind_arm,
    compare_resolution_settings,
    derive_prior_basis,
    evaluate_pair,
    inspect_member,
    load_reservation,
    prior_inward_normal,
    scenario_release_s,
)


CAMPAIGN_STATE_SCHEMA = "ur10e.yield-validation-campaign-state-v1"
TRAINING_CAMPAIGN_STATE_SCHEMA = "ur10e.yield-fair-campaign-state-v1"
INTERRUPT_SCHEMA = "ur10e.yield-validation-interruption-v1"
SKIP_SCHEMA = "ur10e.yield-validation-skipped-refinement-v1"
OUTCOME_SCHEMA = "ur10e.yield-validation-outcome-v1"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FAIR_CONFIG = EXPERIMENT_ROOT / "config" / "yield_fair_campaign_v1.json"
REQUIRED_FREEZE_IDENTITY_FIELDS = (
    "selected_candidates",
    "selection_identities",
    "source_hashes",
    "execution_identities",
    "campaign_config_sha256",
    "tuner_config_sha256",
    "observer_config_sha256",
    "yield_protocol_sha256",
    "campaign_protocol_sha256",
    "training_cell_id",
    "selection_contract_id",
    "freeze_sha256",
)
LEGAL_TRAINING_ATTEMPT_STATUS = frozenset({"complete", "failed", "interrupted", "censored"})
TRAINING_LEDGER_BINDINGS_SCHEMA = "yield-contact-training-ledger-v1"
SCIENTIFIC_FILES = (
    "tools/yield_validation_selection.py",
    "tools/yield_validation_runner.py",
    "tools/yield_fair_selection.py",
    "tools/yield_fair_campaign.py",
    "tools/yield_contact_ledger.py",
    "tools/yield_contact_tuner.py",
)
NATIVE_FILES = (
    "tools/contact_laws.py",
    "tools/contact_qp.py",
    "tools/build_contact_laws.py",
)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class YieldValidationCampaignError(ValueError):
    """Invalid validation command, binding, or restart state."""


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
        raise YieldValidationCampaignError("artifact digest drifted during write")
    return digest


def _source_hashes(root: Path) -> dict[str, str]:
    """Raw contact_yield_*.py hashes. Matches the training freeze source identity."""
    paths = {item.name: item for item in (root / "tools").glob("contact_yield_*.py")}
    if not paths:
        raise YieldValidationCampaignError("contact_yield source files missing")
    return {name: _file_sha256(path) for name, path in sorted(paths.items())}


def _hash_relative_files(root: Path, relatives: tuple[str, ...]) -> dict[str, str]:
    hashed = {}
    for relative in relatives:
        path = root / relative
        if not path.is_file():
            raise YieldValidationCampaignError(f"execution identity file missing: {relative}")
        hashed[relative] = _file_sha256(path)
    return hashed


def _load_json_object(path: Path, role: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise YieldValidationCampaignError(f"{role} must not be a symlink: {path}")
    raw = path.read_bytes()
    root = json.loads(raw.decode("utf-8"))
    if not isinstance(root, Mapping):
        raise YieldValidationCampaignError(f"{role} must be an object")
    return dict(root), raw


def _fair_runtime(experiment_root: Path) -> dict[str, Any]:
    config, _ = _load_json_object(experiment_root / "config" / "yield_fair_campaign_v1.json", "fair campaign config")
    runtime = config.get("runtime") if isinstance(config.get("runtime"), Mapping) else {}
    feasibility = config.get("feasibility") if isinstance(config.get("feasibility"), Mapping) else {}
    return {
        "qp_library": str(runtime.get("qp_library") or "build/contact-qp/libcontact_qp.so"),
        "native_laws_root": str(runtime.get("native_laws_root") or "build/contact-six-laws"),
        "observer_config": str(config.get("observer_config") or "config/yield_normal_observer_v3.json"),
        "tuner_config": str(config.get("tuner_config") or "config/yield_fair_tuning_v1.json"),
        "feasibility": dict(feasibility),
        "outer": dict(config.get("outer") or {}),
    }


def _execution_identities(root: Path) -> dict[str, Any]:
    runtime = _fair_runtime(root)
    qp_library = root / runtime["qp_library"]
    laws_root = root / runtime["native_laws_root"]
    qp_sha = _file_sha256(qp_library) if qp_library.is_file() else None
    laws_sha = None
    if laws_root.is_dir():
        libraries = sorted(path for path in laws_root.rglob("*.so") if path.is_file())
        if libraries:
            laws_sha = {str(path.relative_to(root)): _file_sha256(path) for path in libraries}
    return {
        "scientific_files": _hash_relative_files(root, SCIENTIFIC_FILES),
        "native_files": _hash_relative_files(root, NATIVE_FILES),
        "qp_library": str(qp_library),
        "qp_library_sha256": qp_sha,
        "native_laws_root": str(laws_root),
        "native_laws_sha256": laws_sha,
    }


def _observer_sha256(root: Path) -> str:
    runtime = _fair_runtime(root)
    observer_path = root / runtime["observer_config"]
    payload = json.loads(observer_path.read_text(encoding="utf-8"))
    if payload != OBSERVER_V3:
        raise YieldValidationCampaignError("observer config drifted from yield_normal_observer_v3")
    return _file_sha256(observer_path)


def _thread_lock(output_root: Path) -> threading.Lock:
    key = str(output_root)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _exclusive_campaign(output_root: Path):
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


def _plant_identity(experiment_root: Path) -> dict[str, Any]:
    from contact_yield_kinematics import load_kinematics
    kinematics = load_kinematics(require_ur10e=True)
    return {
        "kinematics_kind": kinematics.kind,
        "calibration_hash": kinematics.calibration_hash,
        "claim_scope": kinematics.claim_scope,
        "home_path": str(HOME_PATH),
        "home_sha256": _file_sha256(Path(HOME_PATH)),
    }


def calibrated_prior_basis(experiment_root: Path) -> dict[str, list[float]]:
    from contact_yield_kinematics import load_kinematics
    kinematics = load_kinematics(require_ur10e=True)
    if kinematics.kind != "ur10e_calibrated_pinocchio":
        raise YieldValidationCampaignError("validation requires ur10e_calibrated_pinocchio")
    home = json.loads(Path(HOME_PATH).read_text(encoding="utf-8"))
    q = np.asarray(home["home_q"], dtype=float)
    pose = kinematics.pose_and_jacobian(q)
    rotation = np.asarray(pose["rotation"], dtype=float)
    # Tool +Z is the preserved contact approach, hence the inward direction.
    approach = rotation[:, 2].copy()
    velocity = np.asarray(Task().reference(0.0)["velocity_m_s"], dtype=float)
    basis = derive_prior_basis(approach=approach, reference_velocity_m_s=velocity)
    return {name: vector.tolist() for name, vector in basis.items()}


def _training_ledger_bindings(tuner: YieldContactTuner, campaign_protocol_sha256: str) -> dict[str, str]:
    return {
        "schema": TRAINING_LEDGER_BINDINGS_SCHEMA,
        "split": "training",
        "campaign_protocol_sha256": campaign_protocol_sha256,
        "tuner_config_sha256": tuner.config_sha256,
        "training_cell_id": tuner.training_cell_id,
        "selection_contract_id": tuner.selection_contract_id,
    }


def _require_freeze_identities(payload: Mapping[str, Any]) -> None:
    missing = [name for name in REQUIRED_FREEZE_IDENTITY_FIELDS if not payload.get(name)]
    if missing:
        raise YieldValidationCampaignError(
            f"training freeze missing bound identities {missing}; selected-candidate hash is not a training freeze"
        )
    for name in (
        "campaign_config_sha256",
        "tuner_config_sha256",
        "observer_config_sha256",
        "yield_protocol_sha256",
        "campaign_protocol_sha256",
        "freeze_sha256",
    ):
        if not _is_sha256(payload.get(name)):
            raise YieldValidationCampaignError(f"training freeze {name} is missing")
    if not isinstance(payload.get("source_hashes"), Mapping) or not payload["source_hashes"]:
        raise YieldValidationCampaignError("training freeze source_hashes are missing")
    if not isinstance(payload.get("execution_identities"), Mapping) or not payload["execution_identities"]:
        raise YieldValidationCampaignError("training freeze execution_identities are missing")
    if not isinstance(payload.get("selection_identities"), Mapping):
        raise YieldValidationCampaignError("training freeze selection_identities are missing")
    if payload.get("holdout_implemented") is True:
        raise YieldValidationCampaignError("training freeze must not implement holdout")
    if payload.get("holdout_used_for_tuning") is True:
        raise YieldValidationCampaignError("holdout-to-training path is refused")


def _load_training_campaign_export(freeze_path: Path, payload: Mapping[str, Any]) -> tuple[dict[str, Any], Path]:
    export_path = freeze_path.parent / "campaign.json"
    ledger_sibling = freeze_path.parent / "campaign.sqlite"
    if not export_path.is_file():
        raise YieldValidationCampaignError("training campaign export is missing")
    if not ledger_sibling.is_file():
        raise YieldValidationCampaignError("training freeze ledger is missing")
    state, _ = _load_json_object(export_path, "training campaign export")
    if state.get("schema") != TRAINING_CAMPAIGN_STATE_SCHEMA:
        raise YieldValidationCampaignError("training campaign export schema differs")
    if state.get("campaign_protocol_sha256") != payload["campaign_protocol_sha256"]:
        raise YieldValidationCampaignError("freeze campaign_protocol_sha256 differs from the training export")
    protocol_payload = state.get("campaign_protocol")
    if not isinstance(protocol_payload, Mapping):
        raise YieldValidationCampaignError("training campaign protocol is missing")
    for key in (
        "source_hashes",
        "execution_identities",
        "tuner_config_sha256",
        "observer_config_sha256",
        "yield_protocol_sha256",
        "training_cell_id",
        "selection_contract_id",
    ):
        if protocol_payload.get(key) != payload.get(key):
            raise YieldValidationCampaignError(f"training freeze {key} differs from the campaign export")
    if state.get("config_sha256") != payload.get("campaign_config_sha256"):
        raise YieldValidationCampaignError("training freeze campaign_config_sha256 differs from the export")
    recorded = Path(state["ledger_path"]).expanduser().resolve()
    if recorded != ledger_sibling.resolve():
        raise YieldValidationCampaignError("training ledger path is unbound from the freeze export")
    output_root = Path(state["output_root"]).expanduser().resolve()
    if output_root != freeze_path.parent.resolve():
        raise YieldValidationCampaignError("training freeze is unbound from its campaign export root")
    return state, recorded


def _inspect_bound_training_ledger(
    ledger_path: Path,
    *,
    selected: Mapping[str, Any],
    freeze_sha256: str,
    bindings: Mapping[str, str],
) -> None:
    if not ledger_path.is_file():
        raise YieldValidationCampaignError("training freeze ledger is missing")
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    try:
        try:
            protocol_row = conn.execute("SELECT value FROM metadata WHERE key='protocol'").fetchone()
            frozen_row = conn.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()
            scope_row = conn.execute("SELECT value FROM metadata WHERE key='controllers'").fetchone()
        except sqlite3.Error as error:
            raise YieldValidationCampaignError(f"training freeze ledger is unreadable: {error}") from error
        expected_protocol = hashlib.sha256(ledger_canonical(dict(bindings)).encode()).hexdigest()
        if protocol_row is None or protocol_row[0] != expected_protocol:
            raise YieldValidationCampaignError("training ledger protocol is unbound from the freeze identities")
        if scope_row is None or scope_row[0] != ledger_canonical(TRAINING_METHODS):
            raise YieldValidationCampaignError("training ledger controller scope differs")
        if frozen_row is None or not frozen_row[0]:
            raise YieldValidationCampaignError("training freeze ledger has no frozen export")
        if json.loads(frozen_row[0]) != json.loads(_canonical(dict(selected))):
            raise YieldValidationCampaignError("freeze selected_candidates differ from the source ledger")
        if hashlib.sha256(frozen_row[0].encode("utf-8")).hexdigest() != freeze_sha256:
            raise YieldValidationCampaignError("freeze_sha256 differs from the source ledger export")
        running = conn.execute("SELECT id FROM attempts WHERE status='running'").fetchone()
        if running is not None:
            raise YieldValidationCampaignError("source ledger still has an inflight attempt")
        seen_ids: set[str] = set()
        for method in TRAINING_METHODS:
            units = conn.execute(
                "SELECT number,candidate FROM units WHERE controller=? ORDER BY number",
                (method,),
            ).fetchall()
            if [number for number, _ in units] != list(range(24)):
                raise YieldValidationCampaignError("source ledger freeze is incomplete")
            for number, _candidate in units:
                attempts = conn.execute(
                    "SELECT id,condition,status,evidence FROM attempts WHERE controller=? AND unit=?",
                    (method, number),
                ).fetchall()
                if len(attempts) != 2:
                    raise YieldValidationCampaignError("missing pair")
                conditions = []
                for attempt_id, condition, status, evidence in attempts:
                    if attempt_id in seen_ids:
                        raise YieldValidationCampaignError("attempt duplicate")
                    seen_ids.add(attempt_id)
                    if condition not in {"nominal", "disturbed"}:
                        raise YieldValidationCampaignError("missing pair")
                    if status not in LEGAL_TRAINING_ATTEMPT_STATUS:
                        raise YieldValidationCampaignError("illegal status")
                    if not evidence:
                        raise YieldValidationCampaignError("mismatched evidence")
                    payload = json.loads(evidence)
                    if not isinstance(payload, Mapping):
                        raise YieldValidationCampaignError("mismatched evidence")
                    for key, expected in bindings.items():
                        if payload.get(key) != expected:
                            raise YieldValidationCampaignError("mismatched evidence")
                    conditions.append(condition)
                if set(conditions) != {"nominal", "disturbed"}:
                    raise YieldValidationCampaignError("missing pair")
            extra = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE controller=?", (method,)
            ).fetchone()[0]
            if extra != 48:
                raise YieldValidationCampaignError("attempt duplicate")
            found = conn.execute(
                "SELECT number FROM units WHERE controller=? AND candidate=?",
                (method, ledger_canonical(selected[method])),
            ).fetchall()
            if not found:
                raise YieldValidationCampaignError("selected candidate was not evaluated")
            admitted = False
            for (number,) in found:
                pair = {
                    condition: json.loads(evidence)
                    for condition, evidence in conn.execute(
                        "SELECT condition,evidence FROM attempts WHERE controller=? AND unit=? AND status='complete'",
                        (method, number),
                    )
                }
                if set(pair) != {"nominal", "disturbed"}:
                    continue
                objective = pair["disturbed"].get("objective")
                if (
                    pair["nominal"].get("nominal_feasible") is True
                    and not isinstance(objective, bool)
                    and isinstance(objective, (int, float))
                    and math.isfinite(objective)
                    and objective >= 0
                ):
                    admitted = True
                    break
            if not admitted:
                raise YieldValidationCampaignError("selected candidate lacks a complete feasible nominal/disturbed pair")
    finally:
        conn.close()


def _verify_training_schedule(
    ledger_path: Path,
    *,
    tuner: YieldContactTuner,
    campaign_protocol_sha256: str,
    selected: Mapping[str, Any],
) -> None:
    handle, copy_path = tempfile.mkstemp(prefix="yield-validation-freeze-", suffix=".sqlite")
    os.close(handle)
    ledger = None
    try:
        shutil.copy2(ledger_path, copy_path)
        ledger = YieldContactLedger(
            Path(copy_path),
            tuner=tuner,
            campaign_protocol_sha256=campaign_protocol_sha256,
        )
        for method in TRAINING_METHODS:
            try:
                history = ledger.training_observations(method)
            except ValueError as error:
                raise YieldValidationCampaignError(f"training observations are illegal: {error}") from error
            if len(history) != 24:
                raise YieldValidationCampaignError("source ledger freeze is incomplete")
            for index, row in enumerate(history):
                expected = tuner.propose(method, history[:index], index).candidate
                actual = tuner._parse_candidate(method, row.candidate)
                if actual.key != expected.key:
                    raise YieldValidationCampaignError("recorded candidate differs from deterministic proposal schedule")
            incumbent = tuner.propose(method, history[:20], 20).candidate
            actual = tuner._parse_candidate(method, selected[method])
            if actual.key != incumbent.key:
                raise YieldValidationCampaignError("selection differs from frozen discovery incumbent")
    finally:
        if ledger is not None:
            ledger.close()
        Path(copy_path).unlink(missing_ok=True)


def load_training_freeze(
    path: Path | str,
    *,
    experiment_root: Path | str | None = None,
) -> dict[str, Any]:
    freeze_path = Path(path).expanduser().resolve()
    payload, raw = _load_json_object(freeze_path, "training freeze")
    if payload.get("valid") is True and payload.get("schema") != TRAINING_FREEZE_SCHEMA:
        raise YieldValidationCampaignError("invalid freeze cannot pass by setting a boolean")
    if payload.get("complete") is True and payload.get("schema") != TRAINING_FREEZE_SCHEMA:
        raise YieldValidationCampaignError("invalid freeze cannot pass by setting a boolean")
    if payload.get("schema") != TRAINING_FREEZE_SCHEMA or payload.get("version") != 1:
        raise YieldValidationCampaignError("training freeze schema/version differs")
    _require_freeze_identities(payload)
    selected = payload.get("selected_candidates")
    if not isinstance(selected, Mapping) or set(selected) != set(TRAINING_METHODS):
        raise YieldValidationCampaignError("freeze must export complete SFC, DSFC and MSFC incumbents")
    freeze_sha256 = payload["freeze_sha256"]
    expected_digest = hashlib.sha256(_canonical(dict(selected)).encode("utf-8")).hexdigest()
    if freeze_sha256 != expected_digest:
        raise YieldValidationCampaignError("freeze_sha256 does not bind the exported selected candidates")
    experiment_root = Path(experiment_root or EXPERIMENT_ROOT).resolve()
    if _source_hashes(experiment_root) != dict(payload["source_hashes"]):
        raise YieldValidationCampaignError("source identity drifted")
    training_config, training_raw = _load_json_object(
        experiment_root / "config/yield_fair_campaign_v1.json", "training config")
    if _sha256_bytes(training_raw) != payload["campaign_config_sha256"]:
        raise YieldValidationCampaignError("training campaign config drifted")
    if training_execution_identities(experiment_root, training_config) != payload["execution_identities"]:
        raise YieldValidationCampaignError("training execution identity drifted")
    if _observer_sha256(experiment_root) != payload["observer_config_sha256"]:
        raise YieldValidationCampaignError("observer config drifted")
    if protocol()["sha256"] != payload["yield_protocol_sha256"]:
        raise YieldValidationCampaignError("yield protocol drifted")
    tuner = YieldContactTuner(
        experiment_root / "config" / "yield_fair_tuning_v1.json",
        training_cell_id=str(payload["training_cell_id"]),
        selection_contract_id=str(payload["selection_contract_id"]),
    )
    if tuner.config_sha256 != payload["tuner_config_sha256"]:
        raise YieldValidationCampaignError("tuner config drifted")
    parsed = {}
    try:
        for method in TRAINING_METHODS:
            candidate = tuner._parse_candidate(method, selected[method])
            parsed[method] = candidate.as_dict()
            if candidate.method != method:
                raise YieldValidationCampaignError("freeze candidate method differs")
    except YieldContactTunerError as error:
        raise YieldValidationCampaignError(f"incomplete or invalid freeze candidate: {error}") from error
    msfc = arm_law_parameters("MSFC", parsed)
    if msfc.get("minimum_metric_eigenvalue") == MSFC_IDENTITY_METRIC:
        raise YieldValidationCampaignError("frozen MSFC incumbent is already identity-metric; identity is an ablation")
    campaign_state, ledger_path = _load_training_campaign_export(freeze_path, payload)
    if _sha256_bytes(_canonical(campaign_state["campaign_protocol"]).encode()) != payload["campaign_protocol_sha256"]:
        raise YieldValidationCampaignError("training campaign protocol digest differs")
    bindings = _training_ledger_bindings(tuner, payload["campaign_protocol_sha256"])
    _inspect_bound_training_ledger(
        ledger_path,
        selected=parsed,
        freeze_sha256=freeze_sha256,
        bindings=bindings,
    )
    _verify_training_schedule(
        ledger_path,
        tuner=tuner,
        campaign_protocol_sha256=payload["campaign_protocol_sha256"],
        selected=parsed,
    )
    return {
        "path": str(freeze_path),
        "sha256": _sha256_bytes(raw),
        "freeze_sha256": freeze_sha256,
        "selected_candidates": parsed,
        "selection_identities": payload["selection_identities"],
        "source_hashes": dict(payload["source_hashes"]),
        "execution_identities": dict(payload["execution_identities"]),
        "campaign_protocol_sha256": payload["campaign_protocol_sha256"],
        "training_cell_id": payload["training_cell_id"],
        "selection_contract_id": payload["selection_contract_id"],
        "formal_campaign_complete": bool(payload.get("formal_campaign_complete")),
        "holdout_implemented": False,
        "ledger_path": str(ledger_path),
        "campaign_export_path": str(freeze_path.parent / "campaign.json"),
        "campaign_export": campaign_state,
        "payload": payload,
    }


def build_manifest(reservation: Mapping[str, Any]) -> list[dict[str, Any]]:
    slots = []
    index = 0
    for cell in reservation["cells"]:
        for arm in ARMS:
            for resolution_id, dt_s, plant_substeps in RESOLUTION_SETTINGS:
                for condition in ("nominal", "disturbed"):
                    slots.append({
                        "index": index,
                        "slot_id": f"{cell['id']}-{arm}-{resolution_id}-{condition}",
                        "cell_id": cell["id"],
                        "arm": arm,
                        "actual_method": ARM_ACTUAL_METHOD[arm],
                        "resolution_id": resolution_id,
                        "condition": condition,
                        "controller_dt_s": dt_s,
                        "plant_substeps": plant_substeps,
                        "scenario": cell["nominal_scenario"] if condition == "nominal" else cell["disturbed_scenario"],
                        "material": cell["material"],
                        "preparation": cell["preparation"],
                    })
                    index += 1
    if len(slots) != 180:
        raise YieldValidationCampaignError("finite validation manifest must contain 180 slots")
    return slots


def _cell_map(reservation: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {cell["id"]: cell for cell in reservation["cells"]}


def _training_bands(experiment_root: Path) -> dict[str, float]:
    runtime = _fair_runtime(experiment_root)
    nominal = dict((runtime["feasibility"].get("nominal") or {}))
    guards = dict((runtime["feasibility"].get("disturbed_guards") or {}))
    attitude_deg = float(nominal.get("attitude_rms_deg_max", 2.81))
    return {
        "nominal_path_rms_m_max": float(nominal.get("path_rms_m_max", 0.00548)),
        "nominal_progress_ratio_min": float(nominal.get("progress_ratio_min", 0.85)),
        "nominal_attitude_rms_rad_max": math.radians(attitude_deg),
        "nominal_load_mae_n_max": float(nominal.get("load_mae_n_max", 0.127)),
        "nominal_load_peak_n_max": float(nominal.get("load_peak_n_max", 6.27)),
        "nominal_load_min_n_min": float(nominal.get("load_min_n_min", 1.0)),
        "disturbed_load_min_n_min": float(guards.get("load_min_n_min", 1.0)),
        "disturbed_load_peak_n_max": float(guards.get("load_peak_n_max", 8.0)),
        "disturbed_progress_ratio_min": float(guards.get("progress_ratio_min", 0.85)),
    }


def _slot_contract(
    state: Mapping[str, Any],
    slot: Mapping[str, Any],
) -> YieldValidationContract:
    cell = _cell_map(state["reservation"])[slot["cell_id"]]
    basis = state["prior_basis"]
    prior = prior_inward_normal(
        approach=basis["approach"],
        along=basis["along"],
        across=basis["across"],
        direction=cell["prior_direction"],
        angle_deg=cell["prior_angle_deg"],
    )
    parameters = arm_law_parameters(slot["arm"], state["selected_candidates"])
    bands = state["training_bands"]
    outer = state["outer"]
    return YieldValidationContract(
        cell_id=cell["id"],
        arm=slot["arm"],
        actual_method=slot["actual_method"],
        material=cell["material"],
        kappa_xx=cell["surface_parameters"]["kappa_xx"],
        kappa_yy=cell["surface_parameters"]["kappa_yy"],
        prior_direction=cell["prior_direction"],
        prior_angle_deg=cell["prior_angle_deg"],
        preparation=cell["preparation"],
        nominal_scenario=cell["nominal_scenario"],
        disturbed_scenario=cell["disturbed_scenario"],
        timeline="full_cycle",
        duration_s=PERIOD_S,
        dt_s=float(slot["controller_dt_s"]),
        period_s=PERIOD_S,
        plant_substeps=int(slot["plant_substeps"]),
        resolution_id=slot["resolution_id"],
        campaign_kind="holdout",
        record_fullstate=True,
        require_ur10e=True,
        observer_parameters=dict(OBSERVER_V3),
        expected_prior=prior.tolist(),
        approach_inward_base=list(basis["approach"]),
        path_stiffness_n_per_m=float(outer.get("path_stiffness_n_per_m", 120.0)),
        compliance_stiffness_n_per_m=float(outer.get("compliance_stiffness_n_per_m", 0.0)),
        load_onset_s=LOAD_ONSET_S,
        path_release_s=scenario_release_s(cell["disturbed_scenario"], timeline="full_cycle"),
        attitude_onset_s=ATTITUDE_ONSET_S,
        jload_scale_n=JLOAD_SCALE_N,
        jpath_scale_m=JPATH_SCALE_M,
        jatt_scale_rad=JATT_SCALE_RAD,
        candidate_parameters=parameters,
        source_hashes=state["campaign_protocol"]["source_hashes"],
        protocol_sha256=state["campaign_protocol"]["yield_protocol_sha256"],
        **bands,
    )


def _run_kwargs(state: Mapping[str, Any], slot: Mapping[str, Any], contract: YieldValidationContract) -> dict[str, Any]:
    qp_library = Path(state["qp_library"])
    build_root = Path(state["native_laws_root"])
    kwargs: dict[str, Any] = {
        "method": contract.actual_method,
        "scenario": slot["scenario"],
        "material": contract.material,
        "duration_s": contract.duration_s,
        "dt_s": contract.dt_s,
        "timeline": "full_cycle",
        "campaign_kind": "holdout",
        "preparation": contract.preparation,
        "record_fullstate": True,
        "require_ur10e": True,
        "law_parameters": dict(contract.candidate_parameters),
        "plant_substeps": contract.plant_substeps,
        "estimator_parameters": {
            **dict(OBSERVER_V3),
            "initial_inward_normal_base": list(contract.expected_prior),
        },
        "surface_parameters": {"kappa_xx": contract.kappa_xx, "kappa_yy": contract.kappa_yy},
    }
    if qp_library.is_file():
        kwargs["qp_library"] = qp_library
    if build_root.is_dir():
        kwargs["build_root"] = build_root
    return kwargs


def create_campaign(
    output: Path | str,
    *,
    freeze_path: Path | str,
    reservation_path: Path | str = DEFAULT_RESERVATION_PATH,
    experiment_root: Path | str | None = None,
    prior_basis: Mapping[str, Sequence[float]] | None = None,
    plant_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    output_root = Path(output).expanduser().resolve()
    state_path = output_root / "campaign.json"
    if state_path.exists() or (output_root / "manifest.json").exists():
        raise FileExistsError(f"output root is immutable: {output_root}")
    experiment_root = Path(experiment_root or EXPERIMENT_ROOT).resolve()
    freeze = load_training_freeze(freeze_path, experiment_root=experiment_root)
    reservation_file = Path(reservation_path).expanduser().resolve()
    reservation = load_reservation(reservation_file)
    reservation_sha256 = _file_sha256(reservation_file)
    observer_sha256 = _observer_sha256(experiment_root)
    declared = reservation["observer"].get("sha256")
    if declared and declared != observer_sha256:
        raise YieldValidationCampaignError("observer sha256 differs from the reservation")
    source_hashes = _source_hashes(experiment_root)
    execution_identities = _execution_identities(experiment_root)
    yield_protocol = protocol()
    plant = dict(plant_identity) if plant_identity is not None else _plant_identity(experiment_root)
    if plant.get("kinematics_kind") != "ur10e_calibrated_pinocchio":
        raise YieldValidationCampaignError("validation requires ur10e_calibrated_pinocchio")
    basis = dict(prior_basis) if prior_basis is not None else calibrated_prior_basis(experiment_root)
    for name in ("approach", "along", "across"):
        if name not in basis:
            raise YieldValidationCampaignError("prior basis incomplete")
    recovered = derive_prior_basis(
        approach=basis["approach"],
        reference_velocity_m_s=np.cross(np.asarray(basis["across"], dtype=float), np.asarray(basis["approach"], dtype=float)),
    )
    if not np.allclose(recovered["along"], np.asarray(basis["along"], dtype=float), atol=1e-12, rtol=0.0):
        raise YieldValidationCampaignError("prior along/across frame is inconsistent")
    prior_inward_normal(
        approach=basis["approach"], along=basis["along"], across=basis["across"],
        direction="along", angle_deg=7.0,
    )
    runtime = _fair_runtime(experiment_root)
    manifest = build_manifest(reservation)
    protocol_payload = {
        "schema": "ur10e.yield-validation-campaign-protocol-v1",
        "reservation_schema": RESERVATION_SCHEMA,
        "reservation_path": str(reservation_file),
        "reservation_sha256": reservation_sha256,
        "training_freeze_path": freeze["path"],
        "training_freeze_file_sha256": freeze["sha256"],
        "training_freeze_sha256": freeze["freeze_sha256"],
        "training_ledger_path": freeze["ledger_path"],
        "training_campaign_export_path": freeze["campaign_export_path"],
        "training_campaign_protocol_sha256": freeze["campaign_protocol_sha256"],
        "training_execution_identities": dict(freeze["execution_identities"]),
        "observer_config_sha256": observer_sha256,
        "yield_protocol_sha256": str(yield_protocol["sha256"]),
        "source_hashes": dict(source_hashes),
        "execution_identities": dict(execution_identities),
        "plant_identity": dict(plant),
        "selected_candidate_keys": {
            method: hashlib.sha256(_canonical(candidate).encode("utf-8")).hexdigest()
            for method, candidate in freeze["selected_candidates"].items()
        },
    }
    campaign_protocol_sha256 = _sha256_bytes(_canonical(protocol_payload).encode("utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": CAMPAIGN_STATE_SCHEMA,
        "version": 1,
        "experiment_root": str(experiment_root),
        "output_root": str(output_root),
        "reservation": {
            "schema": reservation["schema"],
            "cells": reservation["cells"],
            "arms": reservation["arms"],
            "resolution_settings": reservation["resolution_settings"],
            "task": reservation["task"],
            "cost": reservation["cost"],
        },
        "selected_candidates": freeze["selected_candidates"],
        "prior_basis": {name: [float(x) for x in basis[name]] for name in ("approach", "along", "across")},
        "training_bands": _training_bands(experiment_root),
        "outer": {
            "path_stiffness_n_per_m": float((runtime["outer"] or {}).get("path_stiffness_n_per_m", 120.0)),
            "compliance_stiffness_n_per_m": float((runtime["outer"] or {}).get("compliance_stiffness_n_per_m", 0.0)),
        },
        "qp_library": str(experiment_root / runtime["qp_library"]),
        "native_laws_root": str(experiment_root / runtime["native_laws_root"]),
        "campaign_protocol": protocol_payload,
        "campaign_protocol_sha256": campaign_protocol_sha256,
        "formal_validation_complete": False,
        "formal_campaign_complete": False,
        "holdout_used_for_tuning": False,
        "training_writes": False,
        "claim_scope": CLAIM_SCOPE,
        "hardware_qualified": False,
        "maximum_total_trials": 180,
    }
    _atomic_write_json(state_path, state)
    _atomic_write_json(output_root / "manifest.json", {
        "schema": "ur10e.yield-validation-manifest-v1",
        "slots": manifest,
        "order": "cell,arm,resolution,condition",
        "count": len(manifest),
    })
    return {
        "schema": CAMPAIGN_STATE_SCHEMA,
        "output_root": str(output_root),
        "campaign_protocol_sha256": campaign_protocol_sha256,
        "created": True,
        "executed_trials": 0,
        "formal_validation_complete": False,
    }


def _load_state(output: Path | str) -> dict[str, Any]:
    output_root = Path(output).expanduser().resolve()
    state_path = output_root / "campaign.json"
    if not state_path.is_file():
        raise YieldValidationCampaignError("campaign state is missing")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != CAMPAIGN_STATE_SCHEMA:
        raise YieldValidationCampaignError("campaign state schema differs")
    return state


def _load_manifest(output_root: Path) -> list[dict[str, Any]]:
    payload = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    slots = payload.get("slots")
    if not isinstance(slots, list) or len(slots) != 180:
        raise YieldValidationCampaignError("validation manifest is not the reserved 180-slot schedule")
    return list(slots)


def _verify_bindings(state: Mapping[str, Any]) -> None:
    experiment_root = Path(state["experiment_root"])
    protocol_payload = state["campaign_protocol"]
    reservation_path = Path(protocol_payload["reservation_path"])
    if _file_sha256(reservation_path) != protocol_payload["reservation_sha256"]:
        raise YieldValidationCampaignError("reservation drifted")
    freeze_path = Path(protocol_payload["training_freeze_path"])
    if _file_sha256(freeze_path) != protocol_payload["training_freeze_file_sha256"]:
        raise YieldValidationCampaignError("training freeze drifted")
    ledger_path = Path(protocol_payload["training_ledger_path"])
    if not ledger_path.is_file():
        raise YieldValidationCampaignError("training freeze ledger is missing")
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    try:
        frozen = conn.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()
    finally:
        conn.close()
    if frozen is None or hashlib.sha256(frozen[0].encode("utf-8")).hexdigest() != protocol_payload["training_freeze_sha256"]:
        raise YieldValidationCampaignError("training freeze ledger drifted")
    if _observer_sha256(experiment_root) != protocol_payload["observer_config_sha256"]:
        raise YieldValidationCampaignError("observer config drifted")
    if _source_hashes(experiment_root) != protocol_payload["source_hashes"]:
        raise YieldValidationCampaignError("source identity drifted")
    if _execution_identities(experiment_root) != protocol_payload["execution_identities"]:
        raise YieldValidationCampaignError("execution identity drifted")
    if protocol()["sha256"] != protocol_payload["yield_protocol_sha256"]:
        raise YieldValidationCampaignError("yield protocol drifted")


def _artifact_path(output_root: Path, slot_id: str) -> Path:
    return output_root / "attempts" / slot_id / "artifact.json"


def _interrupt_path(output_root: Path, slot_id: str) -> Path:
    return output_root / "attempts" / slot_id / "interruption.json"


def _outcome_path(output_root: Path, slot_id: str) -> Path:
    return output_root / "attempts" / slot_id / "outcome.json"


def _skip_path(output_root: Path, slot_id: str) -> Path:
    return output_root / "skipped" / f"{slot_id}.json"


def _inflight_path(output_root: Path) -> Path:
    return output_root / "inflight.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise YieldValidationCampaignError(f"{path} is not an object")
    return dict(payload)


def _terminal(output_root: Path, slot_id: str) -> dict[str, Any] | None:
    outcome = _read_json(_outcome_path(output_root, slot_id))
    if outcome is not None:
        return outcome
    skipped = _read_json(_skip_path(output_root, slot_id))
    if skipped is not None:
        return skipped
    return None


def _base_pair_allows_refinement(output_root: Path, slot: Mapping[str, Any], slots: Sequence[Mapping[str, Any]]) -> bool | None:
    if slot["resolution_id"] == "base":
        return True
    nominal_id = f"{slot['cell_id']}-{slot['arm']}-base-nominal"
    disturbed_id = f"{slot['cell_id']}-{slot['arm']}-base-disturbed"
    nominal = _terminal(output_root, nominal_id)
    disturbed = _terminal(output_root, disturbed_id)
    if nominal is None or disturbed is None:
        return None
    if nominal.get("status") != "complete" or disturbed.get("status") != "complete":
        return False
    if nominal.get("member_valid") is not True or disturbed.get("member_valid") is not True:
        return False
    if disturbed.get("pair_valid") is not True:
        return False
    return True


def _inflight(output_root: Path) -> dict[str, Any] | None:
    return _read_json(_inflight_path(output_root))


def _next_actions(output_root: Path, slots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    inflight = _inflight(output_root)
    if inflight is not None:
        raise YieldValidationCampaignError(
            f"inflight attempt {inflight['slot_id']} must be reconciled; refusing to rerun"
        )
    actions = []
    for slot in slots:
        if _terminal(output_root, slot["slot_id"]) is not None:
            continue
        if slot["resolution_id"] != "base":
            allowed = _base_pair_allows_refinement(output_root, slot, slots)
            if allowed is False:
                actions.append({"kind": "skip", "slot": slot})
                continue
            if allowed is None:
                raise YieldValidationCampaignError("refinement reached before the base pair is terminal")
        actions.append({"kind": "execute", "slot": slot})
        break
    return actions


def campaign_status(output: Path | str) -> dict[str, Any]:
    state = _load_state(output)
    output_root = Path(state["output_root"])
    _verify_bindings(state)
    slots = _load_manifest(output_root)
    _verify_sealed_files(output_root, slots)
    inflight = _inflight(output_root)
    executed = 0
    skipped = 0
    failed = 0
    interrupted = 0
    complete = 0
    for slot in slots:
        terminal = _terminal(output_root, slot["slot_id"])
        if terminal is None:
            continue
        status = terminal.get("status")
        if status == "skipped_refinement":
            skipped += 1
        elif status == "failed":
            failed += 1
            executed += 1
        elif status == "interrupted":
            interrupted += 1
            executed += 1
        elif status == "complete":
            complete += 1
            executed += 1
    nxt = None
    if inflight is None:
        pending = [slot for slot in slots if _terminal(output_root, slot["slot_id"]) is None]
        nxt = pending[0] if pending else {"complete": True}
    else:
        nxt = {"blocked_by_inflight": inflight["slot_id"]}
    return {
        "schema": CAMPAIGN_STATE_SCHEMA,
        "output_root": str(output_root),
        "inflight": inflight,
        "next": nxt,
        "executed_trials": executed,
        "skipped_refinements": skipped,
        "failed": failed,
        "interrupted": interrupted,
        "complete": complete,
        "remaining_slots": 180 - executed - skipped,
        "formal_validation_complete": False,
        "holdout_used_for_tuning": False,
        "claim_scope": CLAIM_SCOPE,
        "maximum_total_trials": 180,
    }


def _clear_inflight(output_root: Path, slot_id: str) -> None:
    path = _inflight_path(output_root)
    current = _read_json(path)
    if current is None:
        return
    if current.get("slot_id") != slot_id:
        raise YieldValidationCampaignError("inflight slot differs")
    path.unlink()


def _write_outcome(output_root: Path, payload: Mapping[str, Any]) -> str:
    return _atomic_write_json(_outcome_path(output_root, payload["slot_id"]), payload)


def _pair_result_fields(result: YieldValidationPairResult) -> dict[str, Any]:
    return {
        "pair_valid": result.valid,
        "failed_or_incomplete": result.failed_or_incomplete,
        "skip_refinements": result.skip_refinements,
        "nominal_band_ok_flag": result.nominal_band_ok,
        "disturbed_guard_ok_flag": result.disturbed_guard_ok,
        "pair_feasible_flag": result.pair_feasible_flag,
        "objective": result.objective,
        "objective_components": dict(result.objective_components or {}) if result.objective_components else None,
        "objective_eligible": result.objective_eligible,
        "absolute_descriptors": dict(result.absolute_descriptors or {}),
        "reported_recovery": dict(result.reported_recovery or {}),
        "reason": result.reason,
    }


def _seal_nominal(
    *,
    output_root: Path,
    slot: Mapping[str, Any],
    digest: str,
    artifact: Mapping[str, Any],
    contract: YieldValidationContract,
    inspect=inspect_member,
) -> dict[str, Any]:
    try:
        member = inspect(artifact, contract, condition="nominal")
    except YieldValidationSelectionError as error:
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "slot_id": slot["slot_id"],
            "status": "failed",
            "member_valid": False,
            "failed_or_incomplete": True,
            "artifact_sha256": digest,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "reason": str(error),
        }
        _write_outcome(output_root, outcome)
        return outcome
    if not member.valid:
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "slot_id": slot["slot_id"],
            "status": "failed",
            "member_valid": False,
            "failed_or_incomplete": True,
            "artifact_sha256": digest,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "nominal_band_ok_flag": member.nominal_band_ok,
            "reason": member.reason,
            "reported_metrics": dict(member.reported_metrics),
        }
        _write_outcome(output_root, outcome)
        return outcome
    outcome = {
        "schema": OUTCOME_SCHEMA,
        "slot_id": slot["slot_id"],
        "status": "complete",
        "member_valid": True,
        "failed_or_incomplete": False,
        "artifact_sha256": digest,
        "arm": slot["arm"],
        "actual_method": slot["actual_method"],
        "nominal_band_ok_flag": member.nominal_band_ok,
        "absolute_descriptors": dict(member.absolute_descriptors),
        "reported_metrics": dict(member.reported_metrics),
        "recomputed_metrics": dict(member.recomputed_metrics),
    }
    _write_outcome(output_root, outcome)
    return outcome


def _load_artifact(output_root: Path, slot_id: str, *, expected_method: str) -> tuple[dict[str, Any], str]:
    path = _artifact_path(output_root, slot_id)
    if not path.is_file():
        raise YieldValidationCampaignError("sealed artifact missing")
    encoded = path.read_bytes()
    digest = _sha256_bytes(encoded)
    outcome = _terminal(output_root, slot_id)
    if outcome is None or outcome.get("artifact_sha256") != digest:
        raise YieldValidationCampaignError("sealed artifact digest differs; not fabricating success")
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise YieldValidationCampaignError("sealed artifact is not an object")
    if payload.get("method") != expected_method:
        raise YieldValidationCampaignError("existing artifact identity differs; not fabricating success")
    return dict(payload), digest


def _seal_disturbed(
    *,
    output_root: Path,
    slot: Mapping[str, Any],
    digest: str,
    artifact: Mapping[str, Any],
    contract: YieldValidationContract,
    inspect=inspect_member,
    pair_eval=evaluate_pair,
) -> dict[str, Any]:
    nominal_id = f"{slot['cell_id']}-{slot['arm']}-{slot['resolution_id']}-nominal"
    nominal_terminal = _terminal(output_root, nominal_id)
    if nominal_terminal is None:
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "slot_id": slot["slot_id"],
            "status": "failed",
            "member_valid": False,
            "pair_valid": False,
            "failed_or_incomplete": True,
            "artifact_sha256": digest,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "reason": "nominal artifact missing",
        }
        _write_outcome(output_root, outcome)
        return outcome
    if nominal_terminal.get("status") != "complete" or nominal_terminal.get("member_valid") is not True:
        # Pair is named negative; retain disturbed evidence without ranking.
        try:
            member = inspect(artifact, contract, condition="disturbed")
            member_valid = member.valid
            reported = dict(member.reported_metrics)
            reason = "failed/incomplete nominal; disturbed retained, no performance ranking"
        except YieldValidationSelectionError as error:
            member_valid = False
            reported = dict(artifact.get("metrics") or {}) if isinstance(artifact.get("metrics"), Mapping) else {}
            reason = str(error)
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "slot_id": slot["slot_id"],
            "status": "failed" if not member_valid else "complete",
            "member_valid": bool(member_valid),
            "pair_valid": False,
            "failed_or_incomplete": True,
            "artifact_sha256": digest,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "reason": reason,
            "reported_metrics": reported,
        }
        _write_outcome(output_root, outcome)
        return outcome
    try:
        nominal_artifact, _ = _load_artifact(output_root, nominal_id, expected_method=contract.actual_method)
        result = pair_eval(nominal_artifact, artifact, contract)
    except YieldValidationSelectionError as error:
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "slot_id": slot["slot_id"],
            "status": "failed",
            "member_valid": False,
            "pair_valid": False,
            "failed_or_incomplete": True,
            "artifact_sha256": digest,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "reason": str(error),
        }
        _write_outcome(output_root, outcome)
        return outcome
    if not result.valid:
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "slot_id": slot["slot_id"],
            "status": "failed",
            "member_valid": False,
            "pair_valid": False,
            "failed_or_incomplete": True,
            "artifact_sha256": digest,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            **_pair_result_fields(result),
        }
        _write_outcome(output_root, outcome)
        return outcome
    outcome = {
        "schema": OUTCOME_SCHEMA,
        "slot_id": slot["slot_id"],
        "status": "complete",
        "member_valid": True,
        "failed_or_incomplete": False,
        "artifact_sha256": digest,
        "arm": slot["arm"],
        "actual_method": slot["actual_method"],
        **_pair_result_fields(result),
    }
    _write_outcome(output_root, outcome)
    return outcome


def _verify_sealed_files(output_root: Path, slots: Sequence[Mapping[str, Any]]) -> None:
    for slot in slots:
        terminal = _read_json(_outcome_path(output_root, slot["slot_id"]))
        if terminal is None:
            skipped = _read_json(_skip_path(output_root, slot["slot_id"]))
            if skipped is not None:
                raise YieldValidationCampaignError(f"skipped slot {slot['slot_id']} is missing a digest-bound outcome")
            continue
        status = terminal.get("status")
        if status == "skipped_refinement":
            path = _skip_path(output_root, slot["slot_id"])
            digest = terminal.get("record_sha256")
        elif status == "interrupted":
            path = _interrupt_path(output_root, slot["slot_id"])
            digest = terminal.get("artifact_sha256")
        else:
            path = _artifact_path(output_root, slot["slot_id"])
            digest = terminal.get("artifact_sha256")
        if not path.is_file():
            raise YieldValidationCampaignError(f"sealed artifact {slot['slot_id']} is missing")
        if not _is_sha256(digest) or _file_sha256(path) != digest:
            raise YieldValidationCampaignError(
                f"sealed artifact {slot['slot_id']} digest differs; not fabricating success"
            )


def _record_skip(output_root: Path, slot: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    payload = {
        "schema": SKIP_SCHEMA,
        "slot_id": slot["slot_id"],
        "status": "skipped_refinement",
        "arm": slot["arm"],
        "actual_method": slot["actual_method"],
        "cell_id": slot["cell_id"],
        "resolution_id": slot["resolution_id"],
        "condition": slot["condition"],
        "executed": False,
        "reason": reason,
        "claim_scope": CLAIM_SCOPE,
    }
    digest = _atomic_write_json(_skip_path(output_root, slot["slot_id"]), payload)
    outcome = {
        "schema": OUTCOME_SCHEMA,
        "slot_id": slot["slot_id"],
        "status": "skipped_refinement",
        "member_valid": False,
        "pair_valid": False,
        "failed_or_incomplete": True,
        "executed": False,
        "record_sha256": digest,
        "arm": slot["arm"],
        "actual_method": slot["actual_method"],
        "reason": reason,
    }
    _write_outcome(output_root, outcome)
    return outcome


def run_next(
    output: Path | str,
    *,
    runner: Callable[..., Mapping[str, Any]] | None = None,
    inspect: Callable[..., Any] | None = None,
    pair_eval: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state = _load_state(output)
    with _exclusive_campaign(Path(state["output_root"])):
        _verify_bindings(state)
        output_root = Path(state["output_root"])
        slots = _load_manifest(output_root)
        _verify_sealed_files(output_root, slots)
        execute = runner or _default_runner
        inspect_fn = inspect or inspect_member
        pair_fn = pair_eval or evaluate_pair
        skipped = []
        while True:
            actions = _next_actions(output_root, slots)
            if not actions:
                return {
                    "status": "schedule_complete",
                    "skipped": skipped,
                    "executed_trials": campaign_status(output_root)["executed_trials"],
                    "formal_validation_complete": False,
                }
            action = actions[0]
            slot = action["slot"]
            if action["kind"] == "skip":
                skipped.append(_record_skip(
                    output_root, slot,
                    reason="base pair failed or incomplete; refinement not executed",
                ))
                continue
            contract = _slot_contract(state, slot)
            bound = bind_arm(
                contract,
                arm=slot["arm"],
                parameters=contract.candidate_parameters,
                source_hashes=state["campaign_protocol"]["source_hashes"],
                protocol_sha256=state["campaign_protocol"]["yield_protocol_sha256"],
            )
            inflight_payload = {
                "slot_id": slot["slot_id"],
                "arm": slot["arm"],
                "actual_method": slot["actual_method"],
                "condition": slot["condition"],
                "index": slot["index"],
                "registered": True,
            }
            _atomic_write_json(_inflight_path(output_root), inflight_payload)
            kwargs = _run_kwargs(state, slot, bound)
            artifact = execute(**kwargs)
            if not isinstance(artifact, Mapping):
                raise YieldValidationCampaignError("runner must return an artifact object")
            if artifact.get("method") != bound.actual_method:
                raise YieldValidationCampaignError("runner artifact method differs; not relabeling")
            if artifact.get("campaign_kind") == "training":
                raise YieldValidationCampaignError("training rows cannot enter validation")
            if _source_hashes(Path(state["experiment_root"])) != state["campaign_protocol"]["source_hashes"]:
                raise YieldValidationCampaignError("source identity drifted during trial")
            if _execution_identities(Path(state["experiment_root"])) != state["campaign_protocol"]["execution_identities"]:
                raise YieldValidationCampaignError("execution identity drifted during trial")
            digest = _atomic_write_json(_artifact_path(output_root, slot["slot_id"]), artifact)
            if slot["condition"] == "nominal":
                sealed = _seal_nominal(
                    output_root=output_root, slot=slot, digest=digest, artifact=artifact,
                    contract=bound, inspect=inspect_fn,
                )
            else:
                sealed = _seal_disturbed(
                    output_root=output_root, slot=slot, digest=digest, artifact=artifact,
                    contract=bound, inspect=inspect_fn, pair_eval=pair_fn,
                )
            _clear_inflight(output_root, slot["slot_id"])
            return {
                "slot_id": slot["slot_id"],
                "arm": slot["arm"],
                "actual_method": slot["actual_method"],
                "condition": slot["condition"],
                "artifact_path": str(_artifact_path(output_root, slot["slot_id"])),
                "artifact_sha256": digest,
                "skipped_before": skipped,
                "formal_validation_complete": False,
                **sealed,
            }


def reconcile(
    output: Path | str,
    *,
    interrupt: bool = False,
    inspect: Callable[..., Any] | None = None,
    pair_eval: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state = _load_state(output)
    with _exclusive_campaign(Path(state["output_root"])):
        _verify_bindings(state)
        output_root = Path(state["output_root"])
        _verify_sealed_files(output_root, _load_manifest(output_root))
        inflight = _inflight(output_root)
        if inflight is None:
            return {"reconciled": False, "reason": "nothing inflight", "formal_validation_complete": False}
        slot_id = inflight["slot_id"]
        slots = {item["slot_id"]: item for item in _load_manifest(output_root)}
        slot = slots[slot_id]
        contract = bind_arm(
            _slot_contract(state, slot),
            arm=slot["arm"],
            parameters=_slot_contract(state, slot).candidate_parameters,
        )
        inspect_fn = inspect or inspect_member
        pair_fn = pair_eval or evaluate_pair
        artifact_path = _artifact_path(output_root, slot_id)
        if artifact_path.is_file():
            encoded = artifact_path.read_bytes()
            digest = _sha256_bytes(encoded)
            artifact = json.loads(encoded.decode("utf-8"))
            if not isinstance(artifact, Mapping):
                raise YieldValidationCampaignError("existing artifact is not an object")
            if artifact.get("method") != slot["actual_method"]:
                raise YieldValidationCampaignError("existing artifact identity differs; not fabricating success")
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
            _clear_inflight(output_root, slot_id)
            return {
                "reconciled": True,
                "slot_id": slot_id,
                "artifact_sha256": digest,
                "interrupt": False,
                "formal_validation_complete": False,
                **sealed,
            }
        if not interrupt:
            raise YieldValidationCampaignError(
                "inflight has no complete artifact; explicitly interrupt to seal an interruption"
            )
        interruption = {
            "schema": INTERRUPT_SCHEMA,
            "slot_id": slot_id,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "condition": slot["condition"],
            "status": "interrupted",
            "reason": "explicit reconcile interrupt; consumed slot preserved",
            "claim_scope": CLAIM_SCOPE,
            "formal_validation_complete": False,
        }
        digest = _atomic_write_json(_interrupt_path(output_root, slot_id), interruption)
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "slot_id": slot_id,
            "status": "interrupted",
            "member_valid": False,
            "pair_valid": False,
            "failed_or_incomplete": True,
            "artifact_sha256": digest,
            "arm": slot["arm"],
            "actual_method": slot["actual_method"],
            "reason": interruption["reason"],
        }
        _write_outcome(output_root, outcome)
        _clear_inflight(output_root, slot_id)
        return {
            "reconciled": True,
            "slot_id": slot_id,
            "interrupt": True,
            "status": "interrupted",
            "artifact_sha256": digest,
            "formal_validation_complete": False,
        }


def campaign_report(output: Path | str) -> dict[str, Any]:
    state = _load_state(output)
    output_root = Path(state["output_root"])
    _verify_bindings(state)
    slots = _load_manifest(output_root)
    by_cell: dict[str, Any] = {}
    for slot in slots:
        terminal = _terminal(output_root, slot["slot_id"])
        cell = by_cell.setdefault(slot["cell_id"], {})
        arm = cell.setdefault(slot["arm"], {"actual_method": slot["actual_method"], "settings": {}})
        setting = arm["settings"].setdefault(slot["resolution_id"], {"members": {}})
        setting["members"][slot["condition"]] = terminal
        if slot["condition"] == "disturbed" and terminal and terminal.get("status") == "complete" and terminal.get("pair_valid") is True:
            setting["valid"] = True
            setting["absolute_descriptors"] = dict(terminal.get("absolute_descriptors") or {})
            setting["objective"] = terminal.get("objective")
            setting["nominal_band_ok_flag"] = terminal.get("nominal_band_ok_flag")
            setting["disturbed_guard_ok_flag"] = terminal.get("disturbed_guard_ok_flag")
        elif slot["condition"] == "disturbed" and terminal is not None:
            setting["valid"] = False
            setting["failed_or_incomplete"] = True
            setting["status"] = terminal.get("status")
            setting["reason"] = terminal.get("reason")
    comparisons: dict[str, Any] = {}
    for cell_id, arms in by_cell.items():
        cell_cmp = {}
        names = [arm for arm in ARMS if arm in arms]
        for i, arm_a in enumerate(names):
            for arm_b in names[i + 1:]:
                settings_a = arms[arm_a]["settings"]
                settings_b = arms[arm_b]["settings"]
                if all(
                    settings_a.get(name, {}).get("valid") is True and settings_b.get(name, {}).get("valid") is True
                    for name, _, _ in RESOLUTION_SETTINGS
                ):
                    cell_cmp[f"{arm_a}_vs_{arm_b}"] = compare_resolution_settings(
                        arm_a=arm_a, arm_b=arm_b, settings_a=settings_a, settings_b=settings_b,
                    )
        comparisons[cell_id] = cell_cmp
    return {
        "schema": "ur10e.yield-validation-report-v1",
        "output_root": str(output_root),
        "cells": by_cell,
        "comparisons": comparisons,
        "no_j_pooling": True,
        "no_ci": True,
        "winner": None,
        "formal_validation_complete": False,
        "claim_scope": CLAIM_SCOPE,
        "fullstate_limit": "full-state schema validation only; exact forward replay is not performed",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    create = sub.add_parser("create", help="bind freeze+reservation and create an immutable campaign root")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--freeze", type=Path, required=True)
    create.add_argument("--reservation", type=Path, default=DEFAULT_RESERVATION_PATH)
    status = sub.add_parser("status", help="show finite-schedule progress without launching trials")
    status.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run-next", help="register and run exactly the next executable member")
    run.add_argument("--output", type=Path, required=True)
    rec = sub.add_parser("reconcile", help="seal an existing artifact or an explicit interruption")
    rec.add_argument("--output", type=Path, required=True)
    rec.add_argument("--interrupt", action="store_true")
    rep = sub.add_parser("report", help="emit per-cell/arm/resolution descriptors without pooling J")
    rep.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "create":
        result = create_campaign(args.output, freeze_path=args.freeze, reservation_path=args.reservation)
    elif args.command == "status":
        result = campaign_status(args.output)
    elif args.command == "run-next":
        result = run_next(args.output)
    elif args.command == "reconcile":
        result = reconcile(args.output, interrupt=args.interrupt)
    elif args.command == "report":
        result = campaign_report(args.output)
    else:
        parser.error("unknown command")
        return 2
    print(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
