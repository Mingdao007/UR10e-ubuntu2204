"""Concrete mailbox + JSONL bridge adapter for the v2 supervisor."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .mailbox import AtomicMailbox
from .model import canonical_json_bytes
from .repository import Repository
from .supervisor import (
    AnalysisArtifact,
    AnalysisResult,
    ArtifactSeal,
    RuntimeFailure,
    SafetyHalt,
)


EVENT_SCHEMA = "step5d.autotune.bridge-event/v2"
INT32_MAX = 2_147_483_647
EXECUTION_PROFILE_ID = 533
BACKEND_ID = "step5d_autotune_v2_live"


def _stable_int32(material: str) -> int:
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big") % INT32_MAX + 1


class JsonlBridgePort:
    """Transport adapter; it contains no trajectory or force-control logic."""

    def __init__(
        self,
        *,
        repository: Repository,
        mailbox: AtomicMailbox,
        event_path: Path,
        allowed_artifact_root: Path,
        deployment_id: str,
        analyzer_argv: Sequence[str],
        command_root: Path,
        transfer_destination: str | None,
        timeout_s: float = 120.0,
        health_check: Callable[[], None] | None = None,
    ) -> None:
        self.repository = repository
        self.mailbox = mailbox
        self.event_path = event_path.resolve()
        self.allowed_artifact_root = allowed_artifact_root.resolve()
        self.deployment_id = deployment_id
        self.profile_id = repository.deployment_profile_id(deployment_id)
        self.analyzer_argv = tuple(analyzer_argv)
        self.command_root = command_root.resolve()
        self.transfer_destination = transfer_destination
        self.timeout_s = timeout_s
        self.offset = 0
        self.health_check = health_check

    def _check_health(self) -> None:
        if self.health_check is None:
            return
        try:
            self.health_check()
        except Exception as exc:
            raise RuntimeFailure(
                f"service heartbeat is unhealthy: {type(exc).__name__}:{exc}"
            ) from exc

    def publish_arm(
        self, *, trial_id: str, sequence: int, candidate: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self._check_health()
        payload = self._arm_payload(trial_id=trial_id, candidate=candidate)
        checksum = self.mailbox.publish(
            sequence=sequence,
            payload=payload,
        )
        return {"sequence": sequence, "mailbox_checksum": checksum}

    def _arm_payload(
        self, *, trial_id: str, candidate: Mapping[str, Any]
    ) -> dict[str, Any]:
        if candidate.get("profile_id") != self.profile_id:
            raise RuntimeFailure(
                "candidate profile_id differs from the deployment profile"
            )
        return {
            "command": "ARM",
            "deployment_id": self.deployment_id,
            "trial_id": trial_id,
            "binding": self._runtime_binding(trial_id, candidate),
        }

    def _ack_payload(self, *, trial_id: str, artifact_sha256: str) -> dict[str, Any]:
        detail = self.repository.trial_detail(trial_id)
        candidate = self.repository.candidate(detail["candidate_id"])
        return {
            "command": "ACK_BUNDLE",
            "deployment_id": self.deployment_id,
            "trial_id": trial_id,
            "artifact_sha256": artifact_sha256,
            "binding": self._runtime_binding(trial_id, candidate),
        }

    def _runtime_binding(
        self, trial_id: str, candidate: Mapping[str, Any]
    ) -> dict[str, Any]:
        detail = self.repository.trial_detail(trial_id)
        deployment = self.repository.deployment_binding(self.deployment_id)
        if detail["deployment_id"] != self.deployment_id:
            raise RuntimeFailure("trial deployment differs from live adapter deployment")
        if candidate["profile_id"] != deployment["profile_id"]:
            raise RuntimeFailure("candidate profile differs from live adapter deployment")
        arm_sequence = int(detail["arm_sequence"])
        campaign_fingerprint = hashlib.sha256(
            canonical_json_bytes(
                {
                    "deployment_id": self.deployment_id,
                    "code_fingerprint": deployment["code_fingerprint"],
                    "tp_fingerprint": deployment["tp_fingerprint"],
                    "guard_fingerprint": deployment["guard_fingerprint"],
                    "profile": deployment["profile"],
                }
            )
        ).hexdigest()
        return {
            "trial_uid": trial_id,
            "backend_id": BACKEND_ID,
            "campaign_epoch": _stable_int32(self.deployment_id),
            "tp_trial_id": arm_sequence,
            "candidate_token": _stable_int32(candidate["comparison_key"]),
            "arm_command_seq": arm_sequence,
            "execution_profile_id": EXECUTION_PROFILE_ID,
            "candidate": {
                "group_id": candidate["group_id"],
                "target_force_n": "12",
                "p": candidate["p_text"],
                "i": candidate["i_text"],
                "d": candidate["d_text"],
                "profile_id": deployment["profile_id"],
                "comparison_key": candidate["comparison_key"],
                "purpose": candidate["purpose"],
            },
            "profile": deployment["profile"],
            "source_fingerprint": deployment["code_fingerprint"],
            "config_fingerprint": deployment["guard_fingerprint"],
            "campaign_fingerprint": campaign_fingerprint,
        }

    def _arm_binding(self, trial_id: str) -> tuple[int, str]:
        detail = self.repository.trial_detail(trial_id)
        candidate = self.repository.candidate(detail["candidate_id"])
        sequence = int(detail["arm_sequence"])
        checksum = self.mailbox.checksum_for(
            sequence=sequence,
            payload=self._arm_payload(trial_id=trial_id, candidate=candidate),
        )
        return sequence, checksum

    def _ack_binding(self, trial_id: str, artifact: ArtifactSeal) -> tuple[int, str]:
        detail = self.repository.trial_detail(trial_id)
        if detail["ack_sequence"] is None:
            raise RuntimeFailure("ready-home wait lacks a persisted ACK sequence")
        sequence = int(detail["ack_sequence"])
        checksum = self.mailbox.checksum_for(
            sequence=sequence,
            payload=self._ack_payload(
                trial_id=trial_id, artifact_sha256=artifact.sha256
            ),
        )
        return sequence, checksum

    def _wait(
        self,
        trial_id: str,
        expected: str,
        *,
        expected_sequence: int,
        expected_checksum: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            try:
                with self.event_path.open("r", encoding="utf-8") as handle:
                    handle.seek(self.offset)
                    line = handle.readline()
                    if line and line.endswith("\n"):
                        self.offset = handle.tell()
                    else:
                        line = ""
            except FileNotFoundError:
                line = ""
            if not line:
                self._check_health()
                time.sleep(0.02)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeFailure(f"bridge event JSON is invalid: {exc}") from exc
            if not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA:
                raise RuntimeFailure("bridge event schema mismatch")
            if event.get("deployment_id") != self.deployment_id:
                raise RuntimeFailure("bridge event deployment identity drift")
            if event.get("trial_id") != trial_id:
                continue
            event_type = event.get("event")
            if event_type == "safety_halt":
                arm_sequence, arm_checksum = self._arm_binding(trial_id)
                event_binding = (
                    event.get("command_sequence"),
                    event.get("command_checksum"),
                )
                if event_binding not in {
                    (expected_sequence, expected_checksum),
                    (arm_sequence, arm_checksum),
                }:
                    raise RuntimeFailure(
                        "bridge safety halt does not bind the persisted command"
                    )
                required_safety_fields = {
                    "reason",
                    "tp_stop_acknowledged",
                    "tp_state",
                    "runtime_state",
                    "normal_force_n",
                    "force_norm_n",
                    "sample_counter",
                    "stop_packets_sent",
                    "path",
                    "sha256",
                }
                if not required_safety_fields.issubset(event):
                    raise RuntimeFailure("bridge safety halt evidence is incomplete")
                if (
                    type(event["tp_stop_acknowledged"]) is not bool
                    or not isinstance(event["tp_state"], str)
                    or isinstance(event["sample_counter"], bool)
                    or not isinstance(event["sample_counter"], int)
                    or int(event["sample_counter"]) < 0
                    or isinstance(event["stop_packets_sent"], bool)
                    or not isinstance(event["stop_packets_sent"], int)
                    or int(event["stop_packets_sent"]) < 0
                ):
                    raise RuntimeFailure("bridge safety halt evidence types differ")
                artifact = self._seal_event_artifact(event, role="safety-halt")
                reason = str(event.get("reason") or "bridge_safety_halt")
                raise SafetyHalt(
                    f"bridge_safety_halt:{reason}",
                    artifact=artifact,
                    evidence={
                        key: event[key]
                        for key in (
                            "tp_stop_acknowledged",
                            "tp_state",
                            "runtime_state",
                            "normal_force_n",
                            "force_norm_n",
                            "sample_counter",
                            "stop_packets_sent",
                        )
                        if key in event
                    },
                )
            if event_type == expected:
                if (
                    event.get("command_sequence") != expected_sequence
                    or event.get("command_checksum") != expected_checksum
                ):
                    raise RuntimeFailure(
                        f"bridge event {expected} does not bind the persisted command"
                    )
                return event
        raise RuntimeFailure(f"timed out waiting for bridge event {expected}")

    def _seal_event_artifact(
        self, event: Mapping[str, Any], *, role: str
    ) -> ArtifactSeal:
        source_value = Path(str(event.get("path", "")))
        if not source_value.is_absolute():
            raise RuntimeFailure(f"{role} artifact path must be absolute")
        try:
            path = source_value.resolve(strict=True)
        except OSError as exc:
            raise RuntimeFailure(f"{role} artifact is missing") from exc
        if source_value != path:
            raise RuntimeFailure(f"{role} artifact path contains a symlink or traversal")
        try:
            path.relative_to(self.allowed_artifact_root)
        except ValueError as exc:
            raise RuntimeFailure(f"{role} artifact escapes the governed runtime root") from exc
        actual = _sha256(path)
        if actual != event.get("sha256"):
            raise RuntimeFailure(f"{role} artifact digest differs from bridge seal")
        sealed = _seal_immutable_copy(
            source=path,
            expected_sha256=actual,
            allowed_root=self.allowed_artifact_root,
        )
        return ArtifactSeal(path=sealed, sha256=actual)

    def wait_tp_consumed(self, *, trial_id: str) -> Mapping[str, Any]:
        sequence, checksum = self._arm_binding(trial_id)
        return self._wait(
            trial_id,
            "tp_consumed",
            expected_sequence=sequence,
            expected_checksum=checksum,
        )

    def wait_run_started(self, *, trial_id: str) -> Mapping[str, Any]:
        sequence, checksum = self._arm_binding(trial_id)
        return self._wait(
            trial_id,
            "run_started",
            expected_sequence=sequence,
            expected_checksum=checksum,
        )

    def wait_home_verified(self, *, trial_id: str) -> Mapping[str, Any]:
        sequence, checksum = self._arm_binding(trial_id)
        event = self._wait(
            trial_id,
            "home_verified",
            expected_sequence=sequence,
            expected_checksum=checksum,
        )
        if event.get("safe_home_verified") is not True:
            raise RuntimeFailure("bridge did not prove measured Home closure")
        return event

    def seal_raw(self, *, trial_id: str) -> ArtifactSeal:
        sequence, checksum = self._arm_binding(trial_id)
        event = self._wait(
            trial_id,
            "raw_capture_sealed",
            expected_sequence=sequence,
            expected_checksum=checksum,
        )
        return self._seal_event_artifact(event, role="raw")

    def publish_ack(
        self, *, trial_id: str, sequence: int, artifact: ArtifactSeal
    ) -> Mapping[str, Any]:
        self._check_health()
        payload = self._ack_payload(
            trial_id=trial_id, artifact_sha256=artifact.sha256
        )
        checksum = self.mailbox.publish(
            sequence=sequence,
            payload=payload,
        )
        return {"sequence": sequence, "mailbox_checksum": checksum}

    def wait_ready_home(self, *, trial_id: str) -> Mapping[str, Any]:
        artifact_row = self.repository.artifact_for_trial(
            trial_id, role="raw_capture_seal"
        )
        if artifact_row is None:
            raise RuntimeFailure("ready-home wait lacks an immutable raw artifact")
        artifact = ArtifactSeal(Path(artifact_row["path"]), artifact_row["sha256"])
        sequence, checksum = self._ack_binding(trial_id, artifact)
        event = self._wait(
            trial_id,
            "ready_home",
            expected_sequence=sequence,
            expected_checksum=checksum,
        )
        if (
            event.get("command_cleared") is not True
            or event.get("measured_home_verified") is not True
        ):
            raise RuntimeFailure(
                "ready-home event lacks command-clear and measured-Home proof"
            )
        return event

    def analyze(self, *, trial_id: str, artifact: ArtifactSeal) -> AnalysisResult:
        self._check_health()
        if _sha256(artifact.path) != artifact.sha256:
            raise RuntimeFailure("immutable raw artifact changed after ACK")
        if not self.analyzer_argv:
            raise RuntimeFailure("postprocess analyzer argv is not configured")
        argv = [
            token.replace("{capture}", str(artifact.path))
            .replace("{trial_id}", trial_id)
            .replace("{root}", str(self.command_root))
            .replace("{runtime_root}", str(self.allowed_artifact_root))
            for token in self.analyzer_argv
        ]
        completed = subprocess.run(
            argv,
            cwd=self.allowed_artifact_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=300.0,
        )
        self._check_health()
        if completed.returncode != 0:
            raise RuntimeFailure(
                f"analyzer failed rc={completed.returncode}: {completed.stderr[-500:]}"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeFailure("analyzer stdout is not JSON") from exc
        if (
            not isinstance(result, dict)
            or result.get("schema") != "step5d.autotune.analysis/v2"
            or result.get("trial_id") != trial_id
            or not isinstance(result.get("metrics"), dict)
            or not isinstance(result.get("artifacts"), list)
            or not isinstance(result.get("warnings"), list)
        ):
            raise RuntimeFailure("analyzer result lacks metrics")
        derived: list[AnalysisArtifact] = []
        for row in result["artifacts"]:
            if not isinstance(row, dict) or set(row) != {"role", "path", "sha256"}:
                raise RuntimeFailure("analyzer artifact schema differs")
            path = Path(str(row["path"])).resolve()
            try:
                path.relative_to(self.allowed_artifact_root)
            except ValueError as exc:
                raise RuntimeFailure("analyzer artifact escapes runtime root") from exc
            if not path.is_file() or path.is_symlink() or _sha256(path) != row["sha256"]:
                raise RuntimeFailure("analyzer artifact digest or file identity differs")
            derived.append(
                AnalysisArtifact(
                    role=str(row["role"]),
                    path=path,
                    sha256=str(row["sha256"]),
                    transfer_destination=(
                        self.transfer_destination
                        if row["role"] == "parameter_named_png"
                        else None
                    ),
                )
            )
        warnings = result["warnings"]
        if any(not isinstance(value, str) for value in warnings):
            raise RuntimeFailure("analyzer warnings must be strings")
        return AnalysisResult(
            metrics=result["metrics"],
            diagnostic_eligible=result.get("diagnostic_eligible") is True,
            objective_eligible=result.get("objective_eligible") is True,
            artifacts=tuple(derived),
            warnings=tuple(warnings),
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        raise RuntimeFailure(f"artifact is missing or unsafe: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeFailure("artifact must be a regular file without hard links")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeFailure("artifact changed while being hashed")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _seal_immutable_copy(
    *, source: Path, expected_sha256: str, allowed_root: Path
) -> Path:
    seal_root = allowed_root / "immutable_raw" / "sha256"
    seal_root.mkdir(parents=True, exist_ok=True)
    if seal_root.is_symlink() or seal_root.resolve() != seal_root:
        raise RuntimeFailure("immutable raw seal directory is unsafe")
    destination = seal_root / expected_sha256
    if destination.exists() or destination.is_symlink():
        if _sha256(destination) != expected_sha256:
            raise RuntimeFailure("content-addressed raw seal already differs")
        return destination
    temporary = seal_root / (
        f".{expected_sha256}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    source_descriptor = os.open(
        source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    target_descriptor = -1
    digest = hashlib.sha256()
    try:
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise RuntimeFailure("raw source must be a regular file without hard links")
        target_descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise RuntimeFailure("short immutable raw-seal write")
                view = view[written:]
        if digest.hexdigest() != expected_sha256:
            raise RuntimeFailure("raw artifact changed while being sealed")
        os.fsync(target_descriptor)
        os.fchmod(target_descriptor, 0o400)
        os.close(target_descriptor)
        target_descriptor = -1
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            if _sha256(destination) != expected_sha256:
                raise RuntimeFailure("concurrent immutable raw seal differs")
        temporary.unlink()
        directory = os.open(
            seal_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(source_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if _sha256(destination) != expected_sha256:
        raise RuntimeFailure("immutable raw seal verification failed")
    return destination
