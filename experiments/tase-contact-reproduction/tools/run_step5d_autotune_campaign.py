#!/usr/bin/env python3
"""Run one durable Step5d-native campaign against an existing bridge.

The TP and bridge are long-lived.  This process is the single campaign writer:
it persists ARM/ACK intent before publishing the mailbox command, follows the
bridge CSV for TP state and safe-home evidence, seals the immutable trial
bundle, and advances automatically until success or a typed stop phase.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


def _bootstrap_stable_cuda_runtime() -> None:
    """Re-exec the CLI with the persistent Step5d CuPy/CUDA runtime."""

    if __name__ != "__main__" or os.environ.get("STEP5D_CUDA_BOOTSTRAPPED") == "1":
        return
    runtime = Path(
        os.environ.get(
            "STEP5D_PYTHON_RUNTIME_ROOT",
            "/home/andy/.codex-python/ur10e-digital-twin-20260711",
        )
    )
    if not (runtime / "cupy").is_dir():
        return
    library_dirs = tuple(
        runtime / "nvidia" / package / "lib"
        for package in ("cuda_nvrtc", "nvjitlink", "cuda_runtime")
        if (runtime / "nvidia" / package / "lib").is_dir()
    )
    required_libraries = tuple(
        runtime / "nvidia" / package / "lib"
        for package in ("cuda_nvrtc", "cuda_runtime")
    )
    if not all(path.is_dir() for path in required_libraries):
        return
    environment = dict(os.environ)
    environment["STEP5D_CUDA_BOOTSTRAPPED"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(runtime), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (*map(str, library_dirs), environment.get("LD_LIBRARY_PATH", ""))
    ).rstrip(os.pathsep)
    os.execve(sys.executable, [sys.executable, *sys.argv], environment)


_bootstrap_stable_cuda_runtime()

from step5d_autotune_backend import (
    CampaignAuthorization,
    Step5dV35Backend,
)
from step5d_autotune_batch_plan import (
    CandidateBatchPlan,
    assert_append_only,
    load_plan,
)
from step5d_autotune_contract import (
    CampaignSpec,
    ExecutionProfile,
    ForceCandidate,
    sha256_json,
)
from step5d_autotune_coordinator import CampaignCoordinator, MailboxObservation
from step5d_autotune_journal import (
    JournalIntegrityError,
    JournalReference,
    ReconcileAction,
    SupervisorJournal,
    TpSnapshot,
)
from step5d_autotune_live_driver import (
    AtomicCommandMailbox,
    CampaignHomeReference,
    HostClosureCollector,
    TrialArtifactProducer,
    finalize_produced_bundle_and_dispatch_ack,
)
from step5d_autotune_state_machine import TpLoopState
from step5d_autotune_store import CampaignStore
from step5d_autotune_supervisor import (
    TERMINAL_PHASES,
    CampaignPhase,
    CampaignSupervisor,
    execution_profile_integer_id,
)
from step5d_autotune_runtime_lifecycle import (
    BatchAttemptContext,
    PostAckClosureCollector,
    PreAckTypedClosureCollector,
    next_runtime_batch_candidate,
    prepare_batch_attempt_context,
    recover_runtime_batch_trial_briefs,
    runtime_batch_verified_complete,
)


STATE_NAMES = {int(state): state.name for state in TpLoopState}


class StopAfterCurrentRequested(RuntimeError):
    """The v3 durable latch was observed while the TP was safely Home."""


def _finite(row: Mapping[str, str], name: str) -> float:
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"bridge row lacks finite {name}") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"bridge row has non-finite {name}")
    return value


def _integer(row: Mapping[str, str], name: str) -> int:
    value = _finite(row, name)
    if not value.is_integer():
        raise RuntimeError(f"bridge row {name} is not an integer")
    return int(value)


def _wait_for_async_capture(path: Path, *, timeout_s: float = 3.0) -> None:
    """Wait only after WAIT_ACK, while TP/bridge continue their zero-output hold."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            return
        time.sleep(0.005)
    raise RuntimeError("V3 asynchronous capture was not durably published at WAIT_ACK")


def tp_snapshot_from_bridge_row(row: Mapping[str, str]) -> TpSnapshot:
    state_value = _integer(row, "ur_output_int_register_26")
    try:
        state = STATE_NAMES[state_value]
    except KeyError as exc:
        raise RuntimeError(f"bridge row has unknown TP state {state_value}") from exc
    return TpSnapshot(
        campaign_epoch_echo=_integer(row, "ur_output_int_register_24"),
        trial_id_echo=_integer(row, "ur_output_int_register_25"),
        state=state,
        candidate_token_echo=_integer(row, "ur_output_int_register_27"),
        terminal_reason=_integer(row, "ur_output_int_register_28"),
        execution_profile_integer_id_echo=_integer(
            row, "ur_output_int_register_29"
        ),
        consumed_command_seq=_integer(row, "ur_output_int_register_30"),
    )


def closure_sample_from_bridge_row(row: Mapping[str, str]) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "timestamp": _finite(row, "ur_timestamp"),
        "safety_mode": _integer(row, "ur_safety_mode"),
    }
    for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd"):
        sample[name] = [
            _finite(row, f"ur_{name}_{index}") for index in range(6)
        ]
    for index in range(24, 31):
        sample[f"output_int_register_{index}"] = _integer(
            row, f"ur_output_int_register_{index}"
        )
    for index in range(36, 39):
        sample[f"output_double_register_{index}"] = _finite(
            row, f"ur_output_double_register_{index}"
        )
    return sample


class BridgeCsvFollower:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file() or self.path.is_symlink():
            raise RuntimeError("bridge CSV must be an existing regular file")
        self.handle = self.path.open("r", newline="", encoding="utf-8")
        header = self.handle.readline()
        self.fieldnames = next(csv.reader([header]))
        if not self.fieldnames or len(self.fieldnames) != len(set(self.fieldnames)):
            raise RuntimeError("bridge CSV header is missing or duplicated")
        self.handle.seek(0, os.SEEK_END)

    def rows(self, *, timeout_s: float) -> Iterator[dict[str, str]]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            position = self.handle.tell()
            line = self.handle.readline()
            if not line or not line.endswith("\n"):
                self.handle.seek(position)
                time.sleep(0.01)
                continue
            values = next(csv.reader([line]))
            if len(values) != len(self.fieldnames):
                raise RuntimeError("bridge CSV row width changed")
            yield dict(zip(self.fieldnames, values))
        raise TimeoutError("timed out waiting for a fresh bridge row")

    def close(self) -> None:
        self.handle.close()


def _latest_complete_row(path: Path) -> dict[str, str]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        latest: dict[str, str] | None = None
        for row in reader:
            if None not in row and all(value is not None for value in row.values()):
                latest = dict(row)
    if latest is None:
        raise RuntimeError("bridge CSV has no complete data row")
    return latest


def _campaign_spec(
    root: Path,
    fingerprint: str,
    epoch: int,
    *,
    campaign_id: str | None = None,
) -> CampaignSpec:
    source = json.loads(
        (root / "config" / "step5d_autotune_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = source["baseline"]
    objective = source["objective"]
    return CampaignSpec(
        campaign_id=campaign_id or f"step5d-native-{epoch}",
        campaign_epoch=epoch,
        campaign_fingerprint=fingerprint,
        target_force_n=float(baseline["target_force_n"]),
        objective_window_start_s=float(objective["window_s"][0]),
        objective_window_end_s=float(objective["window_s"][1]),
        objective_bin_s=float(objective["bin_s"]),
        required_bins=int(objective["required_complete_bins"]),
        success_mae_n=float(objective["success_mae_n"]),
        confirmation_relative_delta_max=float(
            objective.get("confirmation_relative_delta_max", 0.15)
        ),
        f0_shadow_reaction_normal_base=tuple(
            float(value) for value in baseline["f0_shadow_reaction_normal_base"]
        ),
    )


def _campaign_from_payload(payload: Mapping[str, Any]) -> CampaignSpec:
    values = dict(payload)
    shadow = values.get("f0_shadow_reaction_normal_base")
    if shadow is not None:
        values["f0_shadow_reaction_normal_base"] = tuple(shadow)
    return CampaignSpec(**values)


@dataclass(frozen=True)
class CampaignEpochLayout:
    epoch: int
    root: Path
    store_root: Path
    journal_root: Path
    manifest: Mapping[str, Any]
    campaign: CampaignSpec


@dataclass(frozen=True)
class AdoptedCandidateHistory:
    campaign_id: str
    campaign_epoch: int
    profile_id: str
    plant_epoch: int
    executed_candidates: tuple[ForceCandidate, ...]
    physically_attempted_candidate_uids: frozenset[str]
    fingerprint: str


def discover_campaign_epochs(campaign_root: Path) -> tuple[CampaignEpochLayout, ...]:
    """Return the verified epoch chain instead of assuming epoch 1 is parent."""

    campaign_root = campaign_root.resolve()
    roots: list[Path] = []
    if (campaign_root / "store" / "campaign.json").is_file():
        roots.append(campaign_root)
    epochs_root = campaign_root / "epochs"
    if epochs_root.is_dir():
        for candidate in sorted(epochs_root.iterdir()):
            if candidate.is_symlink() or not candidate.is_dir():
                raise RuntimeError("campaign epochs directory contains an unsafe entry")
            if len(candidate.name) != 10 or not candidate.name.isdigit():
                raise RuntimeError("campaign epoch directory name must be ten digits")
            if (candidate / "store" / "campaign.json").is_file():
                roots.append(candidate.resolve())

    layouts: list[CampaignEpochLayout] = []
    seen_epochs: set[int] = set()
    campaign_id: str | None = None
    for epoch_root in roots:
        manifest_path = epoch_root / "store" / "campaign.json"
        if manifest_path.is_symlink():
            raise RuntimeError("campaign manifest must not be a symlink")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("campaign"), Mapping
        ):
            raise RuntimeError("campaign manifest lacks a campaign object")
        campaign = _campaign_from_payload(manifest["campaign"])
        if epoch_root.parent == epochs_root and int(epoch_root.name) != campaign.campaign_epoch:
            raise RuntimeError("campaign epoch directory disagrees with its manifest")
        if campaign.campaign_epoch in seen_epochs:
            raise RuntimeError("campaign epoch appears in more than one store")
        if campaign_id is not None and campaign.campaign_id != campaign_id:
            raise RuntimeError("campaign epoch chain crosses campaign_id")
        seen_epochs.add(campaign.campaign_epoch)
        campaign_id = campaign_id or campaign.campaign_id
        layouts.append(
            CampaignEpochLayout(
                epoch=campaign.campaign_epoch,
                root=epoch_root,
                store_root=epoch_root / "store",
                journal_root=epoch_root / "journal",
                manifest=manifest,
                campaign=campaign,
            )
        )
    return tuple(sorted(layouts, key=lambda row: row.epoch))


def select_campaign_epoch(
    campaign_root: Path,
    *,
    campaign_epoch: int | None = None,
) -> CampaignEpochLayout:
    """Select one durable epoch explicitly when a failed newer epoch is retained."""

    chain = discover_campaign_epochs(campaign_root.resolve())
    if not chain:
        raise RuntimeError("legacy campaign root has no durable epoch")
    if campaign_epoch is None:
        return chain[-1]
    matches = tuple(row for row in chain if row.epoch == campaign_epoch)
    if len(matches) != 1:
        raise RuntimeError(
            f"legacy campaign epoch {campaign_epoch} is not uniquely available"
        )
    return matches[0]


def validate_legacy_campaign_adoption(
    campaign_root: Path,
    *,
    campaign_epoch: int | None = None,
) -> dict[str, Any]:
    """Exercise the complete retained-parent read path before operator Play."""

    layout = select_campaign_epoch(
        campaign_root,
        campaign_epoch=campaign_epoch,
    )
    latest = SupervisorJournal(layout.journal_root).load_latest()
    profile = profile_from_epoch(layout)
    store = CampaignStore(layout.store_root)
    history = store.read_resume_history()
    promotion = store.read_promotion_history()
    if latest.state.phase != "home":
        raise RuntimeError(
            f"legacy campaign epoch {layout.epoch} is not durably Home"
        )
    return {
        "campaign_id": layout.campaign.campaign_id,
        "campaign_epoch": layout.epoch,
        "journal_revision": latest.revision,
        "journal_record_sha256": latest.record_sha256,
        "phase": latest.state.phase,
        "execution_profile_id": profile.profile_id,
        "resume_history_count": len(history),
        "promotion_history_count": len(promotion),
        "root": str(layout.root),
    }


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _profile(root: Path) -> ExecutionProfile:
    source = json.loads(
        (root / "config" / "step5d_autotune_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = source["baseline"]
    return ExecutionProfile(
        profile_id=baseline["execution_profile_id"],
        normal_max_rate_rad_s=float(baseline["normal_max_rate_rad_s"]),
        host_qdot_slew_rad_s2=float(baseline["host_qdot_slew_rad_s2"]),
        tp_speedj_accel_rad_s2=float(baseline["tp_speedj_accel_rad_s2"]),
    )


def profile_from_epoch(layout: CampaignEpochLayout) -> ExecutionProfile:
    """Restore the retained profile from durable epoch bytes, never current config."""

    latest = SupervisorJournal(layout.journal_root).load_latest()
    payload = layout.manifest.get("execution_profile")
    if not isinstance(payload, Mapping):
        state = latest.state
        cursors = [
            state.active_trial,
            None if state.pending_ack is None else state.pending_ack.trial,
            None if state.pending_retry is None else state.pending_retry.origin_trial,
        ]
        for cursor in cursors:
            if cursor is None:
                continue
            trial_payload = json.loads(
                Path(cursor.trial_spec.path).read_text(encoding="utf-8")
            )
            payload = trial_payload.get("execution_profile")
            if isinstance(payload, Mapping):
                break
        if not isinstance(payload, Mapping):
            for row in reversed(CampaignStore(layout.store_root).read_resume_history()):
                trial_payload = row.get("trial")
                candidate = (
                    trial_payload.get("execution_profile")
                    if isinstance(trial_payload, Mapping)
                    else None
                )
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("profile_id") == state.execution_profile_id
                ):
                    payload = candidate
                    break
    if not isinstance(payload, Mapping):
        raise RuntimeError("durable epoch does not contain its retained execution profile")
    profile = ExecutionProfile(**dict(payload))
    if any(
        (
            profile.profile_id != latest.state.execution_profile_id,
            execution_profile_integer_id(profile)
            != latest.state.execution_profile_integer_id,
        )
    ):
        raise RuntimeError("durable epoch profile differs from its journal binding")
    return profile


def _campaign_authorization(
    path: Path,
    *,
    campaign: CampaignSpec,
    campaign_fingerprint: str,
) -> CampaignAuthorization:
    """Load an external owner-produced authorization bound to one fingerprint."""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError("campaign authorization must be an absolute regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "campaign_id",
        "campaign_epoch",
        "campaign_fingerprint",
        "bounded_baseline_and_loop",
        "live_authorized",
        "controller_readback_verified",
        "authorization_source",
        "authorized_at",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise RuntimeError("campaign authorization fields differ from v1 contract")
    if payload["schema_version"] != "step5d_autotune_campaign_authorization_v1":
        raise RuntimeError("campaign authorization schema mismatch")
    if any(
        (
            payload["campaign_id"] != campaign.campaign_id,
            payload["campaign_epoch"] != campaign.campaign_epoch,
            payload["campaign_fingerprint"] != campaign_fingerprint,
            payload["bounded_baseline_and_loop"] is not True,
            payload["live_authorized"] is not True,
            payload["controller_readback_verified"] is not True,
            not isinstance(payload["authorization_source"], str),
            not payload["authorization_source"].strip(),
            not isinstance(payload["authorized_at"], str),
            not payload["authorized_at"].strip(),
        )
    ):
        raise RuntimeError("campaign authorization is not bound to this exact epoch/fingerprint")
    return CampaignAuthorization(
        campaign_id=campaign.campaign_id,
        campaign_fingerprint=campaign_fingerprint,
        authorization_ref_sha256=sha256_json(payload),
        bounded_baseline_and_loop=True,
        live_authorized=True,
        controller_readback_verified=True,
    )


def _campaign_binding(
    path: Path,
    *,
    campaign: CampaignSpec,
    campaign_fingerprint: str,
) -> CampaignAuthorization:
    """Load the machine-generated V3 epoch/fingerprint binding (not user auth)."""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError("campaign binding must be an absolute regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "campaign_id",
        "campaign_epoch",
        "campaign_fingerprint",
        "bounded_baseline_and_loop",
        "controller_readback_verified",
        "binding_source",
        "generated_at",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise RuntimeError("campaign binding fields differ from V3 contract")
    if payload["schema_version"] != "step5d_autotune_campaign_binding_v2":
        raise RuntimeError("campaign binding schema mismatch")
    if any(
        (
            payload["campaign_id"] != campaign.campaign_id,
            payload["campaign_epoch"] != campaign.campaign_epoch,
            payload["campaign_fingerprint"] != campaign_fingerprint,
            payload["bounded_baseline_and_loop"] is not True,
            payload["controller_readback_verified"] is not True,
            not isinstance(payload["binding_source"], str),
            not payload["binding_source"].strip(),
            not isinstance(payload["generated_at"], str),
            not payload["generated_at"].strip(),
        )
    ):
        raise RuntimeError("campaign binding is not bound to this exact epoch/fingerprint")
    return CampaignAuthorization(
        campaign_id=campaign.campaign_id,
        campaign_fingerprint=campaign_fingerprint,
        authorization_ref_sha256=sha256_json(payload),
        bounded_baseline_and_loop=True,
        live_authorized=True,
        controller_readback_verified=True,
    )


def _event(path: Path, event: str, **payload: Any) -> None:
    row = {"event": event, "monotonic_s": time.monotonic(), **payload}
    encoded = json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def ensure_mailbox_parent(
    mailbox_path: Path,
    bridge_run: Path,
    *,
    v3_runtime_root: Path | None = None,
) -> None:
    if v3_runtime_root is None:
        expected_bridge = bridge_run.resolve()
        expected_parent = (bridge_run / "runtime").resolve()
    else:
        root = v3_runtime_root.expanduser().resolve()
        expected_bridge = (root / "bridge").resolve()
        expected_parent = root
    if bridge_run.resolve() != expected_bridge or mailbox_path.parent.resolve() != expected_parent:
        raise RuntimeError(
            "mailbox/bridge paths differ from the selected bridge run/runtime layout"
        )
    mailbox_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not mailbox_path.parent.is_dir() or mailbox_path.parent.is_symlink():
        raise RuntimeError("mailbox parent must be a real directory")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(dict(payload), indent=2, sort_keys=True, allow_nan=False) + "\n"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _publish_runner_ready(
    path: Path,
    *,
    durable_state_ready: bool,
    bridge_run: Path,
    campaign_root: Path,
    campaign: CampaignSpec,
    campaign_fingerprint: str,
    selection_policy: str,
) -> None:
    if durable_state_ready is not True:
        raise RuntimeError("runner ready requires completed durable state recovery")
    _atomic_json(
        path,
        {
            "schema_version": "step5d_autotune_runner_ready_v1",
            "ok": True,
            "pid": os.getpid(),
            "bridge_run": str(bridge_run),
            "campaign_root": str(campaign_root),
            "campaign_epoch": campaign.campaign_epoch,
            "campaign_fingerprint": campaign_fingerprint,
            "selection_policy": selection_policy,
            "state": "ready_home",
            "durable_state_ready": True,
        },
    )


def _verified_parent_layout(manifest: Mapping[str, Any]) -> CampaignEpochLayout | None:
    parent = manifest.get("parent_epoch")
    if parent is None:
        return None
    if not isinstance(parent, Mapping) or set(parent) != {
        "campaign_epoch",
        "journal_revision",
        "journal_record_sha256",
        "root",
        "store_manifest_sha256",
    }:
        raise RuntimeError("parent epoch reference is incomplete")
    root = Path(str(parent["root"]))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise RuntimeError("parent epoch root is not a safe absolute directory")
    manifest_path = root / "store" / "campaign.json"
    if _sha256_path(manifest_path) != parent["store_manifest_sha256"]:
        raise RuntimeError("parent epoch store manifest digest changed")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    campaign = _campaign_from_payload(payload["campaign"])
    latest = SupervisorJournal(root / "journal").load_latest()
    if any(
        (
            campaign.campaign_epoch != parent["campaign_epoch"],
            latest.revision != parent["journal_revision"],
            latest.record_sha256 != parent["journal_record_sha256"],
        )
    ):
        raise RuntimeError("parent epoch journal identity changed")
    return CampaignEpochLayout(
        epoch=campaign.campaign_epoch,
        root=root,
        store_root=root / "store",
        journal_root=root / "journal",
        manifest=payload,
        campaign=campaign,
    )


def _prior_resume_history(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    layouts: list[CampaignEpochLayout] = []
    current = _verified_parent_layout(manifest)
    seen: set[Path] = set()
    while current is not None:
        if current.root in seen:
            raise RuntimeError("parent epoch chain contains a cycle")
        seen.add(current.root)
        layouts.append(current)
        current = _verified_parent_layout(current.manifest)
    rows: list[dict[str, Any]] = []
    for layout in reversed(layouts):
        rows.extend(CampaignStore(layout.store_root).read_resume_history())
    return rows


def adopted_candidate_history(
    campaign_root: Path,
    *,
    campaign_epoch: int | None = None,
    plant_epoch: int = 1,
) -> AdoptedCandidateHistory:
    """Load exact executed anchors and attempted identities for code-epoch adoption."""

    layout = select_campaign_epoch(
        campaign_root,
        campaign_epoch=campaign_epoch,
    )
    profile_id = profile_from_epoch(layout).profile_id
    rows = [
        *_prior_resume_history(layout.manifest),
        *CampaignStore(layout.store_root).read_resume_history(),
    ]
    executed: list[ForceCandidate] = []
    for row in rows:
        profile = row.get("execution_profile")
        if (
            not isinstance(profile, Mapping)
            or profile.get("profile_id") != profile_id
            or row.get("plant_epoch") != plant_epoch
        ):
            continue
        payload = row.get("candidate")
        if not isinstance(payload, Mapping):
            raise RuntimeError("adopted candidate history lacks candidate payload")
        candidate = ForceCandidate(
            force_p_gain=payload.get("force_p_gain"),
            force_i_gain=payload.get("force_i_gain"),
            force_damping=payload.get("force_damping"),
        )
        if row.get("candidate_uid") != candidate.candidate_uid:
            raise RuntimeError("adopted candidate history identity differs")
        executed.append(candidate)
    if not executed:
        raise RuntimeError("adopted candidate history has no compatible executed anchor")

    latest = SupervisorJournal(layout.journal_root).load_latest()
    attempted = {candidate.candidate_uid for candidate in executed}
    attempted.update(
        fate.trial.candidate_uid for fate in latest.state.terminal_fates
    )
    fingerprint_payload = {
        "campaign_id": layout.campaign.campaign_id,
        "campaign_epoch": layout.epoch,
        "journal_record_sha256": latest.record_sha256,
        "profile_id": profile_id,
        "plant_epoch": plant_epoch,
        "executed_candidate_uids": [
            candidate.candidate_uid for candidate in executed
        ],
        "physically_attempted_candidate_uids": sorted(attempted),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return AdoptedCandidateHistory(
        campaign_id=layout.campaign.campaign_id,
        campaign_epoch=layout.epoch,
        profile_id=profile_id,
        plant_epoch=plant_epoch,
        executed_candidates=tuple(executed),
        physically_attempted_candidate_uids=frozenset(attempted),
        fingerprint=fingerprint,
    )


def _infra_abort_evidence(latest: Any) -> tuple[JournalReference, TpSnapshot]:
    cursor = latest.state.active_trial
    if cursor is None:
        raise RuntimeError("infra-abort recovery lacks an active trial")
    trial_spec_path = Path(cursor.trial_spec.path)
    trial_payload = json.loads(trial_spec_path.read_text(encoding="utf-8"))
    provenance = trial_payload.get("provenance_run_dir")
    if not isinstance(provenance, str):
        raise RuntimeError("active trial lacks its provenance bridge run")
    bridge_run = Path(provenance)
    marker_path = bridge_run / ".capture_complete.json"
    summary_path = bridge_run / "summary.json"
    partial_rel = f"autotune_trials/{cursor.trial_uid}/capture.csv.part"
    partial_path = bridge_run / partial_rel
    if any(path.is_symlink() or not path.is_file() for path in (marker_path, summary_path, partial_path)):
        raise RuntimeError("infra-abort recovery evidence is missing or unsafe")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("capture_closed") is not True or marker.get("immutable") is not True:
        raise RuntimeError("infra-abort bridge run is not immutably closed")
    source_files = marker.get("source_files")
    if not isinstance(source_files, list):
        raise RuntimeError("infra-abort completion marker lacks source files")
    indexed = {
        row.get("path"): row
        for row in source_files
        if isinstance(row, Mapping) and isinstance(row.get("path"), str)
    }
    for relative, path in (("summary.json", summary_path), (partial_rel, partial_path)):
        row = indexed.get(relative)
        if not isinstance(row, Mapping) or row.get("sha256") != _sha256_path(path):
            raise RuntimeError(f"infra-abort evidence digest differs: {relative}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("stop_reason") != "signal_sigint":
        raise RuntimeError("infra-abort recovery requires exact signal_sigint stop evidence")
    latest_partial = _latest_complete_row(partial_path)
    if any(
        (
            _integer(latest_partial, "ur_output_int_register_25") != cursor.trial_id,
            _integer(latest_partial, "ur_output_int_register_27")
            != cursor.candidate_token,
            _integer(latest_partial, "ur_output_int_register_30")
            != cursor.arm_command_seq,
        )
    ):
        raise RuntimeError("partial capture does not bind the exact consumed ARM")
    digest = _sha256_path(marker_path)
    return (
        JournalReference(
            reference_id=digest,
            path=str(marker_path.resolve()),
            sha256=digest,
        ),
        tp_snapshot_from_bridge_row(latest_partial),
    )


def _mailbox_observation_for_latest(latest: Any) -> MailboxObservation:
    cursor = latest.state.active_trial
    if cursor is None:
        return MailboxObservation.missing()
    trial_payload = json.loads(
        Path(cursor.trial_spec.path).read_text(encoding="utf-8")
    )
    provenance = trial_payload.get("provenance_run_dir")
    if not isinstance(provenance, str):
        raise RuntimeError("active trial lacks provenance for mailbox recovery")
    mailbox = AtomicCommandMailbox(
        Path(provenance) / "runtime" / "command.json",
        network_mode=True,
    )
    return MailboxObservation.from_command(mailbox.read_latest())


def _settle_home_after_restart(
    coordinator: CampaignCoordinator,
    snapshot: TpSnapshot,
    *,
    persist: bool,
    recover_infra_aborted_active: bool,
    trial_brief_admissions: Mapping[str, Any] | None = None,
) -> ReconcileAction:
    latest = coordinator.latest
    active = None if latest is None else latest.state.active_trial
    if (
        recover_infra_aborted_active
        and active is not None
        and snapshot.state == "READY_HOME"
        and snapshot.consumed_command_seq in {0, active.arm_command_seq}
    ):
        evidence, consumed_snapshot = _infra_abort_evidence(latest)
        coordinator.terminalize_consumed_infra_abort(
            replace(
                snapshot,
                consumed_command_seq=consumed_snapshot.consumed_command_seq,
            ),
            evidence=evidence,
            persist=persist,
        )
        return ReconcileAction.RESUME_HOME
    pending = None if latest is None else latest.state.pending_ack
    admission = (
        None
        if pending is None or trial_brief_admissions is None
        else trial_brief_admissions.get(pending.trial.trial_uid)
    )
    return coordinator.reconcile(
        snapshot,
        trial_brief_admission=admission,
    ).decision.action


def _wait_for_codex_candidate(
    *,
    plan_path: Path,
    campaign_id: str,
    supervisor: CampaignSupervisor,
    coordinator: CampaignCoordinator,
    bridge_csv: Path,
    campaign_root: Path,
    previous_plan: CandidateBatchPlan | None,
    timeout_s: float,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[ForceCandidate | None, CandidateBatchPlan]:
    deadline = time.monotonic() + timeout_s
    announced_revision: int | None = None
    while time.monotonic() < deadline:
        if stop_requested is not None and stop_requested():
            raise StopAfterCurrentRequested
        if plan_path.is_file() and not plan_path.is_symlink():
            plan = load_plan(plan_path, campaign_id=campaign_id)
            if previous_plan is not None:
                assert_append_only(previous_plan, plan)
            candidate = next_runtime_batch_candidate(
                plan=plan,
                campaign_root=campaign_root,
            )
            if candidate is not None:
                if not supervisor.planned_candidate_within_policy_envelope(candidate):
                    raise RuntimeError(
                        "next Codex batch candidate exceeds the current selection-policy "
                        f"envelope (evidence tier {supervisor.current_search_tier.value})"
                    )
                return candidate, plan
            return None, plan
            if announced_revision != plan.revision:
                announced_revision = plan.revision
                print(
                    json.dumps(
                        {
                            "waiting_for_codex_batch": True,
                            "plan_revision": plan.revision,
                            "attempted_parameter_sets": len(
                                supervisor.attempted_candidate_uids
                                | coordinator.terminal_candidate_uids
                            ),
                            "current_search_tier": supervisor.current_search_tier.value,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            previous_plan = plan
        row = _latest_complete_row(bridge_csv)
        if _integer(row, "ur_safety_mode") != 1:
            raise RuntimeError("UR Safety left NORMAL while waiting for a Codex batch")
        if tp_snapshot_from_bridge_row(row).state not in {
            "READY_HOME",
            "READY_NEAR",
            "READY_HOME_CLOSED",
        }:
            raise RuntimeError("TP left a typed ready state while waiting for a Codex batch")
        time.sleep(0.25)
    raise TimeoutError("timed out waiting for the next Codex five-candidate batch")


def _v3_stop_requested(
    stop_latch: Path | None,
    *,
    campaign_root: Path,
) -> bool:
    if stop_latch is None:
        return False
    expected = (campaign_root / "control" / "stop_after_current.json").absolute()
    if stop_latch.expanduser().absolute() != expected:
        raise RuntimeError("v3 stop latch must use campaign_root/control/stop_after_current.json")
    from step5d_autotune_v3.state import CampaignPaths, read_stop_latch

    return read_stop_latch(CampaignPaths(campaign_root))["armed"]


def _v3_overlay_for_candidate(
    path: Path | None,
    *,
    candidate: ForceCandidate,
    profile: ExecutionProfile,
    plan_revision: int | None,
    launch_profile_path: Path | None,
) -> Mapping[str, Any] | None:
    """Resolve one append-only V3 overlay immediately before READY_HOME ARM."""

    if path is None:
        return None
    if plan_revision is None or launch_profile_path is None:
        raise RuntimeError("V3 overlays require a candidate-plan revision and launch profile")
    from step5d_autotune_v3.runtime_profile import (
        load_launch_profile,
        normalize_trial_overlay,
    )
    from step5d_autotune_v3.state import read_strict_json

    overlay_path = path.expanduser().absolute()
    payload = read_strict_json(overlay_path, role="v3 trial overlay plan")
    launch_profile = load_launch_profile(launch_profile_path.expanduser().absolute())
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema")
        not in {
            "step5d.autotune-v3/trial-overlay-plan-v1",
            "step5d.autotune-v3/trial-overlay-plan-v2",
        }
        or payload.get("revision") != plan_revision
        or payload.get("launch_profile_fingerprint") != launch_profile.fingerprint
        or not isinstance(payload.get("batches"), list)
    ):
        raise RuntimeError("V3 trial-overlay plan is not bound to the selected plan/profile")
    matches = [
        row
        for batch in payload["batches"]
        if isinstance(batch, Mapping) and isinstance(batch.get("trials"), list)
        for row in batch["trials"]
        if isinstance(row, Mapping)
        and row.get(
            "transport_candidate_uid",
            row.get("candidate_uid"),
        )
        == candidate.candidate_uid
    ]
    if len(matches) != 1:
        raise RuntimeError("V3 candidate must have exactly one trial overlay")
    overlay = normalize_trial_overlay(matches[0].get("overlay"), profile=launch_profile)
    expected = {"execution_profile_id": profile.profile_id}
    if any(overlay[name] != value for name, value in expected.items()):
        raise RuntimeError("V3 overlay identity differs from the selected trial")
    return overlay


def run(args: argparse.Namespace) -> int:
    root = args.experiment_root.resolve()
    bridge_run = args.bridge_run.resolve()
    mailbox_path = args.mailbox.resolve()
    forced_values = (args.force_p, args.force_i, args.force_damping)
    if any(value is not None for value in forced_values) and not all(
        value is not None for value in forced_values
    ):
        raise RuntimeError("--force-p, --force-i, and --force-damping must be supplied together")
    forced_candidate = (
        None
        if forced_values[0] is None
        else ForceCandidate(
            force_p_gain=forced_values[0],
            force_i_gain=forced_values[1],
            force_damping=forced_values[2],
        )
    )
    bridge_csv = bridge_run / "bridge_rtde_500hz.csv"
    ready = json.loads((bridge_run / "bridge_ready.json").read_text(encoding="utf-8"))
    if any(
        (
            ready.get("ok") is not True,
            ready.get("bridge_profile") != "step5d_strict_rnn_autotune_v1",
            ready.get("rtde_send_succeeded") is not True,
            ready.get("sensor_stream_ready") is not True,
            ready.get("prewarm_status") != "ok",
        )
    ):
        raise RuntimeError("bridge readiness is not live-complete")
    ensure_mailbox_parent(
        mailbox_path,
        bridge_run,
        v3_runtime_root=args.v3_runtime_root,
    )
    campaign_root = args.campaign_root.resolve()
    if campaign_root == bridge_run or bridge_run in campaign_root.parents:
        raise RuntimeError("campaign root must be independent from the bridge run")
    if campaign_root.is_symlink():
        raise RuntimeError("campaign root must not be a symlink")
    campaign_root.mkdir(parents=True, exist_ok=True)
    stop_requested = lambda: _v3_stop_requested(
        args.v3_stop_latch,
        campaign_root=campaign_root,
    )
    derived_postprocess = None
    if args.v3_derived_postprocess_root is not None:
        postprocess_root = args.v3_derived_postprocess_root.expanduser().absolute()
        expected_postprocess = (campaign_root / "postprocess").absolute()
        if postprocess_root != expected_postprocess:
            raise RuntimeError("v3 derived postprocess must use campaign_root/postprocess")
        from step5d_autotune_v3.postprocess import DerivedPostprocessQueue

        derived_postprocess = DerivedPostprocessQueue(
            postprocess_root,
            allowed_capture_root=campaign_root,
        )
    plan_path: Path | None = None
    if args.selection_policy == "codex_batches":
        if args.candidate_plan is None:
            raise RuntimeError("codex_batches requires --candidate-plan")
        plan_path = args.candidate_plan.resolve()
        expected_plan = campaign_root / "control" / "candidate_plan.json"
        if plan_path != expected_plan or plan_path.is_symlink():
            raise RuntimeError("candidate plan must use campaign_root/control/candidate_plan.json")
    initial_row = _latest_complete_row(bridge_csv)
    initial = tp_snapshot_from_bridge_row(initial_row)
    if _integer(initial_row, "ur_safety_mode") != 1:
        raise RuntimeError("bridge row does not report UR Safety NORMAL")

    backend = Step5dV35Backend(root)
    frozen = backend.freeze_fingerprint()
    epoch_chain = discover_campaign_epochs(campaign_root)
    if epoch_chain and args.campaign_epoch < epoch_chain[-1].epoch:
        raise RuntimeError("campaign epoch cannot regress behind the latest durable epoch")
    existing_layout = next(
        (row for row in epoch_chain if row.epoch == args.campaign_epoch),
        None,
    )
    if existing_layout is not None:
        retained_policy = existing_layout.manifest.get(
            "selection_policy", "adaptive"
        )
        if retained_policy != args.selection_policy:
            raise RuntimeError(
                "selection policy differs from the durable campaign epoch"
            )
    prior_layout = next(
        (row for row in reversed(epoch_chain) if row.epoch < args.campaign_epoch),
        None,
    )
    if args.legacy_campaign_root is not None:
        if epoch_chain:
            raise RuntimeError("legacy campaign root is allowed only for first adoption")
        prior_layout = select_campaign_epoch(
            args.legacy_campaign_root.resolve(),
            campaign_epoch=args.legacy_campaign_epoch,
        )
        if args.campaign_epoch <= prior_layout.epoch:
            raise RuntimeError("adopted campaign epoch must advance beyond legacy epoch")
    campaign = _campaign_spec(
        root,
        frozen.composite_fingerprint,
        args.campaign_epoch,
        campaign_id=(
            prior_layout.campaign.campaign_id
            if not epoch_chain and prior_layout is not None
            else None if not epoch_chain else epoch_chain[-1].campaign.campaign_id
        ),
    )
    if args.campaign_binding is not None:
        authorization = _campaign_binding(
            args.campaign_binding.resolve(),
            campaign=campaign,
            campaign_fingerprint=frozen.composite_fingerprint,
        )
    elif args.authorization_file is not None:
        authorization = _campaign_authorization(
            args.authorization_file.resolve(),
            campaign=campaign,
            campaign_fingerprint=frozen.composite_fingerprint,
        )
    else:
        raise RuntimeError("internal campaign binding is required")
    preflight = backend.preflight(offline=False, authorization=authorization)
    if not preflight.ok:
        raise RuntimeError("live backend preflight failed: " + ";".join(preflight.blockers))
    follower = BridgeCsvFollower(bridge_csv)
    ready_states = {"READY_HOME", "READY_NEAR", "READY_HOME_CLOSED"}
    if initial.state not in ready_states:
        if not args.wait_for_home:
            follower.close()
            raise RuntimeError(f"TP must start at READY_HOME, got {initial.state}")
        for row in follower.rows(timeout_s=args.home_timeout_s):
            if _integer(row, "ur_safety_mode") != 1:
                follower.close()
                raise RuntimeError("UR Safety left NORMAL while waiting for READY_HOME")
            candidate = tp_snapshot_from_bridge_row(row)
            if candidate.state in ready_states:
                initial = candidate
                break
    if args.preflight_only:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "preflight_only",
                    "bridge_run": str(bridge_run),
                    "mailbox": str(mailbox_path),
                    "campaign_fingerprint": frozen.composite_fingerprint,
                    "tp_state": initial.state,
                    "cuda_available": preflight.cuda_available,
                },
                sort_keys=True,
            )
        )
        follower.close()
        return 0

    epoch_root = (
        existing_layout.root
        if existing_layout is not None
        else campaign_root
        if not epoch_chain
        else campaign_root / "epochs" / f"{campaign.campaign_epoch:010d}"
    )
    store = CampaignStore(epoch_root / "store")
    current_profile = _profile(root)
    if existing_layout is not None:
        store.initialize(dict(existing_layout.manifest))
    else:
        parent_epoch: dict[str, Any] | None = None
        if prior_layout is not None:
            parent_journal = SupervisorJournal(prior_layout.journal_root).load_latest()
            parent_epoch = {
                "campaign_epoch": prior_layout.epoch,
                "journal_revision": parent_journal.revision,
                "journal_record_sha256": parent_journal.record_sha256,
                "root": str(prior_layout.root),
                "store_manifest_sha256": _sha256_path(
                    prior_layout.store_root / "campaign.json"
                ),
            }
        store.initialize({
            "schema_version": "step5d.autotune.campaign-run/v1",
            "campaign": asdict(campaign),
            "frozen_fingerprint": backend.frozen_payload(frozen),
            "execution_profile": current_profile.payload(),
            "selection_policy": args.selection_policy,
            "parent_epoch": parent_epoch,
        })
    journal = SupervisorJournal(epoch_root / "journal")
    batch_trial_brief_admissions = recover_runtime_batch_trial_briefs(
        campaign_root=epoch_root
    )
    migrating = bool(
        prior_layout is not None
        and existing_layout is None
        and not (epoch_root / "journal" / "latest.json").exists()
    )
    if migrating:
        assert prior_layout is not None
        old_frozen = prior_layout.manifest["frozen_fingerprint"]
        old_supervisor = CampaignSupervisor(
            campaign=prior_layout.campaign,
            backend_id=old_frozen["backend_id"],
            source_fingerprint=old_frozen["source_fingerprint"],
            config_fingerprint=old_frozen["config_fingerprint"],
            execution_profile=profile_from_epoch(prior_layout),
            selection_policy=args.selection_policy,
        )
        old_journal = SupervisorJournal(prior_layout.journal_root)
        old_latest = old_journal.load_latest()
        old_restored = CampaignCoordinator.restore(
            supervisor=old_supervisor,
            journal=old_journal,
            latest=old_latest,
            resume_history=CampaignStore(prior_layout.store_root).read_resume_history(),
            promotion_history=CampaignStore(prior_layout.store_root).read_promotion_history(),
            prior_resume_history=_prior_resume_history(prior_layout.manifest),
            tp_snapshot=initial,
            mailbox_observation=_mailbox_observation_for_latest(old_latest),
            defer_reconcile=True,
        )
        settled = _settle_home_after_restart(
            old_restored.coordinator,
            initial,
            persist=False,
            recover_infra_aborted_active=args.recover_infra_aborted_active,
            trial_brief_admissions=batch_trial_brief_admissions,
        )
        if settled is not ReconcileAction.RESUME_HOME:
            raise RuntimeError(
                "prior code epoch is not safely at Home: "
                f"{settled.value}: "
                f"{old_restored.decision.reason}"
            )
        coordinator = old_restored.coordinator.resume_after_code_change(
            new_journal=journal,
            campaign=campaign,
            source_fingerprint=frozen.source_fingerprint,
            config_fingerprint=frozen.config_fingerprint,
        )
        supervisor = coordinator.supervisor
        resumed = True
    else:
        supervisor = CampaignSupervisor(
            campaign=campaign,
            backend_id=frozen.backend_id,
            source_fingerprint=frozen.source_fingerprint,
            config_fingerprint=frozen.config_fingerprint,
            execution_profile=_profile(root),
            selection_policy=args.selection_policy,
        )
        try:
            latest = journal.load_latest()
        except JournalIntegrityError as exc:
            if "missing" not in str(exc) and "no durable revision" not in str(exc):
                raise
            supervisor.seed_command_sequence_from_tp(
                initial.consumed_command_seq
            )
            coordinator = CampaignCoordinator(supervisor=supervisor, journal=journal)
            coordinator.persist_home()
            resumed = False
        else:
            restored = CampaignCoordinator.restore(
                supervisor=supervisor,
                journal=journal,
                latest=latest,
                resume_history=store.read_resume_history(),
                promotion_history=store.read_promotion_history(),
                prior_resume_history=_prior_resume_history(existing_layout.manifest),
                tp_snapshot=initial,
                mailbox_observation=_mailbox_observation_for_latest(latest),
                defer_reconcile=True,
            )
            settled = _settle_home_after_restart(
                restored.coordinator,
                initial,
                persist=True,
                recover_infra_aborted_active=args.recover_infra_aborted_active,
                trial_brief_admissions=batch_trial_brief_admissions,
            )
            if settled is not ReconcileAction.RESUME_HOME:
                raise RuntimeError(
                    "existing campaign cannot resume from READY_HOME: "
                    f"{settled.value}: {restored.decision.reason}"
                )
            coordinator = restored.coordinator
            resumed = True
    if args.runner_ready_file is not None:
        ready_path = args.runner_ready_file.resolve()
        if ready_path.parent != (bridge_run / "runtime").resolve():
            raise RuntimeError("runner ready file must belong to bridge runtime")
        _publish_runner_ready(
            ready_path,
            durable_state_ready=True,
            bridge_run=bridge_run,
            campaign_root=campaign_root,
            campaign=campaign,
            campaign_fingerprint=frozen.composite_fingerprint,
            selection_policy=args.selection_policy,
        )
    mailbox = AtomicCommandMailbox(mailbox_path, network_mode=True)
    event_path = campaign_root / "events.jsonl"
    campaign_root.mkdir(parents=True, exist_ok=True)
    _event(
        event_path,
        "campaign_resumed" if resumed else "campaign_ready",
        campaign_id=campaign.campaign_id,
        campaign_epoch=campaign.campaign_epoch,
        migrated_code_epoch=migrating,
    )

    completed_trials = 0
    plan_closed = False
    batch_completed = False
    stopped_after_current = False
    current_plan: CandidateBatchPlan | None = None
    try:
        while supervisor.phase is CampaignPhase.HOME:
            if stop_requested():
                stopped_after_current = True
                _event(event_path, "stop_after_current_observed", phase="home")
                break
            if args.selection_policy == "codex_batches":
                assert plan_path is not None
                try:
                    forced_candidate, current_plan = _wait_for_codex_candidate(
                        plan_path=plan_path,
                        campaign_id=campaign.campaign_id,
                        supervisor=supervisor,
                        coordinator=coordinator,
                        bridge_csv=bridge_csv,
                        campaign_root=epoch_root,
                        previous_plan=current_plan,
                        timeout_s=args.plan_wait_timeout_s,
                        stop_requested=stop_requested,
                    )
                except StopAfterCurrentRequested:
                    stopped_after_current = True
                    _event(event_path, "stop_after_current_observed", phase="ready_home")
                    break
                if forced_candidate is None:
                    batch_completed = runtime_batch_verified_complete(
                        campaign_root=epoch_root
                    )
                    plan_closed = not batch_completed and current_plan.closed
                    _event(
                        event_path,
                        (
                            "exact_ten_trial_batch_already_completed"
                            if batch_completed
                            else "codex_batch_plan_closed"
                        ),
                        plan_revision=current_plan.revision,
                    )
                    break
            plan_revision = None if current_plan is None else current_plan.revision
            batch_context: BatchAttemptContext | None = None
            home_path = bridge_run / "campaign_home_reference.json"
            home = CampaignHomeReference.load(home_path)
            if args.selection_policy == "codex_batches":
                if any(
                    value is None
                    for value in (
                        current_plan,
                        forced_candidate,
                        args.v3_trial_overlays,
                        args.v3_launch_profile,
                    )
                ):
                    raise RuntimeError(
                        "codex batch runtime requires exact plan/overlay/launch bindings"
                    )
                assert current_plan is not None
                assert forced_candidate is not None
                from step5d_autotune_v3.runtime_profile import load_launch_profile

                launch_profile = load_launch_profile(
                    args.v3_launch_profile.expanduser().absolute()
                )

                def overlay_for(candidate: ForceCandidate) -> Mapping[str, Any]:
                    overlay = _v3_overlay_for_candidate(
                        args.v3_trial_overlays,
                        candidate=candidate,
                        profile=supervisor.execution_profile,
                        plan_revision=plan_revision,
                        launch_profile_path=args.v3_launch_profile,
                    )
                    if overlay is None:
                        raise RuntimeError("exact batch row lacks a V3 trial overlay")
                    return overlay

                batch_context = prepare_batch_attempt_context(
                    plan=current_plan,
                    selected_candidate=forced_candidate,
                    profile=supervisor.execution_profile,
                    overlay_resolver=overlay_for,
                    campaign_uid=campaign.campaign_id,
                    experiment_fingerprint=frozen.composite_fingerprint,
                    launch_fingerprint=launch_profile.fingerprint,
                    controller_readback_fingerprint=(
                        frozen.controller_readback_manifest_sha256
                    ),
                    authorization_ref_sha256=(
                        authorization.authorization_ref_sha256
                    ),
                    stopping_bound_fingerprint=None,
                    plant_epoch=supervisor.plant_epoch,
                    campaign_root=epoch_root,
                    campaign_home_pose=home.home_pose,
                )
            arm = coordinator.issue_arm(
                store,
                provenance_run_dir=bridge_run,
                require_cuda_botorch=args.selection_policy == "adaptive",
                cuda_fit_mode="serial",
                forced_candidate=forced_candidate,
                allow_archived_code_fix_replay=(
                    current_plan is not None
                    and forced_candidate is not None
                    and forced_candidate.candidate_uid
                    == current_plan.code_fix_replay_candidate_uid
                ),
                allow_exact_incomplete_batch_retry=(
                    batch_context is not None
                    and batch_context.retrying_incomplete
                ),
                attempt_started=(
                    None
                    if batch_context is None
                    else lambda selected_trial: batch_context.start_attempt(
                        selected_trial,
                        batch_context.expected_row.trial_overlay,
                    )
                ),
            )
            if args.selection_policy == "codex_batches":
                _event(
                    event_path,
                    "codex_batch_candidate_selected",
                    plan_revision=plan_revision,
                    candidate=supervisor.active_trial.candidate.payload(),
                )
            forced_candidate = None
            trial = supervisor.active_trial
            if trial is None:
                raise RuntimeError("coordinator issued ARM without an active trial")
            prepared = backend.prepare_trial(trial, frozen)
            trial_overlay = _v3_overlay_for_candidate(
                args.v3_trial_overlays,
                candidate=trial.candidate,
                profile=trial.execution_profile,
                plan_revision=plan_revision,
                launch_profile_path=args.v3_launch_profile,
            )
            if trial_overlay is not None:
                prepared = replace(prepared, trial_overlay=trial_overlay)
            if batch_context is not None and (
                trial_overlay is None
                or dict(trial_overlay)
                != dict(batch_context.expected_row.trial_overlay)
            ):
                raise RuntimeError(
                    "prepared trial overlay differs from durable BatchIdentity"
                )
            if batch_context is not None:
                prepared = replace(
                    prepared,
                    batch_row_index=batch_context.row_index,
                )
            coordinator.dispatch(arm, prepared_trial=prepared, sink=mailbox)
            _event(
                event_path,
                "arm_dispatched",
                trial_uid=trial.trial_uid,
                trial_id=trial.trial_id,
                candidate=trial.candidate.payload(),
                profile=trial.execution_profile.payload(),
                plan_revision=plan_revision,
            )

            collector: HostClosureCollector | PreAckTypedClosureCollector | None = None
            for row in follower.rows(timeout_s=args.trial_timeout_s):
                snapshot = tp_snapshot_from_bridge_row(row)
                if snapshot.state != "WAIT_ACK":
                    continue
                if collector is None:
                    collector = (
                        HostClosureCollector(
                            expected_arm=arm,
                            home_reference=home,
                        )
                        if batch_context is None
                        else PreAckTypedClosureCollector(
                            context=batch_context,
                            trial=trial,
                            expected_arm=arm,
                            campaign_home_reference=home,
                        )
                    )
                sample = (
                    closure_sample_from_bridge_row(row)
                    if isinstance(collector, HostClosureCollector)
                    else row
                )
                collector.observe(
                    sample,
                    monotonic_s=_finite(row, "t_monotonic_s"),
                )
                if collector.ready:
                    break
            if collector is None or not collector.ready:
                raise RuntimeError("WAIT_ACK did not produce a complete safe closure")

            trial_capture_root = (bridge_run / "autotune_trials").resolve()
            if trial_overlay is not None:
                _wait_for_async_capture(
                    trial_capture_root / trial.trial_uid / "capture.csv"
                )
            producer = TrialArtifactProducer(trial_capture_root, trial)
            result = finalize_produced_bundle_and_dispatch_ack(
                collector=collector,
                producer=producer,
                backend=backend,
                store=store,
                coordinator=coordinator,
                prepared_trial=prepared,
                command_sink=mailbox,
                bundle_committed=(
                    None
                    if batch_context is None
                    else batch_context.record_bundle
                ),
            )
            _event(
                event_path,
                "bundle_closed_ack_dispatched",
                trial_uid=trial.trial_uid,
                disposition=result.evaluation.disposition.value,
                eligible=result.evaluation.eligible,
                objective_mae_n=result.evaluation.objective_mae_n,
            )
            if result.ack_packet is None:
                break

            if batch_context is None:
                for row in follower.rows(timeout_s=args.ack_timeout_s):
                    snapshot = tp_snapshot_from_bridge_row(row)
                    if snapshot.consumed_command_seq != result.ack_packet.command_seq:
                        continue
                    if snapshot.state not in {
                        "READY_HOME",
                        "WAIT_INFRA_READY",
                        "FAULT",
                    }:
                        continue
                    coordinator.reconcile(snapshot)
                    break
            else:
                post_ack_collector = PostAckClosureCollector(
                    context=batch_context,
                    trial=trial,
                    ack_packet=result.ack_packet,
                )
                post_ack_snapshot: TpSnapshot | None = None
                for row in follower.rows(timeout_s=args.ack_timeout_s):
                    snapshot = tp_snapshot_from_bridge_row(row)
                    if snapshot.consumed_command_seq != result.ack_packet.command_seq:
                        continue
                    if post_ack_collector.observe(
                        row,
                        monotonic_s=_finite(row, "t_monotonic_s"),
                    ):
                        post_ack_snapshot = snapshot
                        break
                if post_ack_snapshot is None:
                    raise RuntimeError(
                        "exact ACK lacked a complete typed post-ACK safe closure"
                    )
                admission = batch_context.complete_post_ack(
                    trial=trial,
                    arm_packet=arm,
                    ack_packet=result.ack_packet,
                    store_receipt=result.store_receipt,
                    manifest=result.manifest,
                    evaluation=result.evaluation,
                    readback=post_ack_collector.finalize(),
                )
                coordinator.reconcile(
                    post_ack_snapshot,
                    trial_brief_admission=admission,
                )
                if batch_context.journal.state().complete:
                    batch_result = batch_context.journal.finalize()
                    if batch_context.journal.verified_exit_code() != 0:
                        raise RuntimeError("durable BatchResult exit code differs")
                    batch_completed = True
                    _event(
                        event_path,
                        "exact_ten_trial_batch_completed",
                        batch_uid=batch_context.identity.batch_uid,
                        batch_result_uid=batch_result["batch_result_uid"],
                    )
            _event(event_path, "post_ack", phase=supervisor.phase.value)
            completed_trials += 1
            if derived_postprocess is not None:
                job_id = derived_postprocess.submit(
                    capture=result.immutable_bundle_path,
                    trial_id=trial.trial_uid,
                )
                _event(
                    event_path,
                    "derived_postprocess_queued",
                    trial_uid=trial.trial_uid,
                    job_id=job_id,
                )
            else:
                plot_command = [
                    sys.executable,
                    str(root / "tools" / "publish_step5d_autotune_plot.py"),
                    str(result.immutable_bundle_path),
                    "--output-dir",
                    str(campaign_root / "plots"),
                ]
                try:
                    published = subprocess.run(
                        plot_command,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=90.0,
                    )
                    _event(
                        event_path,
                        "trial_plot_published",
                        trial_uid=trial.trial_uid,
                        result=json.loads(published.stdout),
                    )
                except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
                    _event(
                        event_path,
                        "trial_plot_publish_failed",
                        trial_uid=trial.trial_uid,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            if args.one_trial:
                break
            if batch_completed:
                break
            if supervisor.phase is CampaignPhase.WAIT_INFRA_READY:
                break
            if (
                args.selection_policy == "adaptive"
                and supervisor.phase is CampaignPhase.HOME
            ):
                recovery = supervisor.recovery_snapshot()
                probe = recovery.governor_probe
                probe_closed = bool(
                    probe is not None
                    and (
                        probe.stage == "b" and probe.identity_b is not None
                        or probe.stage == "a_prime"
                        and probe.identity_a_prime is not None
                    )
                )
                if probe_closed:
                    evidence = supervisor.build_governor_evidence()
                    governor_decision = coordinator.complete_governor_probe(
                        evidence
                    )
                    _event(
                        event_path,
                        "governor_probe_completed",
                        decision=asdict(governor_decision),
                    )
                elif (
                    not result.close_decision.same_candidate_retry_pending
                    and (
                        result.evaluation.eligible
                        or result.close_decision.reason
                        in {
                            "governor_profile_diagnostic_ready",
                            "governor_a_prime_profile_diagnostic_closed",
                        }
                    )
                ):
                    proposed_profile, governor_decision = (
                        coordinator.begin_governor_probe()
                    )
                    _event(
                        event_path,
                        "governor_probe_considered",
                        proposed_profile=(
                            None
                            if proposed_profile is None
                            else proposed_profile.payload()
                        ),
                        decision=asdict(governor_decision),
                    )
    finally:
        follower.close()

    one_trial_complete = bool(
        args.one_trial
        and completed_trials == 1
        and supervisor.phase is CampaignPhase.HOME
    )
    campaign_succeeded = supervisor.phase is CampaignPhase.SUCCEEDED
    campaign_terminal = supervisor.phase in TERMINAL_PHASES
    _event(
        event_path,
        "campaign_terminal" if campaign_terminal else "runner_stopped",
        phase=supervisor.phase.value,
        trial_completed=completed_trials > 0,
        one_trial_limit_reached=one_trial_complete,
        campaign_succeeded=campaign_succeeded,
        plan_closed=plan_closed,
        batch_completed=batch_completed,
    )
    print(
        json.dumps(
            {
                "run_ok": (
                    campaign_succeeded
                    or one_trial_complete
                    or plan_closed
                    or batch_completed
                    or stopped_after_current
                ),
                "trial_completed": completed_trials > 0,
                "campaign_succeeded": campaign_succeeded,
                "campaign_terminal": campaign_terminal,
                "plan_closed": plan_closed,
                "batch_completed": batch_completed,
                "stopped_after_current": stopped_after_current,
                "selection_policy": args.selection_policy,
                "phase": supervisor.phase.value,
                "campaign_root": str(campaign_root),
            },
            sort_keys=True,
        )
    )
    return 0 if (
        campaign_succeeded
        or one_trial_complete
        or plan_closed
        or batch_completed
        or stopped_after_current
    ) else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--legacy-campaign-root", type=Path)
    parser.add_argument("--legacy-campaign-epoch", type=int)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--runner-ready-file", type=Path)
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--campaign-binding", type=Path)
    parser.add_argument("--campaign-epoch", type=int, default=1)
    parser.add_argument(
        "--selection-policy",
        choices=("adaptive", "codex_batches"),
        default="adaptive",
    )
    parser.add_argument("--candidate-plan", type=Path)
    parser.add_argument("--plan-wait-timeout-s", type=float, default=86400.0)
    parser.add_argument("--wait-for-home", action="store_true")
    parser.add_argument("--home-timeout-s", type=float, default=90.0)
    parser.add_argument("--recover-infra-aborted-active", action="store_true")
    parser.add_argument("--trial-timeout-s", type=float, default=180.0)
    parser.add_argument("--ack-timeout-s", type=float, default=10.0)
    parser.add_argument("--force-p", type=float)
    parser.add_argument("--force-i", type=float)
    parser.add_argument("--force-damping", type=float)
    parser.add_argument("--one-trial", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--v3-stop-latch", type=Path)
    parser.add_argument("--v3-derived-postprocess-root", type=Path)
    parser.add_argument("--v3-trial-overlays", type=Path)
    parser.add_argument("--v3-launch-profile", type=Path)
    parser.add_argument("--v3-runtime-root", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
