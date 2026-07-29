#!/usr/bin/env python3
"""Permanent offline Step5d V3 no-empty parameter feeder.

The feeder owns only sealed-outbox post-processing, optimizer proposals, and
durable queue submission.  It deliberately contains no controller, bridge,
RTDE, socket, Dashboard, ARM, motion, or contact operation.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from step5d_autotune_v3.atomic_io import atomic_bytes
from step5d_parameter_bo import (
    formal_cuda_qlognei,
    load_observations,
    load_pending_candidates,
)
from step5d_parameter_outbox import process_postprocess_task
from step5d_parameter_queue import authoritative_view, submit_manifest
from step5d_parameter_search_domain import (
    augment_catalog_with_orientation_anchors,
    production_candidate_catalog,
    require_search_candidate,
)
from step5d_physics_soft_prior import PhysicsSoftPrior


CONFIG_SCHEMA = "step5d.parameter-receiver/no-empty-feeder-config-v1"
STATE_SCHEMA = "step5d.parameter-receiver/no-empty-feeder-state-v1"
INTENT_SCHEMA = "step5d.parameter-receiver/no-empty-feeder-intent-v1"
RECEIPT_SCHEMA = "step5d.parameter-receiver/no-empty-feeder-receipt-v1"
STATUS_SCHEMA = "step5d.parameter-receiver/no-empty-feeder-status-v1"


class FeederError(RuntimeError):
    """A feeder configuration, state, proposal, or submission is invalid."""


@dataclass(frozen=True)
class FeederConfig:
    queue_root: Path
    outbox_root: Path
    capture_root: Path
    campaign_config_path: Path
    control_contract_path: Path
    launch_profile_path: Path
    state_root: Path | None = None
    interval_s: float = 1.0
    # Watermarks are campaign configuration, not feeder mechanism defaults.
    # The no-tube handoff manifest supplies 8/4 for this campaign.
    target_depth: int | None = None
    low_watermark: int | None = None
    q: int = 8
    seed: int = 9009
    max_fallback_repeats: int = 8
    physics_soft_prior: PhysicsSoftPrior = field(default_factory=PhysicsSoftPrior)

    def __post_init__(self) -> None:
        for name in (
            "queue_root",
            "outbox_root",
            "capture_root",
            "campaign_config_path",
            "control_contract_path",
            "launch_profile_path",
        ):
            value = Path(getattr(self, name)).expanduser().absolute()
            if value.exists() and value.is_symlink():
                raise FeederError(f"{name} must not be a symlink")
            object.__setattr__(self, name, value)
        state_root = self.state_root
        if state_root is None:
            state_root = self.outbox_root / "feeder"
        else:
            state_root = Path(state_root).expanduser().absolute()
        if state_root.exists() and state_root.is_symlink():
            raise FeederError("state_root must not be a symlink")
        object.__setattr__(self, "state_root", state_root)
        if isinstance(self.interval_s, bool) or float(self.interval_s) <= 0.0:
            raise FeederError("interval_s must be positive")
        for name in ("target_depth", "q", "max_fallback_repeats"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise FeederError(f"{name} must be a positive integer")
        if (
            isinstance(self.low_watermark, bool)
            or not isinstance(self.low_watermark, int)
            or self.low_watermark < 0
            or self.low_watermark >= self.target_depth
        ):
            raise FeederError("low_watermark must be in [0, target_depth)")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise FeederError("seed must be a non-negative integer")
        if not isinstance(self.physics_soft_prior, PhysicsSoftPrior):
            raise FeederError("physics_soft_prior must be a PhysicsSoftPrior")

    @classmethod
    def from_json(cls, path: Path) -> "FeederConfig":
        path = path.expanduser().absolute()
        if path.is_symlink() or not path.is_file():
            raise FeederError("feeder config must be a real file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FeederError(f"feeder config is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
            raise FeederError("unsupported feeder config schema")

        def required(*names: str) -> Path:
            for name in names:
                value = payload.get(name)
                if isinstance(value, str) and value:
                    candidate = Path(value)
                    return candidate if candidate.is_absolute() else path.parent / candidate
            raise FeederError(f"feeder config lacks {names[0]}")

        def required_int(name: str) -> int:
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise FeederError(f"feeder config lacks integer {name}")
            return value

        return cls(
            queue_root=required("queue_root"),
            outbox_root=required("outbox_root"),
            capture_root=required("capture_root"),
            campaign_config_path=required("campaign_config", "campaign_config_path"),
            control_contract_path=required("control_contract", "control_contract_path"),
            launch_profile_path=required("launch_profile", "launch_profile_path"),
            state_root=(
                None
                if payload.get("state_root") in (None, "")
                else (
                    Path(str(payload["state_root"]))
                    if Path(str(payload["state_root"])).is_absolute()
                    else path.parent / str(payload["state_root"])
                )
            ),
            interval_s=float(payload.get("interval_s", 1.0)),
            target_depth=required_int("target_depth"),
            low_watermark=required_int("low_watermark"),
            q=int(payload.get("q", 8)),
            seed=int(payload.get("seed", 9009)),
            max_fallback_repeats=int(payload.get("max_fallback_repeats", 8)),
            physics_soft_prior=PhysicsSoftPrior.from_mapping(
                payload.get("physics_soft_prior")
            ),
        )


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _write_mutable(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_bytes(
        path,
        (json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise FeederError(f"immutable feeder record differs: {path}")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextlib.contextmanager
def _state_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise FeederError("feeder state root must be a real directory")
    descriptor = os.open(
        root / ".feeder.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _initial_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "cycle_sequence": 0,
        "processed_tasks": {},
        "pending_intent": None,
        "last_receipt": None,
    }


def _load_state(root: Path) -> dict[str, Any]:
    path = root / "state.json"
    if not path.exists():
        return _initial_state()
    if path.is_symlink() or not path.is_file():
        raise FeederError("feeder state is not a real file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FeederError("feeder state is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise FeederError("feeder state schema differs")
    if not isinstance(payload.get("cycle_sequence"), int) or payload["cycle_sequence"] < 0:
        raise FeederError("feeder cycle sequence is invalid")
    if not isinstance(payload.get("processed_tasks"), dict):
        raise FeederError("feeder processed task ledger is invalid")
    if payload.get("pending_intent") is not None and not isinstance(
        payload["pending_intent"], str
    ):
        raise FeederError("feeder pending intent is invalid")
    return payload


def _save_state(root: Path, state: Mapping[str, Any]) -> None:
    _write_mutable(root / "state.json", state)


def _read_object(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FeederError(f"{role} must be a real file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FeederError(f"{role} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FeederError(f"{role} must be an object")
    return payload


def _queue_depth(view: Mapping[str, Any]) -> int:
    """Return valid pending capacity; inflight is not backup capacity."""

    pending = view.get("pending_requests")
    state = view.get("state")
    if not isinstance(pending, Sequence) or isinstance(pending, (str, bytes)):
        raise FeederError("authoritative queue pending view is missing")
    if not isinstance(state, Mapping):
        raise FeederError("authoritative queue state view is missing")
    inflight = state.get("inflight")
    if inflight is not None and not isinstance(inflight, Mapping):
        raise FeederError("authoritative queue inflight view is invalid")
    return len(pending)


def _task_key(task_path: Path) -> str:
    return task_path.name


def _expected_result_path(task_path: Path, outbox_root: Path) -> Path:
    return outbox_root / "results" / f"{task_path.stem}.json"


def _sealed_result(path: Path) -> bool:
    """Accept only final immutable postprocess results, never ``.part`` files."""

    if path.is_symlink() or not path.is_file() or path.name.endswith(".part.json"):
        return False
    try:
        payload = _read_object(path, "postprocess result")
    except FeederError:
        return False
    return (
        payload.get("schema") == "step5d.parameter-receiver/postprocess-result-v1"
        and payload.get("status") == "SUCCEEDED"
    )


def _candidate_row(candidate: Any, *, source: str, nonce: str) -> dict[str, Any]:
    return {
        "candidate_uid": candidate.candidate_uid,
        "force_p_gain": candidate.force_p_gain,
        "force_i_gain": candidate.force_i_gain,
        "force_damping": candidate.force_damping,
        "normal_filter_tau_s": candidate.normal_filter_tau_s,
        "orientation_ko": candidate.orientation_ko,
        "position": "tail",
        "source": source,
        "occurrence_nonce": nonce,
    }


class ParameterFeeder:
    """One serialized feeder process with injectable offline seams for tests."""

    def __init__(
        self,
        config: FeederConfig,
        *,
        process_task: Callable[..., Any] | None = None,
        optimizer: Callable[..., Any] | None = None,
        submitter: Callable[..., Any] | None = None,
        queue_viewer: Callable[[Path], Mapping[str, Any]] | None = None,
        catalog_provider: Callable[[], Sequence[Any]] | None = None,
    ) -> None:
        self.config = config
        self.process_task = process_task or process_postprocess_task
        self.optimizer = optimizer or (
            lambda observations, candidates, *, q, seed: formal_cuda_qlognei(
                observations,
                candidates,
                q=q,
                seed=seed,
                physics_soft_prior=self.config.physics_soft_prior,
            )
        )
        self.submitter = submitter or submit_manifest
        self.queue_viewer = queue_viewer or authoritative_view
        self.catalog_provider = catalog_provider or production_candidate_catalog

    @property
    def state_root(self) -> Path:
        assert self.config.state_root is not None
        return self.config.state_root

    def _view(self) -> Mapping[str, Any]:
        return self.queue_viewer(self.config.queue_root)

    def _process_new_tasks(self, state: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        summary: dict[str, Any] = {"processed": [], "skipped_existing": [], "errors": []}
        tasks_root = self.config.outbox_root / "tasks"
        if dry_run or not tasks_root.exists():
            return summary
        if tasks_root.is_symlink() or not tasks_root.is_dir():
            summary["errors"].append({"task": str(tasks_root), "error": "tasks root is invalid"})
            return summary
        for task_path in sorted(tasks_root.glob("*.json"), key=lambda path: path.name):
            key = _task_key(task_path)
            if key in state["processed_tasks"]:
                continue
            try:
                result_path = _expected_result_path(task_path, self.config.outbox_root)
                if result_path.exists() or result_path.is_symlink():
                    if not _sealed_result(result_path):
                        raise FeederError("postprocess result is not a sealed terminal result")
                    state["processed_tasks"][key] = {
                        "status": "SKIPPED_EXISTING_RESULT",
                        "result": str(result_path),
                    }
                    summary["skipped_existing"].append(key)
                else:
                    self.process_task(
                        task_path,
                        outbox_root=self.config.outbox_root,
                        capture_root=self.config.capture_root,
                        campaign_config_path=self.config.campaign_config_path,
                        control_contract_path=self.config.control_contract_path,
                    )
                    if not _sealed_result(result_path):
                        raise FeederError("postprocess did not produce a sealed terminal result")
                    state["processed_tasks"][key] = {"status": "PROCESSED"}
                    summary["processed"].append(key)
                _save_state(self.state_root, state)
            except Exception as exc:  # one sealed task must not starve its siblings
                summary["errors"].append({"task": key, "error": str(exc)})
        return summary

    def _material_and_observations(
        self, view: Mapping[str, Any]
    ) -> tuple[tuple[Any, ...], frozenset[str], tuple[dict[str, Any], ...], frozenset[str], tuple[dict[str, Any], ...]]:
        observations, observed_uids, observation_material = load_observations(
            self.config.outbox_root
        )
        pending_uids, pending_material = load_pending_candidates(
            self.config.queue_root, queue_view=view
        )
        return observations, observed_uids, observation_material, pending_uids, pending_material

    def _load_intent(self, intent_id: str) -> dict[str, Any]:
        path = self.state_root / "intents" / f"{intent_id}.json"
        payload = _read_object(path, "feeder intent")
        if payload.get("schema") != INTENT_SCHEMA or payload.get("intent_id") != intent_id:
            raise FeederError("feeder intent identity differs")
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise FeederError("feeder intent rows are missing")
        return payload

    def _build_rows(
        self,
        *,
        need: int,
        basis: Mapping[str, Any],
        observations: Sequence[Any],
        observed_uids: frozenset[str],
        pending_uids: frozenset[str],
        task_errors: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
        excluded = set(observed_uids) | set(pending_uids)
        raw_catalog = augment_catalog_with_orientation_anchors(
            tuple(self.catalog_provider()),
            tuple(observation.candidate for observation in observations),
        )
        catalog = tuple(
            require_search_candidate(candidate, role="feeder catalog candidate")
            for candidate in raw_catalog
            if candidate.candidate_uid not in excluded
        )
        if len(catalog) < need:
            raise FeederError("candidate catalog is smaller than requested queue refill")
        fallback_reason: str | None = None
        optimizer_metadata: dict[str, Any] = {}
        selected: list[Any] = []
        if task_errors:
            fallback_reason = "postprocess_unavailable"
        else:
            try:
                q = min(need, self.config.q)
                selected_tuple, optimizer_metadata = self.optimizer(
                    observations, catalog, q=q, seed=self.config.seed
                )
                if (
                    len(selected_tuple) != q
                    or len({candidate.candidate_uid for candidate in selected_tuple}) != q
                    or any(candidate.candidate_uid in excluded for candidate in selected_tuple)
                ):
                    raise FeederError("optimizer returned excluded or duplicate candidates")
                selected.extend(
                    require_search_candidate(
                        candidate, role="optimizer-selected candidate"
                    )
                    for candidate in selected_tuple
                )
            except Exception as exc:
                fallback_reason = f"optimizer_unavailable:{type(exc).__name__}:{exc}"
        fallback_candidates: list[Any] = list(catalog)
        if fallback_reason is not None:
            if not fallback_candidates:
                raise FeederError("no validated candidate is available for degraded bootstrap")
        if len(selected) < need:
            if fallback_reason is None:
                fallback_reason = "optimizer_partial_result"
            if not fallback_candidates:
                raise FeederError("no validated fallback candidate is available")
            remaining = need - len(selected)
            available = [
                candidate
                for candidate in fallback_candidates
                if candidate.candidate_uid
                not in {row.candidate_uid for row in selected}
            ]
            if len(available) < remaining:
                raise FeederError("validated candidate catalog cannot satisfy unique refill")
            selected.extend(available[:remaining])
        if len(selected) > need:
            selected = selected[:need]
        mode = "formal_cuda_qlognei" if fallback_reason is None else "degraded_fallback"
        basis_digest = _digest(basis)
        rows = []
        for index, candidate in enumerate(selected):
            if fallback_reason is None:
                source = f"formal_cuda_qlognei_q{len(selected)}_seed{self.config.seed}"
            else:
                source = f"degraded_bootstrap:{fallback_reason}"
            nonce = hashlib.sha256(
                f"{basis_digest}:{mode}:{candidate.candidate_uid}:{index}".encode("utf-8")
            ).hexdigest()[:32]
            rows.append(_candidate_row(candidate, source=source, nonce=nonce))
        optimizer_metadata = dict(optimizer_metadata)
        optimizer_metadata.update(
            {"mode": mode, "basis_sha256": basis_digest, "selected_count": len(rows)}
        )
        return rows, fallback_reason, optimizer_metadata

    def _write_intent(self, payload: Mapping[str, Any]) -> str:
        body = dict(payload)
        body.pop("intent_id", None)
        intent_id = _digest(body)
        record = {**body, "intent_id": intent_id}
        _write_once(self.state_root / "intents" / f"{intent_id}.json", record)
        return intent_id

    def _submit_intent(self, intent: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        return tuple(
            self.submitter(
                self.config.queue_root,
                launch_profile_path=self.config.launch_profile_path,
                rows=tuple(intent["rows"]),
            )
        )

    def _write_cycle_records(
        self,
        *,
        cycle_id: int,
        receipt: Mapping[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        receipt_path = self.state_root / "receipts" / f"{cycle_id:012d}.json"
        _write_once(receipt_path, receipt)
        state["last_receipt"] = str(receipt_path)
        _save_state(self.state_root, state)
        _write_mutable(self.state_root / "status.json", {**receipt, "schema": STATUS_SCHEMA})
        return dict(receipt)

    def cycle(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Run one serialized feeder cycle and return its durable receipt."""

        with _state_lock(self.state_root):
            state = _load_state(self.state_root)
            cycle_id = int(state["cycle_sequence"]) + 1
            state["cycle_sequence"] = cycle_id
            _save_state(self.state_root, state)
            before_depth: int | None = None
            after_depth: int | None = None
            errors: list[dict[str, Any]] = []
            task_summary: dict[str, Any] = {"processed": [], "skipped_existing": [], "errors": []}
            submitted: tuple[dict[str, Any], ...] = ()
            fallback_reason: str | None = None
            proposal_basis: dict[str, Any] = {"cycle_sequence": cycle_id}
            proposal_mode = "dry_run" if dry_run else "not_needed_above_low_watermark"
            try:
                before_view = self._view()
                before_depth = _queue_depth(before_view)
                if not dry_run:
                    task_summary = self._process_new_tasks(state, dry_run=False)
                    errors.extend(task_summary["errors"])
                current_view = self._view()
                current_depth = _queue_depth(current_view)
                try:
                    (
                        observations,
                        observed_uids,
                        observation_material,
                        pending_uids,
                        pending_material,
                    ) = self._material_and_observations(current_view)
                except Exception as exc:
                    observations = ()
                    observed_uids = frozenset()
                    observation_material = ()
                    pending_uids = frozenset()
                    pending_material = ()
                    errors.append({"stage": "observation_load", "error": str(exc)})
                proposal_basis = {
                    "queue_revision": current_view["state"]["revision"],
                    "before_depth": before_depth,
                    "current_depth": current_depth,
                    "eligible_observation_count": len(observations),
                    "observed_candidate_count": len(observed_uids),
                    "pending_candidate_count": len(pending_uids),
                    "inflight": current_view["state"].get("inflight"),
                    "observation_material_sha256": _digest(observation_material),
                    "pending_material_sha256": _digest(pending_material),
                    "new_tasks": len(task_summary["processed"]),
                    "skipped_existing_results": len(task_summary["skipped_existing"]),
                    "task_error_count": len(task_summary["errors"]),
                    "seed": self.config.seed,
                    "q": self.config.q,
                }
                if dry_run:
                    after_depth = current_depth
                else:
                    intent_id = state.get("pending_intent")
                    if intent_id:
                        intent = self._load_intent(intent_id)
                        proposal_mode = "restart_idempotent_resubmit"
                    elif current_depth > self.config.low_watermark:
                        intent = None
                    else:
                        need = self.config.target_depth - current_depth
                        if need <= 0:
                            intent = None
                        else:
                            basis = {**proposal_basis, "target_depth": self.config.target_depth}
                            rows, fallback_reason, optimizer_metadata = self._build_rows(
                                need=need,
                                basis=basis,
                                observations=observations,
                                observed_uids=observed_uids,
                                pending_uids=pending_uids,
                                task_errors=task_summary["errors"],
                            )
                            proposal_mode = optimizer_metadata["mode"]
                            proposal_basis = {**proposal_basis, "basis_sha256": _digest(basis)}
                            intent_id = self._write_intent(
                                {
                                    "schema": INTENT_SCHEMA,
                                    "created_cycle": cycle_id,
                                    "before_depth": before_depth,
                                    "requested_count": len(rows),
                                    "proposal_basis": proposal_basis,
                                    "proposal_mode": proposal_mode,
                                    "fallback_reason": fallback_reason,
                                    "optimizer": optimizer_metadata,
                                    "rows": rows,
                                }
                            )
                            state["pending_intent"] = intent_id
                            _save_state(self.state_root, state)
                            intent = self._load_intent(intent_id)
                    if intent is not None:
                        try:
                            submitted = self._submit_intent(intent)
                            after_view = self._view()
                            after_depth = _queue_depth(after_view)
                            state["pending_intent"] = None
                            _save_state(self.state_root, state)
                        except Exception as exc:
                            errors.append({"stage": "queue_submit", "error": str(exc)})
                            after_depth = _queue_depth(self._view())
                            submitted = ()
                    else:
                        after_depth = _queue_depth(self._view())
                        submitted = ()
                receipt = {
                    "schema": RECEIPT_SCHEMA,
                    "status": "DEGRADED" if fallback_reason or errors else "SUCCEEDED",
                    "cycle_sequence": cycle_id,
                    "dry_run": dry_run,
                    "before_depth": before_depth,
                    "after_depth": after_depth,
                    "proposal_basis": proposal_basis,
                    "proposal_mode": proposal_mode,
                    "fallback_reason": fallback_reason,
                    "errors": errors,
                    "processed_tasks": task_summary["processed"],
                    "skipped_existing_results": task_summary["skipped_existing"],
                    "failed_tasks": task_summary["errors"],
                    "submitted_request_uids": [row.get("request_uid") for row in submitted],
                }
            except Exception as exc:
                errors.append({"stage": "cycle", "error": str(exc)})
                try:
                    after_depth = _queue_depth(self._view())
                except Exception:
                    after_depth = None
                receipt = {
                    "schema": RECEIPT_SCHEMA,
                    "status": "FAILED",
                    "cycle_sequence": cycle_id,
                    "dry_run": dry_run,
                    "before_depth": before_depth,
                    "after_depth": after_depth,
                    "proposal_basis": proposal_basis,
                    "proposal_mode": proposal_mode,
                    "fallback_reason": fallback_reason,
                    "errors": errors,
                    "processed_tasks": task_summary["processed"],
                    "skipped_existing_results": task_summary["skipped_existing"],
                    "failed_tasks": task_summary["errors"],
                    "submitted_request_uids": [],
                }
            return self._write_cycle_records(cycle_id=cycle_id, receipt=receipt, state=state)

    def run_forever(
        self,
        *,
        dry_run: bool = False,
        max_cycles: int | None = None,
        stop_event: Any | None = None,
    ) -> int:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            self.cycle(dry_run=dry_run)
            cycles += 1
            if stop_event is not None and stop_event.is_set():
                break
            if max_cycles is not None and cycles >= max_cycles:
                break
            time.sleep(float(self.config.interval_s))
        return 0


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="offline feeder JSON config")
    parser.add_argument("--once", action="store_true", help="run exactly one cycle")
    parser.add_argument("--dry-run", action="store_true", help="prove status without queue mutation")
    parser.add_argument("--max-cycles", type=int, help="offline test bound; omit for durable wait forever")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.max_cycles is not None and args.max_cycles < 1:
            raise FeederError("--max-cycles must be positive")
        config = FeederConfig.from_json(args.config)
        feeder = ParameterFeeder(config)
        if args.once:
            args.max_cycles = 1
        return feeder.run_forever(
            dry_run=args.dry_run,
            max_cycles=args.max_cycles,
        )
    except (FeederError, OSError, ValueError, RuntimeError) as exc:
        print(f"step5d parameter feeder: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
