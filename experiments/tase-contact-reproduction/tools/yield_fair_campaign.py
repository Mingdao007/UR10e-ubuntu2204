#!/usr/bin/env python3
"""Offline paired yield-fair campaign plumbing. Does not launch a campaign on import.

Explicit create/status/run-next/reconcile/freeze only. One native attempt at a
time. Main owns scientific acceptance and any full-cycle execution.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from contact_yield_protocol import protocol
from yield_contact_ledger import YieldContactLedger
from yield_contact_tuner import INITIAL_UNITS, METHODS, TOTAL_UNITS, YieldContactTuner
from yield_fair_selection import (
    OBSERVER_V3,
    YieldFairContract,
    YieldFairSelectionError,
    bind_candidate,
    contract_from_config,
    evaluate_pair,
    inspect_member,
)


CAMPAIGN_STATE_SCHEMA = "ur10e.yield-fair-campaign-state-v1"
FREEZE_SCHEMA = "ur10e.yield-fair-campaign-freeze-v1"
INTERRUPT_SCHEMA = "ur10e.yield-fair-interruption-v1"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = EXPERIMENT_ROOT / "config" / "yield_fair_campaign_v1.json"
SCIENTIFIC_FILES = (
    "tools/yield_fair_selection.py",
    "tools/yield_fair_campaign.py",
    "tools/yield_contact_ledger.py",
    "tools/yield_contact_tuner.py",
    "tools/contact_benchmark_tuner.py",
    "tools/contact_benchmark_ledger.py",
)
NATIVE_FILES = (
    "tools/contact_laws.py",
    "tools/contact_qp.py",
    "tools/build_contact_laws.py",
)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
CLAIM_SCOPE = (
    "prospective DEVELOPMENT-model comparison; no real precise-contact, "
    "safety, or physical qualification claim"
)


class YieldFairCampaignError(ValueError):
    """Invalid campaign command, binding, or restart state."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


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
        raise YieldFairCampaignError("artifact digest drifted during write")
    return digest


def _source_hashes(root: Path) -> dict[str, str]:
    paths = {item.name: item for item in (root / "tools").glob("contact_yield_*.py")}
    if not paths:
        raise YieldFairCampaignError("contact_yield source files missing")
    return {name: _file_sha256(path) for name, path in sorted(paths.items())}


def _hash_relative_files(root: Path, relatives: tuple[str, ...]) -> dict[str, str]:
    hashed = {}
    for relative in relatives:
        path = root / relative
        if not path.is_file():
            raise YieldFairCampaignError(f"execution identity file missing: {relative}")
        hashed[relative] = _file_sha256(path)
    return hashed


def _execution_identities(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    qp_library = root / str(config["runtime"]["qp_library"])
    laws_root = root / str(config["runtime"]["native_laws_root"])
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


def _load_config(config_path: Path) -> tuple[dict[str, Any], bytes]:
    if config_path.is_symlink():
        raise YieldFairCampaignError(f"campaign config must not be a symlink: {config_path}")
    raw = config_path.read_bytes()
    root = json.loads(raw.decode("utf-8"))
    if not isinstance(root, Mapping):
        raise YieldFairCampaignError("campaign config must be an object")
    return dict(root), raw


def _observer_sha256(root: Path, config: Mapping[str, Any]) -> str:
    observer_path = root / str(config["observer_config"])
    payload = json.loads(observer_path.read_text(encoding="utf-8"))
    if payload != OBSERVER_V3:
        raise YieldFairCampaignError("observer config drifted from yield_normal_observer_v3")
    return _file_sha256(observer_path)


def _campaign_protocol(
    *,
    config_sha256: str,
    tuner: YieldContactTuner,
    observer_sha256: str,
    yield_protocol_sha256: str,
    source_hashes: Mapping[str, str],
    execution_identities: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "ur10e.yield-fair-campaign-protocol-v1",
        "campaign_config_sha256": config_sha256,
        "tuner_config_sha256": tuner.config_sha256,
        "observer_config_sha256": observer_sha256,
        "yield_protocol_sha256": yield_protocol_sha256,
        "training_cell_id": tuner.training_cell_id,
        "selection_contract_id": tuner.selection_contract_id,
        "source_hashes": dict(source_hashes),
        "execution_identities": dict(execution_identities),
    }


def _open_tuner(experiment_root: Path, config: Mapping[str, Any]) -> YieldContactTuner:
    return YieldContactTuner(
        experiment_root / str(config["tuner_config"]),
        training_cell_id=str(config["training_cell_id"]),
        selection_contract_id=str(config["selection_contract_id"]),
    )


def create_campaign(
    output: Path | str,
    *,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    experiment_root: Path | str | None = None,
) -> dict[str, Any]:
    output_root = Path(output).expanduser().resolve()
    state_path = output_root / "campaign.json"
    if state_path.exists() or (output_root / "campaign.sqlite").exists():
        raise FileExistsError(f"output root is immutable: {output_root}")
    config_path = Path(config_path).expanduser().resolve()
    experiment_root = Path(experiment_root or EXPERIMENT_ROOT).resolve()
    config, raw = _load_config(config_path)
    contract = contract_from_config(config)
    if config.get("training_cell_id") != contract.material:
        raise YieldFairCampaignError("training_cell_id must be the stiff_low_mu cell")
    observer_sha256 = _observer_sha256(experiment_root, config)
    tuner = _open_tuner(experiment_root, config)
    yield_protocol = protocol()
    source_hashes = _source_hashes(experiment_root)
    execution_identities = _execution_identities(experiment_root, config)
    config_sha256 = _sha256_bytes(raw)
    protocol_payload = _campaign_protocol(
        config_sha256=config_sha256,
        tuner=tuner,
        observer_sha256=observer_sha256,
        yield_protocol_sha256=str(yield_protocol["sha256"]),
        source_hashes=source_hashes,
        execution_identities=execution_identities,
    )
    campaign_protocol_sha256 = _sha256_bytes(_canonical(protocol_payload).encode("utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    ledger_path = output_root / "campaign.sqlite"
    ledger = YieldContactLedger(
        ledger_path,
        tuner=tuner,
        campaign_protocol_sha256=campaign_protocol_sha256,
    )
    try:
        state = {
            "schema": CAMPAIGN_STATE_SCHEMA,
            "version": 1,
            "config_path": str(config_path),
            "config_sha256": config_sha256,
            "experiment_root": str(experiment_root),
            "output_root": str(output_root),
            "ledger_path": str(ledger_path),
            "campaign_protocol": protocol_payload,
            "campaign_protocol_sha256": campaign_protocol_sha256,
            "ledger_bindings": dict(ledger.bindings),
            "qp_library": str(experiment_root / str(config["runtime"]["qp_library"])),
            "native_laws_root": str(experiment_root / str(config["runtime"]["native_laws_root"])),
            "formal_campaign_complete": False,
            "holdout_implemented": False,
            "claim_scope": CLAIM_SCOPE,
            "hardware_qualified": False,
        }
        _atomic_write_json(state_path, state)
    finally:
        ledger.close()
    return {
        "schema": CAMPAIGN_STATE_SCHEMA,
        "output_root": str(output_root),
        "campaign_protocol_sha256": campaign_protocol_sha256,
        "formal_campaign_complete": False,
        "created": True,
    }


def _load_state(output: Path | str) -> dict[str, Any]:
    output_root = Path(output).expanduser().resolve()
    state_path = output_root / "campaign.json"
    if not state_path.is_file():
        raise YieldFairCampaignError("campaign state is missing")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != CAMPAIGN_STATE_SCHEMA:
        raise YieldFairCampaignError("campaign state schema differs")
    return state


def _open_bound(state: Mapping[str, Any]) -> tuple[dict[str, Any], YieldFairContract, YieldContactTuner, YieldContactLedger]:
    experiment_root = Path(state["experiment_root"])
    config_path = Path(state["config_path"])
    config, raw = _load_config(config_path)
    if _sha256_bytes(raw) != state["config_sha256"]:
        raise YieldFairCampaignError("campaign config drifted")
    contract = contract_from_config(config)
    observer_sha256 = _observer_sha256(experiment_root, config)
    if observer_sha256 != state["campaign_protocol"]["observer_config_sha256"]:
        raise YieldFairCampaignError("observer config drifted")
    source_hashes = _source_hashes(experiment_root)
    if source_hashes != state["campaign_protocol"]["source_hashes"]:
        raise YieldFairCampaignError("source identity drifted")
    execution_identities = _execution_identities(experiment_root, config)
    if execution_identities != state["campaign_protocol"].get("execution_identities"):
        raise YieldFairCampaignError("execution identity drifted")
    yield_protocol_sha256 = protocol()["sha256"]
    if yield_protocol_sha256 != state["campaign_protocol"]["yield_protocol_sha256"]:
        raise YieldFairCampaignError("yield protocol drifted")
    tuner = _open_tuner(experiment_root, config)
    if tuner.config_sha256 != state["campaign_protocol"]["tuner_config_sha256"]:
        raise YieldFairCampaignError("tuner config drifted")
    if tuner.training_cell_id != state["campaign_protocol"]["training_cell_id"]:
        raise YieldFairCampaignError("training cell drifted")
    if tuner.selection_contract_id != state["campaign_protocol"]["selection_contract_id"]:
        raise YieldFairCampaignError("selection contract drifted")
    ledger = YieldContactLedger(
        Path(state["ledger_path"]),
        tuner=tuner,
        campaign_protocol_sha256=state["campaign_protocol_sha256"],
    )
    return config, contract, tuner, ledger


def _attempt_id(method: str, unit_index: int, condition: str) -> str:
    return f"{method}-{unit_index:02d}-{condition}"


def _artifact_path(output_root: Path, attempt_id: str) -> Path:
    return output_root / "attempts" / attempt_id / "artifact.json"


def _interrupt_path(output_root: Path, attempt_id: str) -> Path:
    return output_root / "attempts" / attempt_id / "interruption.json"


def _load_sealed_artifact(ledger: YieldContactLedger, output_root: Path, attempt_id: str) -> tuple[dict[str, Any], str]:
    row = ledger.db.execute(
        "SELECT status,evidence FROM attempts WHERE id=?", (attempt_id,)
    ).fetchone()
    if row is None or row[1] is None:
        raise YieldFairCampaignError("sealed artifact registration missing")
    if row[0] == "running":
        raise YieldFairCampaignError("sealed artifact is not terminal")
    evidence = json.loads(row[1])
    digest = evidence.get("artifact_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise YieldFairCampaignError("sealed artifact digest missing")
    path = (_interrupt_path(output_root, attempt_id) if row[0] == "interrupted"
            else _artifact_path(output_root, attempt_id))
    if not path.is_file():
        raise YieldFairCampaignError("sealed artifact missing")
    encoded = path.read_bytes()
    actual = _sha256_bytes(encoded)
    if actual != digest:
        raise YieldFairCampaignError("sealed artifact digest differs; not fabricating success")
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise YieldFairCampaignError("sealed artifact is not an object")
    if row[0] == "interrupted":
        return {**dict(payload), "metrics": {"failed": True}}, actual
    return dict(payload), actual


def _verify_sealed_artifact_files(ledger: YieldContactLedger, output_root: Path) -> None:
    rows = ledger.db.execute("SELECT id,status,evidence FROM attempts").fetchall()
    for attempt_id, status, evidence in rows:
        if evidence is None or status == "running":
            continue
        payload = json.loads(evidence)
        digest = payload.get("artifact_sha256")
        path = (_interrupt_path(output_root, attempt_id) if status == "interrupted"
                else _artifact_path(output_root, attempt_id))
        if not path.is_file():
            raise YieldFairCampaignError(f"sealed artifact {attempt_id} is missing")
        if _sha256_bytes(path.read_bytes()) != digest:
            raise YieldFairCampaignError(
                f"sealed artifact {attempt_id} was mutated; not fabricating success"
            )


def _next_slot(ledger: YieldContactLedger, method: str) -> dict[str, Any] | None:
    inflight = ledger.inflight()
    if inflight is not None:
        raise YieldFairCampaignError(
            f"inflight attempt {inflight['attempt_id']} must be reconciled; refusing to rerun"
        )
    used = int(ledger.progress()[method]["used_units"])
    if used == 0:
        return {"condition": "nominal", "unit_index": 0, "new_unit": True}
    last = used - 1
    pair = {condition: status for condition, status in ledger.db.execute(
        "SELECT condition,status FROM attempts WHERE controller=? AND unit=?",
        (method, last))}
    if "disturbed" not in pair:
        if "nominal" not in pair:
            raise YieldFairCampaignError("previous paired unit is incomplete")
        return {"condition": "disturbed", "unit_index": last, "new_unit": False}
    if set(pair) != {"nominal", "disturbed"}:
        raise YieldFairCampaignError("previous paired unit is incomplete")
    if used >= TOTAL_UNITS:
        return None
    return {"condition": "nominal", "unit_index": used, "new_unit": True}


def _initial_feasible_count(ledger: YieldContactLedger, method: str) -> int:
    history = ledger.training_observations(method)
    return sum(1 for row in history[:INITIAL_UNITS] if row.status == "completed" and row.nominal_feasible is True)


def _unit_candidate(ledger: YieldContactLedger, tuner: YieldContactTuner, method: str, unit_index: int):
    row = ledger.db.execute(
        "SELECT candidate FROM units WHERE controller=? AND number=?",
        (method, unit_index),
    ).fetchone()
    if row is None:
        raise YieldFairCampaignError("paired unit candidate is missing")
    return tuner._parse_candidate(method, json.loads(row[0]))


def _run_kwargs(
    *,
    state: Mapping[str, Any],
    contract: YieldFairContract,
    method: str,
    condition: str,
    parameters: Mapping[str, float],
) -> dict[str, Any]:
    qp_library = Path(state["qp_library"])
    build_root = Path(state["native_laws_root"])
    kwargs: dict[str, Any] = {
        "method": method,
        "scenario": contract.nominal_scenario if condition == "nominal" else contract.disturbed_scenario,
        "material": contract.material,
        "duration_s": contract.duration_s,
        "dt_s": contract.dt_s,
        "timeline": contract.timeline,
        "campaign_kind": "training",
        "preparation": contract.preparation,
        "record_fullstate": True,
        "require_ur10e": True,
        "law_parameters": dict(parameters),
        "plant_substeps": contract.plant_substeps,
        "estimator_parameters": dict(OBSERVER_V3),
        "surface_parameters": {"kappa_xx": contract.kappa_xx, "kappa_yy": contract.kappa_yy},
    }
    if qp_library.is_file():
        kwargs["qp_library"] = qp_library
    if build_root.is_dir():
        kwargs["build_root"] = build_root
    return kwargs


def _bound_contract(state: Mapping[str, Any], contract: YieldFairContract, *, method: str,
                    parameters: Mapping[str, float]) -> YieldFairContract:
    return bind_candidate(
        contract,
        method=method,
        parameters=parameters,
        source_hashes=state["campaign_protocol"]["source_hashes"],
        protocol_sha256=state["campaign_protocol"]["yield_protocol_sha256"],
    )


def _evidence(ledger: YieldContactLedger, digest: str, **fields: Any) -> dict[str, Any]:
    payload = dict(ledger.bindings)
    payload["artifact_sha256"] = digest
    payload.update(fields)
    return payload


def _seal_nominal(ledger: YieldContactLedger, *, attempt_id: str, digest: str, artifact: Mapping[str, Any],
                  bound: YieldFairContract, inspect=inspect_member) -> dict[str, Any]:
    try:
        member = inspect(artifact, bound, condition="nominal")
    except YieldFairSelectionError as error:
        ledger.seal(attempt_id, status="failed", evidence=_evidence(
            ledger, digest, objective_eligible=False, nominal_feasible=False,
            reason=str(error)))
        return {"status": "failed", "nominal_feasible": False, "reason": str(error)}
    if not member.valid:
        ledger.seal(attempt_id, status="failed", evidence=_evidence(
            ledger, digest, objective_eligible=False, nominal_feasible=False,
            reason=member.reason))
        return {"status": "failed", "nominal_feasible": False, "reason": member.reason}
    ledger.seal(attempt_id, status="complete", evidence=_evidence(
        ledger, digest, objective_eligible=True, nominal_feasible=member.nominal_feasible,
        objective_eligible_means="complete_uncensored_result",
        pair_feasible_controls_selection=True,
        nominal_checks=dict(member.checks)))
    return {"status": "complete", "nominal_feasible": member.nominal_feasible}


def _seal_disturbed(ledger: YieldContactLedger, *, attempt_id: str, digest: str,
                    nominal_artifact: Mapping[str, Any] | None, artifact: Mapping[str, Any],
                    bound: YieldFairContract, pair_eval=evaluate_pair) -> dict[str, Any]:
    if nominal_artifact is None:
        ledger.seal(attempt_id, status="failed", evidence=_evidence(
            ledger, digest, objective_eligible=False, pair_feasible=False,
            disturbed_guards_ok=False, reason="nominal artifact missing"))
        return {"status": "failed", "pair_feasible": False, "reason": "nominal artifact missing"}
    try:
        result = pair_eval(nominal_artifact, artifact, bound)
    except YieldFairSelectionError as error:
        ledger.seal(attempt_id, status="failed", evidence=_evidence(
            ledger, digest, objective_eligible=False, pair_feasible=False,
            disturbed_guards_ok=False, reason=str(error)))
        return {"status": "failed", "pair_feasible": False, "reason": str(error)}
    if not result.valid:
        ledger.seal(attempt_id, status="failed", evidence=_evidence(
            ledger, digest, objective_eligible=False, pair_feasible=False,
            disturbed_guards_ok=bool(result.disturbed_guards_ok),
            reason=result.reason))
        return {"status": "failed", "pair_feasible": False, "reason": result.reason}
    ledger.seal(attempt_id, status="complete", evidence=_evidence(
        ledger, digest, objective_eligible=True,
        objective=result.objective,
        objective_components=dict(result.objective_components or {}),
        pair_feasible=result.pair_feasible,
        disturbed_guards_ok=result.disturbed_guards_ok,
        objective_eligible_means="complete_uncensored_result",
        pair_feasible_controls_selection=True,
        nominal_feasible_reported=result.nominal_feasible,
        nominal_checks=dict(result.nominal_checks),
        disturbed_checks=dict(result.disturbed_checks)))
    return {
        "status": "complete",
        "pair_feasible": result.pair_feasible,
        "nominal_feasible": result.nominal_feasible,
        "disturbed_guards_ok": result.disturbed_guards_ok,
        "objective": result.objective,
        "objective_components": dict(result.objective_components or {}),
        "objective_eligible": True,
    }


def campaign_status(output: Path | str) -> dict[str, Any]:
    state = _load_state(output)
    config, contract, tuner, ledger = _open_bound(state)
    try:
        inflight = ledger.inflight()
        methods = {}
        for method in METHODS:
            progress = ledger.progress()[method]
            stopped = False
            nxt: dict[str, Any] | None = None
            if inflight is not None:
                nxt = {"blocked_by_inflight": inflight["attempt_id"]}
            else:
                try:
                    slot = _next_slot(ledger, method)
                    if slot is None:
                        nxt = {"complete": True}
                    else:
                        if slot["new_unit"] and slot["unit_index"] >= INITIAL_UNITS:
                            try:
                                feasible = _initial_feasible_count(ledger, method)
                            except ValueError:
                                feasible = 0
                            if feasible < 3:
                                stopped = True
                        nxt = {**slot, "stopped_before_ei": stopped}
                except YieldFairCampaignError as error:
                    nxt = {"error": str(error)}
            methods[method] = {**progress, "next": nxt, "stopped_before_ei": stopped}
        frozen = ledger.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()
        return {
            "schema": CAMPAIGN_STATE_SCHEMA,
            "output_root": state["output_root"],
            "inflight": inflight,
            "methods": methods,
            "frozen": frozen[0] if frozen else None,
            "formal_campaign_complete": False,
            "holdout_implemented": False,
            "claim_scope": CLAIM_SCOPE,
            "training_cell_id": contract.material,
            "selection_contract_id": tuner.selection_contract_id,
        }
    finally:
        ledger.close()


def run_next(
    output: Path | str,
    *,
    method: str,
    runner: Callable[..., Mapping[str, Any]] | None = None,
    inspect: Callable[..., Any] | None = None,
    pair_eval: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if method not in METHODS:
        raise YieldFairCampaignError("method must be SFC, DSFC or MSFC")
    state = _load_state(output)
    with _exclusive_campaign(Path(state["output_root"])):
        config, contract, tuner, ledger = _open_bound(state)
        execute = runner or _default_runner
        inspect_fn = inspect or inspect_member
        pair_fn = pair_eval or evaluate_pair
        try:
            if ledger.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone():
                raise YieldFairCampaignError("tuning ledger is frozen")
            slot = _next_slot(ledger, method)
            if slot is None:
                raise YieldFairCampaignError(f"{method} paired budget is complete")
            if slot["new_unit"] and slot["unit_index"] >= INITIAL_UNITS:
                feasible = _initial_feasible_count(ledger, method)
                if feasible < 3:
                    raise YieldFairCampaignError(
                        "fewer than 3 feasible of 8 initial; stopping before EI without relaxing or refunding"
                    )
            if slot["new_unit"]:
                history = ledger.training_observations(method)
                if len(history) != slot["unit_index"]:
                    raise YieldFairCampaignError("proposer schedule and sealed history differ")
                proposal = tuner.propose(method, history, slot["unit_index"])
                candidate = proposal.candidate
            else:
                candidate = _unit_candidate(ledger, tuner, method, slot["unit_index"])
            attempt_id = _attempt_id(method, slot["unit_index"], slot["condition"])
            unit = ledger.begin(
                attempt_id=attempt_id,
                controller=method,
                candidate=candidate.as_dict(),
                condition=slot["condition"],
                unit=None if slot["new_unit"] else slot["unit_index"],
            )
            output_root = Path(state["output_root"])
            nominal_artifact = None
            if slot["condition"] == "disturbed":
                nominal_artifact, _ = _load_sealed_artifact(
                    ledger, output_root, _attempt_id(method, slot["unit_index"], "nominal"),
                )
            artifact_path = _artifact_path(output_root, attempt_id)
            kwargs = _run_kwargs(
                state=state,
                contract=contract,
                method=method,
                condition=slot["condition"],
                parameters=candidate.parameters,
            )
            artifact = execute(**kwargs)
            if not isinstance(artifact, Mapping):
                raise YieldFairCampaignError("runner must return an artifact object")
            if _execution_identities(Path(state["experiment_root"]), config) != state["campaign_protocol"]["execution_identities"]:
                raise YieldFairCampaignError("execution identity drifted during trial")
            if _source_hashes(Path(state["experiment_root"])) != state["campaign_protocol"]["source_hashes"]:
                raise YieldFairCampaignError("source identity drifted during trial")
            digest = _atomic_write_json(artifact_path, artifact)
            bound = _bound_contract(state, contract, method=method, parameters=candidate.parameters)
            if slot["condition"] == "nominal":
                sealed = _seal_nominal(
                    ledger, attempt_id=attempt_id, digest=digest, artifact=artifact, bound=bound,
                    inspect=inspect_fn,
                )
            else:
                nominal_artifact, _ = _load_sealed_artifact(
                    ledger, output_root, _attempt_id(method, slot["unit_index"], "nominal"),
                )
                sealed = _seal_disturbed(
                    ledger, attempt_id=attempt_id, digest=digest,
                    nominal_artifact=nominal_artifact, artifact=artifact, bound=bound,
                    pair_eval=pair_fn,
                )
            return {
                "attempt_id": attempt_id,
                "method": method,
                "unit_index": int(unit),
                "condition": slot["condition"],
                "artifact_path": str(artifact_path),
                "artifact_sha256": digest,
                **sealed,
                "formal_campaign_complete": False,
            }
        finally:
            ledger.close()


def reconcile(
    output: Path | str,
    *,
    interrupt: bool = False,
    inspect: Callable[..., Any] | None = None,
    pair_eval: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state = _load_state(output)
    with _exclusive_campaign(Path(state["output_root"])):
        config, contract, tuner, ledger = _open_bound(state)
        try:
            inflight = ledger.inflight()
            if inflight is None:
                return {"reconciled": False, "reason": "nothing inflight", "formal_campaign_complete": False}
            attempt_id = inflight["attempt_id"]
            method = inflight["controller"]
            condition = inflight["condition"]
            unit_index = int(inflight["unit"])
            output_root = Path(state["output_root"])
            artifact_path = _artifact_path(output_root, attempt_id)
            candidate = _unit_candidate(ledger, tuner, method, unit_index)
            bound = _bound_contract(state, contract, method=method, parameters=candidate.parameters)
            inspect_fn = inspect or inspect_member
            pair_fn = pair_eval or evaluate_pair
            if artifact_path.is_file():
                encoded = artifact_path.read_bytes()
                digest = _sha256_bytes(encoded)
                artifact = json.loads(encoded.decode("utf-8"))
                if not isinstance(artifact, Mapping):
                    raise YieldFairCampaignError("existing artifact is not an object")
                if artifact.get("method") != method:
                    raise YieldFairCampaignError("existing artifact identity differs; not fabricating success")
                if condition == "nominal":
                    sealed = _seal_nominal(
                        ledger, attempt_id=attempt_id, digest=digest, artifact=artifact, bound=bound,
                        inspect=inspect_fn,
                    )
                else:
                    nominal_artifact, _ = _load_sealed_artifact(
                        ledger, output_root, _attempt_id(method, unit_index, "nominal"),
                    )
                    sealed = _seal_disturbed(
                        ledger, attempt_id=attempt_id, digest=digest,
                        nominal_artifact=nominal_artifact, artifact=artifact, bound=bound,
                        pair_eval=pair_fn,
                    )
                return {
                    "reconciled": True,
                    "attempt_id": attempt_id,
                    "artifact_sha256": digest,
                    "interrupt": False,
                    **sealed,
                    "formal_campaign_complete": False,
                }
            if not interrupt:
                raise YieldFairCampaignError(
                    "inflight has no complete artifact; explicitly interrupt to seal an interruption"
                )
            interruption = {
                "schema": INTERRUPT_SCHEMA,
                "attempt_id": attempt_id,
                "method": method,
                "unit_index": unit_index,
                "condition": condition,
                "status": "interrupted",
                "objective_eligible": False,
                "reason": "explicit reconcile interrupt; consumed unit preserved",
                "claim_scope": CLAIM_SCOPE,
                "formal_campaign_complete": False,
            }
            digest = _atomic_write_json(_interrupt_path(output_root, attempt_id), interruption)
            ledger.seal(attempt_id, status="interrupted", evidence=_evidence(
                ledger, digest, objective_eligible=False, nominal_feasible=False,
                pair_feasible=False, disturbed_guards_ok=False, reason=interruption["reason"]))
            return {
                "reconciled": True,
                "attempt_id": attempt_id,
                "interrupt": True,
                "status": "interrupted",
                "artifact_sha256": digest,
                "formal_campaign_complete": False,
            }
        finally:
            ledger.close()


def freeze_campaign(output: Path | str) -> dict[str, Any]:
    state = _load_state(output)
    with _exclusive_campaign(Path(state["output_root"])):
        config, contract, tuner, ledger = _open_bound(state)
        export_path = Path(state["output_root"]) / "freeze.json"
        try:
            if ledger.inflight() is not None:
                raise YieldFairCampaignError("attempt inflight")
            _verify_sealed_artifact_files(ledger, Path(state["output_root"]))
            selected = {}
            identities = {}
            for method in METHODS:
                history = ledger.training_observations(method)
                if len(history) != TOTAL_UNITS:
                    raise YieldFairCampaignError("freeze requires complete 24 pairs per method")
                incumbent = tuner.propose(method, history[: INITIAL_UNITS + 12], 20)
                selected[method] = incumbent.candidate.as_dict()
                identities[method] = {
                    "candidate": incumbent.candidate.as_dict(),
                    "candidate_key": incumbent.candidate.key,
                    "unit_index": incumbent.unit_index,
                    "phase": incumbent.phase,
                }
            freeze_sha256 = ledger.freeze(selected)
            export = {
                "schema": FREEZE_SCHEMA,
                "version": 1,
                "selected_candidates": selected,
                "selection_identities": identities,
                "source_hashes": state["campaign_protocol"]["source_hashes"],
                "execution_identities": state["campaign_protocol"]["execution_identities"],
                "campaign_config_sha256": state["config_sha256"],
                "tuner_config_sha256": state["campaign_protocol"]["tuner_config_sha256"],
                "observer_config_sha256": state["campaign_protocol"]["observer_config_sha256"],
                "yield_protocol_sha256": state["campaign_protocol"]["yield_protocol_sha256"],
                "campaign_protocol_sha256": state["campaign_protocol_sha256"],
                "training_cell_id": state["campaign_protocol"]["training_cell_id"],
                "selection_contract_id": state["campaign_protocol"]["selection_contract_id"],
                "freeze_sha256": freeze_sha256,
                "formal_campaign_complete": False,
                "holdout_implemented": False,
                "hardware_qualified": False,
                "claim_scope": CLAIM_SCOPE,
            }
            if export_path.exists():
                prior = json.loads(export_path.read_text(encoding="utf-8"))
                if prior != json.loads(_canonical(export)):
                    raise YieldFairCampaignError("freeze export differs")
            else:
                _atomic_write_json(export_path, export)
            return export
        finally:
            ledger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    create = sub.add_parser("create", help="bind config and create an immutable campaign root")
    create.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    create.add_argument("--output", type=Path, required=True)
    status = sub.add_parser("status", help="show ledger progress without launching trials")
    status.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run-next", help="register and run exactly the next member")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--method", required=True, choices=METHODS)
    rec = sub.add_parser("reconcile", help="seal an existing artifact or an explicit interruption")
    rec.add_argument("--output", type=Path, required=True)
    rec.add_argument("--interrupt", action="store_true")
    freeze = sub.add_parser("freeze", help="freeze complete 24-pair schedules and export identities")
    freeze.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "create":
        result = create_campaign(args.output, config_path=args.config)
    elif args.command == "status":
        result = campaign_status(args.output)
    elif args.command == "run-next":
        result = run_next(args.output, method=args.method)
    elif args.command == "reconcile":
        result = reconcile(args.output, interrupt=args.interrupt)
    elif args.command == "freeze":
        result = freeze_campaign(args.output)
    else:
        parser.error("unknown command")
        return 2
    print(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
