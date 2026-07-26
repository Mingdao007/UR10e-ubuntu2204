"""Fast, no-network/no-trial release contract checks for active V3."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .release_certificate import (
    REFERENCE_SCHEMA,
    ReleaseCertificateError,
    certificate_path,
    load_release_certificate,
    release_certificate_scope,
    sha256_file as certificate_sha256_file,
    write_release_certificate,
)
from .runtime_installation import load_runtime_pointer_identity, runtime_epoch


CANONICAL_LAUNCH_ENV = "STEP5D_V3_CANONICAL_LAUNCHER"
RESULT_SCHEMA = "step5d.autotune-v3/release-contract-result-v1"
EVIDENCE_SCHEMA = "step5d.autotune-v3/release-contract-evidence-ref-v1"
PROFILE = "state_machine_contract_v1"
PROVEN = "RELEASE_CONTRACT_PROVEN"
MISSING = "RELEASE_CONTRACT_CERTIFICATE_MISSING"
CORE_BUDGET_S = 1.0
COLD_BUDGET_S = 3.0
_SHA256 = frozenset("0123456789abcdef")
_PROCESS_ROLE_PATHS = {
    "canonical_launcher": "scripts/step5d-autotune-v3.sh",
    "launcher_supervisor": "tools/run_step5d_autotune_v3_live.py",
    "bridge_wrapper": "tools/run_step5d_autotune_v3_bridge.py",
    "campaign_runner": "tools/run_step5d_parameter_campaign.py",
}
_PROCESS_ROLE_PROFILES = {
    "canonical_launcher": None,
    "launcher_supervisor": "control",
    "bridge_wrapper": "control",
    "campaign_runner": "control",
}


class ReleaseContractError(ValueError):
    """Release identity or pure state-machine evidence is invalid."""


class ReleaseContractBlocked(ReleaseContractError):
    """A required immutable certificate is absent."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReleaseContractError(f"release contract is not canonical JSON: {exc}") from exc


def _sha256_bytes(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseContractError(f"{role} is missing or unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or not set(value) <= _SHA256
    ):
        raise ReleaseContractError(f"{role} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseContractError(f"{role} must be a positive integer")
    return value


def require_canonical_launcher(
    experiment_root: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    root = Path(experiment_root).resolve(strict=True)
    launcher = root / "scripts/step5d-autotune-v3.sh"
    if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ReleaseContractError("canonical Step5d launcher is unavailable")
    values = os.environ if environment is None else environment
    if values.get(CANONICAL_LAUNCH_ENV) != str(launcher):
        raise ReleaseContractError(
            "release contract worker is internal; use "
            "step5d-autotune-v3.sh release-contract-check"
        )
    return launcher


def _source_binding(root: Path, release: Any) -> dict[str, str]:
    manifest_path = root / release.manifest_path
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ReleaseContractError("release manifest is missing or unsafe")
    immutable_root = manifest_path.resolve(strict=True).parent
    actual: dict[str, str] = {}
    files: dict[str, str] = {}
    for relative, expected in sorted(release.source_fingerprints.items()):
        path = immutable_root / relative
        if not path.exists():
            path = root / relative
        observed = _sha256_file(path, f"release source {relative}")
        if observed != expected:
            raise ReleaseContractError(f"release source fingerprint differs: {relative}")
        files[relative] = observed
        actual[f"experiment:{relative}"] = observed
    verification = getattr(release, "verification", {})
    if isinstance(verification, Mapping):
        repository = root
        depth = verification.get("repository_source_root_depth", 0)
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ReleaseContractError("repository source root depth is invalid")
        for _ in range(depth):
            repository = repository.parent
        sources = verification.get("repository_source_fingerprints", {})
        if not isinstance(sources, Mapping):
            raise ReleaseContractError("repository source fingerprints are invalid")
        for relative, expected in sorted(sources.items()):
            observed = _sha256_file(
                repository / relative,
                f"repository source {relative}",
            )
            if observed != expected:
                raise ReleaseContractError(
                    f"repository source fingerprint differs: {relative}"
                )
            actual[f"repository:{relative}"] = observed
    if not files:
        raise ReleaseContractError("release source closure is empty")
    return {
        "source_fingerprint": _sha256_bytes(_canonical_bytes(actual)),
        "source_files_fingerprint": _sha256_bytes(_canonical_bytes(files)),
    }


def _control_environment_sha256(pointer: Mapping[str, Any]) -> str:
    profiles = pointer.get("profiles")
    control = profiles.get("control") if isinstance(profiles, Mapping) else None
    if not isinstance(control, Mapping):
        raise ReleaseContractError("control runtime identity is missing")
    payload = {
        "environment_id": _require_sha256(
            control.get("environment_id"), "control environment ID"
        ),
        "record_tree_sha256": _require_sha256(
            control.get("record_tree_sha256"), "control RECORD tree SHA-256"
        ),
        "profile_tree_sha256": _require_sha256(
            control.get("profile_tree_sha256"), "control profile tree SHA-256"
        ),
    }
    return _sha256_bytes(_canonical_bytes(payload))


def _v3_release(root: Path, release_identity: Any | None) -> Any:
    from .release_identity import (
        ReleaseIdentityError,
        load_current_release,
        load_release_manifest,
    )

    try:
        if release_identity is None:
            return load_current_release(root)
        return load_release_manifest(
            root,
            root / release_identity.manifest_path,
            expected_manifest_sha256=release_identity.manifest_sha256,
        )
    except ReleaseIdentityError as exc:
        raise ReleaseContractError(f"release identity is invalid: {exc}") from exc


def release_contract_scope_for_release(
    experiment_root: Path,
    release_identity: Any,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(experiment_root).resolve(strict=True)
    values = os.environ if environment is None else environment
    release = _v3_release(root, release_identity)
    source = _source_binding(root, release)
    pointer = load_runtime_pointer_identity(environ=values)
    return release_certificate_scope(
        subject_kind="autotune_v3",
        release_manifest_sha256=release.manifest_sha256,
        source_fingerprint=source["source_fingerprint"],
        source_files_fingerprint=source["source_files_fingerprint"],
        launcher_sha256=_sha256_file(
            root / "scripts/step5d-autotune-v3.sh", "canonical launcher"
        ),
        control_environment_sha256=_control_environment_sha256(pointer),
        runtime_epoch=runtime_epoch(pointer),
        contract_profile=PROFILE,
    )


def _transition_witness() -> dict[str, Any]:
    from step5d_autotune_contract import TrialDisposition
    from step5d_autotune_state_machine import (
        EXPECTED_SAFE_SEQUENCE,
        HostCommand,
        HostPacket,
        TpLoopState,
        TpPacket,
        classify_terminal_reason,
        packet_matches,
        verify_transcript,
    )

    arm1 = HostPacket(
        campaign_epoch=1,
        trial_id=1,
        command=HostCommand.ARM,
        candidate_token=101,
        execution_profile_id=633,
        command_seq=1,
        logical_batch_sequence=1,
    )

    def tp(state: TpLoopState, packet: HostPacket) -> TpPacket:
        return TpPacket(
            campaign_epoch_echo=packet.campaign_epoch,
            trial_id_echo=packet.trial_id,
            state=state,
            candidate_token_echo=packet.candidate_token,
            terminal_reason=1 if state is TpLoopState.WAIT_ACK else 0,
            execution_profile_id_echo=packet.execution_profile_id,
            consumed_command_seq=packet.command_seq,
            logical_batch_sequence_echo=packet.logical_batch_sequence,
        )

    transcript = tuple(tp(state, arm1) for state in EXPECTED_SAFE_SEQUENCE)
    transcript_ok, failures = verify_transcript(arm1, transcript)
    disposition = classify_terminal_reason(
        1,
        host_cause=None,
        safe_closure=True,
        eligible_evidence=False,
    )
    arm2 = HostPacket(
        campaign_epoch=1,
        trial_id=2,
        command=HostCommand.ARM,
        candidate_token=102,
        execution_profile_id=633,
        command_seq=3,
        logical_batch_sequence=1,
    )
    arm2_run = tp(TpLoopState.RUN, arm2)
    if (
        not transcript_ok
        or failures
        or disposition is not TrialDisposition.FAIL_CLOSED
        or not packet_matches(arm2, arm2_run)
        or packet_matches(arm1, arm2_run)
    ):
        raise ReleaseContractError("state-machine contract failed")
    rows = [
        {
            "state": packet.state.name,
            "command_seq": packet.consumed_command_seq,
            "trial_id": packet.trial_id_echo,
        }
        for packet in transcript
    ]
    return {
        "schema": "step5d.autotune-v3/release-contract-witness-v1",
        "arm1": {
            "trial_id": 1,
            "command_seq": 1,
            "transcript_sha256": _sha256_bytes(_canonical_bytes(rows)),
        },
        "close": {
            "safe": True,
            "optimizer_eligible": False,
            "disposition": disposition.value,
        },
        "arm2": {"trial_id": 2, "command_seq": 3, "state": arm2_run.state.name},
        "stale_arm1_replay_rejected": True,
    }


def validate_release_contract_result(
    payload: Mapping[str, Any],
    *,
    expected_scope: Mapping[str, Any],
) -> Mapping[str, Any]:
    fields = {
        "schema",
        "ok",
        "state",
        "scope",
        "witness",
        "invariants",
        "timing",
        "started_at_unix_ns",
        "completed_at_unix_ns",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != fields
        or payload.get("schema") != RESULT_SCHEMA
        or payload.get("ok") is not True
        or payload.get("state") != PROVEN
        or payload.get("scope") != dict(expected_scope)
        or payload.get("witness") != _transition_witness()
        or payload.get("invariants")
        != {
            "physical_trial": False,
            "network_started": False,
            "subprocess_started": False,
            "wall_clock_trial_wait": False,
        }
    ):
        raise ReleaseContractError("release contract result fields differ")
    started = _positive_int(payload["started_at_unix_ns"], "contract start")
    completed = _positive_int(payload["completed_at_unix_ns"], "contract completion")
    if completed < started:
        raise ReleaseContractError("contract completion precedes start")
    timing = payload["timing"]
    if not isinstance(timing, Mapping) or set(timing) != {
        "core_transition_s",
        "core_budget_s",
        "cold_budget_s",
    }:
        raise ReleaseContractError("release contract timing fields differ")
    for name in ("core_transition_s", "core_budget_s", "cold_budget_s"):
        value = timing[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReleaseContractError(f"release contract {name} is invalid")
        if not math.isfinite(float(value)) or float(value) <= 0.0:
            raise ReleaseContractError(f"release contract {name} is invalid")
    if (
        timing["core_budget_s"] != CORE_BUDGET_S
        or timing["cold_budget_s"] != COLD_BUDGET_S
        or float(timing["core_transition_s"]) > CORE_BUDGET_S
    ):
        raise ReleaseContractError("release contract timing budget exceeded")
    return payload["scope"]


def _write_evidence(root: Path, payload: Mapping[str, Any]) -> dict[str, str]:
    encoded = _canonical_bytes(dict(payload))
    digest = _sha256_bytes(encoded)
    path = root / "release-contract" / "evidence" / digest / "contract.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ReleaseContractError("release contract evidence conflicts")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return {"schema": EVIDENCE_SCHEMA, "path": str(path), "sha256": digest}


def _cached(
    output: Path,
    scope: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]] | None:
    path = certificate_path(output, scope)
    if not path.exists():
        return None
    try:
        _certificate, _evidence, payload = load_release_certificate(
            output,
            path,
            expected_scope=scope,
        )
    except ReleaseCertificateError as exc:
        raise ReleaseContractError(f"release contract certificate is invalid: {exc}") from exc
    validate_release_contract_result(payload, expected_scope=scope)
    return payload, {
        "schema": REFERENCE_SCHEMA,
        "path": str(path),
        "sha256": certificate_sha256_file(path, "release contract certificate"),
    }


def run_release_contract_check(
    experiment_root: Path,
    output_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    release_identity: Any | None = None,
    subject_kind: str = "autotune_v3",
    reuse_only: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    started_monotonic = time.monotonic()
    started_at = time.time_ns()
    root = Path(experiment_root).resolve(strict=True)
    values = os.environ if environment is None else environment
    require_canonical_launcher(root, values)
    if subject_kind != "autotune_v3":
        raise ReleaseContractError(f"unknown release contract subject: {subject_kind}")
    release = _v3_release(root, release_identity)
    scope = release_contract_scope_for_release(
        root,
        release,
        environment=values,
    )
    output = Path(output_root).resolve()
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise ReleaseContractError("release contract output root is unsafe")
    output.mkdir(parents=True, exist_ok=True)
    cached = _cached(output, scope)
    if cached is not None:
        return cached
    if reuse_only:
        raise ReleaseContractBlocked(MISSING)

    core_started = time.monotonic()
    witness = _transition_witness()
    core_elapsed = time.monotonic() - core_started
    if core_elapsed > CORE_BUDGET_S:
        raise ReleaseContractError("state-machine contract exceeded one second")
    payload = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "state": PROVEN,
        "scope": scope,
        "witness": witness,
        "invariants": {
            "physical_trial": False,
            "network_started": False,
            "subprocess_started": False,
            "wall_clock_trial_wait": False,
        },
        "timing": {
            "core_transition_s": core_elapsed,
            "core_budget_s": CORE_BUDGET_S,
            "cold_budget_s": COLD_BUDGET_S,
        },
        "started_at_unix_ns": started_at,
        "completed_at_unix_ns": time.time_ns(),
    }
    validate_release_contract_result(payload, expected_scope=scope)
    evidence = _write_evidence(output, payload)
    _certificate, reference = write_release_certificate(
        output,
        scope=scope,
        contract_evidence_path=Path(evidence["path"]),
        contract_evidence_sha256=evidence["sha256"],
        completed_at_unix_ns=payload["completed_at_unix_ns"],
    )
    if time.monotonic() - started_monotonic > COLD_BUDGET_S:
        raise ReleaseContractError("release contract cold path exceeded three seconds")
    return payload, reference


def production_process_role_paths() -> Mapping[str, str]:
    return dict(_PROCESS_ROLE_PATHS)


def production_process_tree_fingerprint(experiment_root: Path) -> str:
    root = Path(experiment_root).resolve(strict=True)
    rows = [
        {
            "role": role,
            "script": str((root / relative).resolve(strict=True)),
            "script_sha256": _sha256_file(root / relative, f"{role} script"),
            "runtime_profile": _PROCESS_ROLE_PROFILES[role],
        }
        for role, relative in sorted(_PROCESS_ROLE_PATHS.items())
    ]
    return _sha256_bytes(_canonical_bytes(rows))


def resolve_process_argv_paths(
    proc: Path,
    argv: Sequence[str],
) -> tuple[str | None, ...]:
    cwd: Path | None = None
    resolved: list[str | None] = []
    for argument in argv:
        if argument.startswith("-"):
            resolved.append(None)
            continue
        candidate = Path(argument)
        if not candidate.is_absolute():
            if cwd is None:
                cwd = (proc / "cwd").resolve(strict=True)
            candidate = cwd / candidate
        resolved.append(str(candidate.resolve(strict=False)))
    return tuple(resolved)


__all__ = [
    "CANONICAL_LAUNCH_ENV",
    "COLD_BUDGET_S",
    "CORE_BUDGET_S",
    "MISSING",
    "PROFILE",
    "PROVEN",
    "RESULT_SCHEMA",
    "ReleaseContractBlocked",
    "ReleaseContractError",
    "production_process_role_paths",
    "production_process_tree_fingerprint",
    "release_contract_scope_for_release",
    "require_canonical_launcher",
    "resolve_process_argv_paths",
    "run_release_contract_check",
    "validate_release_contract_result",
]
