"""Durable orchestration contracts for TacDiffusion formal V4 collection.

This module owns no robot or sensor socket.  It makes the live runner's state
transitions, retry accounting, and recovery policy explicit and independently
testable.  Every physical attempt gets a unique immutable directory and one
hash-chained terminal ledger record.  Failed attempts never consume an
eligible episode ordinal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .formal_campaign import (
    FORMAL_CAMPAIGN_EPISODE_COUNT,
    FormalCampaignContractV1,
    FormalCampaignEpisodeV1,
)


FORMAL_CONTACT_ACQUISITION_SCHEMA_V1 = (
    "ur10e_tacdiffusion_contact_acquisition/v1"
)
FORMAL_SENSOR_DELIVERY_WATCHDOG_S = 0.080
FORMAL_CAMPAIGN_LEDGER_SCHEMA_V1 = "ur10e_tacdiffusion_campaign_ledger/v1"
FORMAL_ATTEMPT_RECEIPT_SCHEMA_V1 = "ur10e_tacdiffusion_attempt_receipt/v1"
ZERO_SHA256 = "0" * 64


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _canonical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: object, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


class FormalAttemptPhase(str, Enum):
    BASELINE = "BASELINE"
    ACQUISITION = "ACQUISITION"
    CONTACT_SEARCH = "CONTACT_SEARCH"
    CONTACT_SETTLE = "CONTACT_SETTLE"
    TRACK = "TRACK"
    RETRACT = "RETRACT"
    HOME_RETURN = "HOME_RETURN"
    COMPLETE = "COMPLETE"
    FAULT = "FAULT"


class FormalAttemptOutcome(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    RECOVERABLE_FAILURE = "recoverable_failure"
    HARD_FAULT = "hard_fault"


@dataclass(frozen=True)
class ContactAcquisitionContractV1:
    """Frozen deterministic approach/contact/retract contract.

    Reaction is the environment-on-tool direction.  Approach is its negative.
    At the current downward-facing tool this corresponds to positive TCP Fz
    command and negative measured TCP Fz reaction, but no sign is inferred
    from a scalar field: both named vectors remain explicit.
    """

    baseline_samples: int = 1000
    native_sensor_rate_hz: int = 1000
    approach_speed_m_s: float = 0.0005
    maximum_search_distance_m: float = 0.025
    contact_latch_load_n: float = 1.0
    contact_latch_duration_s: float = 0.050
    acquisition_acceleration_m_s2: float = 0.010
    acquisition_deceleration_m_s2: float = 0.010
    sensor_delivery_watchdog_s: float = FORMAL_SENSOR_DELIVERY_WATCHDOG_S
    stationary_dwell_s: float = 0.100
    stationary_tcp_speed_limit_m_s: float = 0.0001
    stationary_rotation_speed_limit_rad_s: float = 0.002
    stationary_joint_speed_limit_rad_s: float = 0.001
    maximum_handoff_mismatch_m: float = 0.0003
    settle_duration_s: float = 0.500
    track_duration_s: float = 8.0
    retract_distance_m: float = 0.010
    reaction_normal_base: tuple[float, float, float] = (0.0, 0.0, 1.0)
    approach_normal_base: tuple[float, float, float] = (0.0, 0.0, -1.0)
    normal_load_definition: str = (
        "dot(environment_on_tool_reaction_base,reaction_normal_base)"
    )
    safe_auto_return_outcomes: tuple[str, ...] = (
        FormalAttemptOutcome.ELIGIBLE.value,
        FormalAttemptOutcome.INELIGIBLE.value,
        FormalAttemptOutcome.RECOVERABLE_FAILURE.value,
    )
    no_auto_return_fault_classes: tuple[str, ...] = (
        "sensor_fault",
        "force_guard",
        "torque_guard",
        "protective_stop",
        "safety_changed",
        "joint_fault",
        "route_identity_changed",
        "route_fault",
    )
    schema_version: str = FORMAL_CONTACT_ACQUISITION_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_CONTACT_ACQUISITION_SCHEMA_V1:
            raise ValueError("unsupported contact acquisition schema")
        if self.baseline_samples != 1000 or self.native_sensor_rate_hz != 1000:
            raise ValueError("contact acquisition requires a 1000-frame software baseline")
        frozen = {
            "approach_speed_m_s": (self.approach_speed_m_s, 0.0005),
            "maximum_search_distance_m": (self.maximum_search_distance_m, 0.025),
            "contact_latch_load_n": (self.contact_latch_load_n, 1.0),
            "contact_latch_duration_s": (self.contact_latch_duration_s, 0.050),
            "acquisition_acceleration_m_s2": (
                self.acquisition_acceleration_m_s2,
                0.010,
            ),
            "acquisition_deceleration_m_s2": (
                self.acquisition_deceleration_m_s2,
                0.010,
            ),
            "stationary_dwell_s": (self.stationary_dwell_s, 0.100),
            "stationary_tcp_speed_limit_m_s": (
                self.stationary_tcp_speed_limit_m_s,
                0.0001,
            ),
            "stationary_rotation_speed_limit_rad_s": (
                self.stationary_rotation_speed_limit_rad_s,
                0.002,
            ),
            "stationary_joint_speed_limit_rad_s": (
                self.stationary_joint_speed_limit_rad_s,
                0.001,
            ),
            "maximum_handoff_mismatch_m": (
                self.maximum_handoff_mismatch_m,
                0.0003,
            ),
            "track_duration_s": (self.track_duration_s, 8.0),
            "retract_distance_m": (self.retract_distance_m, 0.010),
        }
        for name, (actual, expected) in frozen.items():
            if not math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"contact acquisition {name} is frozen")
        for name in (
            "acquisition_acceleration_m_s2",
            "acquisition_deceleration_m_s2",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.001 <= value <= 0.1:
                raise ValueError(f"contact acquisition {name} is outside its bound")
        bounded_positive = {
            "sensor_delivery_watchdog_s": (
                self.sensor_delivery_watchdog_s,
                FORMAL_SENSOR_DELIVERY_WATCHDOG_S,
            ),
            "stationary_dwell_s": (self.stationary_dwell_s, 1.0),
            "stationary_tcp_speed_limit_m_s": (
                self.stationary_tcp_speed_limit_m_s,
                0.01,
            ),
            "stationary_rotation_speed_limit_rad_s": (
                self.stationary_rotation_speed_limit_rad_s,
                0.02,
            ),
            "stationary_joint_speed_limit_rad_s": (
                self.stationary_joint_speed_limit_rad_s,
                0.01,
            ),
            "maximum_handoff_mismatch_m": (self.maximum_handoff_mismatch_m, 0.001),
        }
        for name, (actual, maximum) in bounded_positive.items():
            value = float(actual)
            if name == "sensor_delivery_watchdog_s":
                valid = math.isclose(
                    value,
                    FORMAL_SENSOR_DELIVERY_WATCHDOG_S,
                    rel_tol=0.0,
                    abs_tol=1.0e-12,
                )
            else:
                valid = math.isfinite(value) and 0.0 < value <= maximum
            if not valid:
                raise ValueError(f"contact acquisition {name} is outside its bound")
        if not math.isfinite(self.settle_duration_s) or not 0.25 <= self.settle_duration_s <= 2.0:
            raise ValueError("contact settle duration must be bounded")
        reaction = tuple(float(value) for value in self.reaction_normal_base)
        approach = tuple(float(value) for value in self.approach_normal_base)
        if len(reaction) != 3 or len(approach) != 3:
            raise ValueError("contact normals must contain three values")
        if not all(math.isfinite(value) for value in reaction + approach):
            raise ValueError("contact normals must be finite")
        if not math.isclose(sum(value * value for value in reaction), 1.0, abs_tol=1e-12):
            raise ValueError("reaction normal must be unit length")
        if approach != tuple(-value for value in reaction):
            raise ValueError("approach normal must be negative reaction normal")
        if "reaction_normal_base" not in self.normal_load_definition:
            raise ValueError("normal load semantics must name reaction_normal_base")
        object.__setattr__(self, "reaction_normal_base", reaction)
        object.__setattr__(self, "approach_normal_base", approach)

    @property
    def latch_samples(self) -> int:
        return int(round(self.contact_latch_duration_s * self.native_sensor_rate_hz))

    def search_displacement_m(self, elapsed_s: float) -> float:
        elapsed = float(elapsed_s)
        if not math.isfinite(elapsed) or elapsed < 0.0:
            raise ValueError("search elapsed time must be finite and non-negative")
        return min(self.maximum_search_distance_m, self.approach_speed_m_s * elapsed)

    def search_pose(
        self, entry_pose_base: Sequence[float], elapsed_s: float
    ) -> tuple[float, ...]:
        pose = tuple(float(value) for value in entry_pose_base)
        if len(pose) != 6 or not all(math.isfinite(value) for value in pose):
            raise ValueError("entry pose must contain six finite values")
        distance = self.search_displacement_m(elapsed_s)
        xyz = tuple(
            pose[index] + distance * self.approach_normal_base[index]
            for index in range(3)
        )
        return xyz + pose[3:]

    def retract_pose(
        self, contact_pose_base: Sequence[float], progress: float
    ) -> tuple[float, ...]:
        pose = tuple(float(value) for value in contact_pose_base)
        p = float(progress)
        if len(pose) != 6 or not all(math.isfinite(value) for value in pose):
            raise ValueError("contact pose must contain six finite values")
        if not math.isfinite(p) or not 0.0 <= p <= 1.0:
            raise ValueError("retract progress must be within [0,1]")
        # Quintic scalar avoids an acceleration discontinuity at both ends.
        smooth = 10.0 * p**3 - 15.0 * p**4 + 6.0 * p**5
        xyz = tuple(
            pose[index] + self.retract_distance_m * smooth * self.reaction_normal_base[index]
            for index in range(3)
        )
        return xyz + pose[3:]

    def permits_automatic_return(self, *, outcome: str, fault_class: str | None) -> bool:
        if fault_class is not None:
            if fault_class in self.no_auto_return_fault_classes:
                return False
            if outcome == FormalAttemptOutcome.HARD_FAULT.value:
                return False
        return outcome in self.safe_auto_return_outcomes

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "baseline_samples": self.baseline_samples,
            "native_sensor_rate_hz": self.native_sensor_rate_hz,
            "approach_speed_m_s": self.approach_speed_m_s,
            "maximum_search_distance_m": self.maximum_search_distance_m,
            "contact_latch_load_n": self.contact_latch_load_n,
            "contact_latch_duration_s": self.contact_latch_duration_s,
            "contact_latch_samples": self.latch_samples,
            "acquisition_acceleration_m_s2": self.acquisition_acceleration_m_s2,
            "acquisition_deceleration_m_s2": self.acquisition_deceleration_m_s2,
            "sensor_delivery_watchdog_s": self.sensor_delivery_watchdog_s,
            "sensor_delivery_watchdog_semantics": (
                "latest_native_batch_delivery_age_only_not_per_frame_host_arrival"
            ),
            "stationary_dwell_s": self.stationary_dwell_s,
            "stationary_tcp_speed_limit_m_s": self.stationary_tcp_speed_limit_m_s,
            "stationary_rotation_speed_limit_rad_s": self.stationary_rotation_speed_limit_rad_s,
            "stationary_joint_speed_limit_rad_s": self.stationary_joint_speed_limit_rad_s,
            "maximum_handoff_mismatch_m": self.maximum_handoff_mismatch_m,
            "settle_duration_s": self.settle_duration_s,
            "track_duration_s": self.track_duration_s,
            "retract_distance_m": self.retract_distance_m,
            "reaction_normal_base": list(self.reaction_normal_base),
            "approach_normal_base": list(self.approach_normal_base),
            "normal_load_definition": self.normal_load_definition,
            "safe_auto_return_outcomes": list(self.safe_auto_return_outcomes),
            "no_auto_return_fault_classes": list(self.no_auto_return_fault_classes),
        }


class ConsecutiveContactLatchV1:
    """Latch contact only after 50 consecutive native Kunwei samples."""

    def __init__(self, contract: ContactAcquisitionContractV1) -> None:
        self.contract = contract
        self.reset()

    def reset(self) -> None:
        self.consecutive = 0
        self.last_sample_index: int | None = None
        self.latched_sample_index: int | None = None

    def observe(self, *, sample_index: int, normal_load_n: float) -> bool:
        index = int(sample_index)
        load = float(normal_load_n)
        if index < 0 or not math.isfinite(load):
            raise ValueError("contact latch sample is invalid")
        if self.last_sample_index is not None and index != self.last_sample_index + 1:
            self.consecutive = 0
        self.last_sample_index = index
        if load >= self.contract.contact_latch_load_n:
            self.consecutive += 1
        else:
            self.consecutive = 0
        if self.consecutive >= self.contract.latch_samples:
            self.latched_sample_index = index
            return True
        return False


@dataclass(frozen=True)
class FormalAttemptReceiptV1:
    campaign_id: str
    attempt_ordinal: int
    eligible_ordinal: int
    episode: FormalCampaignEpisodeV1
    attempt_id: str
    outcome: str
    phase: str
    fault_class: str | None
    auto_return_performed: bool
    evidence_path: str
    evidence_sha256: str
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    recorder_manifest_path: str | None = None
    recorder_manifest_sha256: str | None = None
    eligibility_path: str | None = None
    eligibility_sha256: str | None = None
    previous_record_sha256: str = ZERO_SHA256
    schema_version: str = FORMAL_ATTEMPT_RECEIPT_SCHEMA_V1
    record_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_ATTEMPT_RECEIPT_SCHEMA_V1:
            raise ValueError("unsupported formal attempt receipt schema")
        if not self.campaign_id.strip() or not self.attempt_id.strip():
            raise ValueError("formal attempt identity is incomplete")
        if self.attempt_ordinal < 0 or self.eligible_ordinal < 0:
            raise ValueError("formal attempt ordinals must be non-negative")
        if self.episode.episode_index != self.eligible_ordinal:
            raise ValueError("formal attempt eligible ordinal/episode mismatch")
        if self.outcome not in {value.value for value in FormalAttemptOutcome}:
            raise ValueError("formal attempt outcome is invalid")
        if self.phase not in {value.value for value in FormalAttemptPhase}:
            raise ValueError("formal attempt phase is invalid")
        if self.outcome == FormalAttemptOutcome.ELIGIBLE.value:
            if self.phase != FormalAttemptPhase.COMPLETE.value:
                raise ValueError("eligible attempt must finish COMPLETE")
            if (
                self.artifact_path is None
                or self.recorder_manifest_path is None
                or self.eligibility_path is None
            ):
                raise ValueError(
                    "eligible attempt must bind artifact, recorder manifest, and eligibility receipt"
                )
            if self.fault_class is not None:
                raise ValueError("eligible attempt cannot carry a fault class")
        if self.outcome == FormalAttemptOutcome.HARD_FAULT.value and self.auto_return_performed:
            raise ValueError("hard-fault attempt cannot claim automatic return")
        for name in ("evidence_sha256", "previous_record_sha256"):
            _require_sha256(getattr(self, name), name)
        for name in (
            "artifact_sha256",
            "recorder_manifest_sha256",
            "eligibility_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, name)
        expected = _canonical_sha256(self.canonical_payload())
        if self.record_sha256 is not None and self.record_sha256 != expected:
            raise ValueError("formal attempt record hash mismatch")
        object.__setattr__(self, "record_sha256", expected)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "attempt_ordinal": self.attempt_ordinal,
            "eligible_ordinal": self.eligible_ordinal,
            "episode": self.episode.as_json(),
            "attempt_id": self.attempt_id,
            "outcome": self.outcome,
            "phase": self.phase,
            "fault_class": self.fault_class,
            "auto_return_performed": self.auto_return_performed,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "recorder_manifest_path": self.recorder_manifest_path,
            "recorder_manifest_sha256": self.recorder_manifest_sha256,
            "eligibility_path": self.eligibility_path,
            "eligibility_sha256": self.eligibility_sha256,
            "previous_record_sha256": self.previous_record_sha256,
        }

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"record_sha256": self.record_sha256}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "FormalAttemptReceiptV1":
        raw_episode = value.get("episode")
        if not isinstance(raw_episode, Mapping):
            raise ValueError("formal attempt episode payload is missing")
        episode = FormalCampaignEpisodeV1(
            episode_index=int(raw_episode["episode_index"]),
            phase=str(raw_episode["phase"]),
            trajectory_family=str(raw_episode["trajectory_family"]),
            target_load_n=float(raw_episode["target_load_n"]),
            training_included=bool(raw_episode["training_included"]),
        )
        return cls(
            campaign_id=str(value.get("campaign_id", "")),
            attempt_ordinal=int(value.get("attempt_ordinal", -1)),
            eligible_ordinal=int(value.get("eligible_ordinal", -1)),
            episode=episode,
            attempt_id=str(value.get("attempt_id", "")),
            outcome=str(value.get("outcome", "")),
            phase=str(value.get("phase", "")),
            fault_class=(None if value.get("fault_class") is None else str(value["fault_class"])),
            auto_return_performed=bool(value.get("auto_return_performed", False)),
            evidence_path=str(value.get("evidence_path", "")),
            evidence_sha256=str(value.get("evidence_sha256", "")),
            artifact_path=(None if value.get("artifact_path") is None else str(value["artifact_path"])),
            artifact_sha256=(None if value.get("artifact_sha256") is None else str(value["artifact_sha256"])),
            recorder_manifest_path=(
                None
                if value.get("recorder_manifest_path") is None
                else str(value["recorder_manifest_path"])
            ),
            recorder_manifest_sha256=(
                None
                if value.get("recorder_manifest_sha256") is None
                else str(value["recorder_manifest_sha256"])
            ),
            eligibility_path=(None if value.get("eligibility_path") is None else str(value["eligibility_path"])),
            eligibility_sha256=(None if value.get("eligibility_sha256") is None else str(value["eligibility_sha256"])),
            previous_record_sha256=str(value.get("previous_record_sha256", "")),
            schema_version=str(value.get("schema_version", "")),
            record_sha256=(None if value.get("record_sha256") is None else str(value["record_sha256"])),
        )


@dataclass(frozen=True)
class FormalCampaignProgressV1:
    campaign_id: str
    attempts: int
    eligible: int
    ineligible: int
    recoverable_failures: int
    hard_faults: int
    terminal: bool
    last_record_sha256: str

    @property
    def complete(self) -> bool:
        return self.eligible == FORMAL_CAMPAIGN_EPISODE_COUNT

    def as_json(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "attempts": self.attempts,
            "eligible": self.eligible,
            "required_eligible": FORMAL_CAMPAIGN_EPISODE_COUNT,
            "ineligible": self.ineligible,
            "recoverable_failures": self.recoverable_failures,
            "hard_faults": self.hard_faults,
            "terminal": self.terminal,
            "complete": self.complete,
            "last_record_sha256": self.last_record_sha256,
        }


class FormalCampaignLedgerV1:
    """Append-only, hash-chained terminal attempt ledger."""

    def __init__(self, campaign_root: str | Path, contract: FormalCampaignContractV1) -> None:
        self.root = Path(campaign_root)
        self.contract = contract
        self.ledger_path = self.root / "campaign_ledger.jsonl"
        self.attempts_root = self.root / "attempts"

    def initialize(self, *, source_bindings: Mapping[str, str]) -> dict[str, object]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.attempts_root.mkdir(parents=True, exist_ok=True)
        manifest_path = self.root / "campaign_manifest.json"
        payload: dict[str, object] = {
            "schema_version": FORMAL_CAMPAIGN_LEDGER_SCHEMA_V1,
            "campaign": self.contract.as_json(),
            "source_bindings": {
                str(key): _require_sha256(value, f"source_bindings.{key}")
                for key, value in sorted(source_bindings.items())
            },
            "eligible_target": FORMAL_CAMPAIGN_EPISODE_COUNT,
            "attempts_are_immutable": True,
            "failed_attempts_consume_eligible_ordinal": False,
            "model_active": False,
            "shadow_only": True,
        }
        payload["manifest_sha256"] = _canonical_sha256(payload)
        if manifest_path.exists():
            observed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if observed != payload:
                raise RuntimeError("formal campaign manifest identity changed")
        else:
            self._write_new_json(manifest_path, payload)
        return payload

    @staticmethod
    def _write_new_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def records(self, *, verify_artifacts: bool = True) -> tuple[FormalAttemptReceiptV1, ...]:
        if not self.ledger_path.exists():
            return ()
        raw = self.ledger_path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("formal campaign ledger has a torn final line")
        result: list[FormalAttemptReceiptV1] = []
        previous = ZERO_SHA256
        for line_number, line in enumerate(raw.splitlines(), start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"formal campaign ledger line {line_number} is invalid") from exc
            if not isinstance(value, Mapping):
                raise ValueError("formal campaign ledger row must be an object")
            record = FormalAttemptReceiptV1.from_json(value)
            if record.campaign_id != self.contract.campaign_id:
                raise ValueError("formal campaign ledger campaign identity mismatch")
            if record.attempt_ordinal != len(result):
                raise ValueError("formal campaign attempt ordinals are not contiguous")
            if record.previous_record_sha256 != previous:
                raise ValueError("formal campaign ledger hash chain is broken")
            expected_eligible = sum(
                prior.outcome == FormalAttemptOutcome.ELIGIBLE.value for prior in result
            )
            if record.eligible_ordinal != expected_eligible:
                raise ValueError("failed attempt consumed or skipped an eligible ordinal")
            planned = self.contract.build_episode_plan()[record.eligible_ordinal]
            if record.episode != planned:
                raise ValueError("formal campaign ledger episode plan mismatch")
            if verify_artifacts:
                self._verify_bound_path(record.evidence_path, record.evidence_sha256)
                if record.artifact_path is not None:
                    self._verify_bound_path(record.artifact_path, record.artifact_sha256)
                if record.recorder_manifest_path is not None:
                    self._verify_bound_path(
                        record.recorder_manifest_path,
                        record.recorder_manifest_sha256,
                    )
                if record.eligibility_path is not None:
                    self._verify_bound_path(record.eligibility_path, record.eligibility_sha256)
            previous = str(record.record_sha256)
            result.append(record)
        return tuple(result)

    def _verify_bound_path(self, relative: str, expected_sha256: str | None) -> None:
        if expected_sha256 is None:
            raise ValueError("formal campaign bound path is missing its SHA-256")
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("formal campaign bound path escapes campaign root") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"formal campaign bound artifact is missing: {relative}")
        if _sha256_file(candidate) != expected_sha256:
            raise ValueError(f"formal campaign bound artifact hash mismatch: {relative}")

    def progress(self, *, verify_artifacts: bool = True) -> FormalCampaignProgressV1:
        records = self.records(verify_artifacts=verify_artifacts)
        eligible = sum(record.outcome == FormalAttemptOutcome.ELIGIBLE.value for record in records)
        hard_faults = sum(record.outcome == FormalAttemptOutcome.HARD_FAULT.value for record in records)
        return FormalCampaignProgressV1(
            campaign_id=self.contract.campaign_id,
            attempts=len(records),
            eligible=eligible,
            ineligible=sum(record.outcome == FormalAttemptOutcome.INELIGIBLE.value for record in records),
            recoverable_failures=sum(record.outcome == FormalAttemptOutcome.RECOVERABLE_FAILURE.value for record in records),
            hard_faults=hard_faults,
            terminal=eligible >= self.contract.total_episodes or hard_faults > 0,
            last_record_sha256=(ZERO_SHA256 if not records else str(records[-1].record_sha256)),
        )

    def next_attempt(
        self, *, verify_artifacts: bool = True
    ) -> tuple[int, FormalCampaignEpisodeV1, Path]:
        progress = self.progress(verify_artifacts=verify_artifacts)
        if progress.hard_faults:
            raise RuntimeError("formal campaign is latched on a hard fault")
        if progress.complete:
            raise RuntimeError("formal campaign already has 200 eligible episodes")
        attempt_id = (
            f"attempt_{progress.attempts:04d}_eligible_{progress.eligible:03d}"
        )
        path = self.attempts_root / attempt_id
        if path.exists():
            raise FileExistsError(f"formal attempt directory already exists: {path}")
        return progress.attempts, self.contract.build_episode_plan()[progress.eligible], path

    def append_terminal(self, receipt: FormalAttemptReceiptV1) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        descriptor = os.open(self.ledger_path, flags, 0o640)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            # Re-resolve progress only after acquiring the durable ledger lock.
            progress = self.progress(verify_artifacts=False)
            if receipt.attempt_ordinal != progress.attempts:
                raise ValueError("formal attempt append ordinal mismatch")
            if receipt.eligible_ordinal != progress.eligible:
                raise ValueError("formal attempt append eligible ordinal mismatch")
            if receipt.previous_record_sha256 != progress.last_record_sha256:
                raise ValueError("formal attempt append previous hash mismatch")
            if progress.hard_faults or progress.complete:
                raise RuntimeError("formal campaign terminal state rejects append")
            # Re-read all bound bytes immediately before the durable append.
            self._verify_bound_path(receipt.evidence_path, receipt.evidence_sha256)
            if receipt.artifact_path is not None:
                self._verify_bound_path(receipt.artifact_path, receipt.artifact_sha256)
            if receipt.recorder_manifest_path is not None:
                self._verify_bound_path(
                    receipt.recorder_manifest_path,
                    receipt.recorder_manifest_sha256,
                )
            if receipt.eligibility_path is not None:
                self._verify_bound_path(
                    receipt.eligibility_path,
                    receipt.eligibility_sha256,
                )
            line = _canonical_bytes(receipt.as_json()) + b"\n"
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        # Cold-read after append proves the on-disk chain, not just the object.
        observed = self.records(verify_artifacts=False)
        if not observed or observed[-1].record_sha256 != receipt.record_sha256:
            raise RuntimeError("formal campaign ledger append cold-read mismatch")


def classify_fault(error: BaseException | str) -> tuple[str, bool]:
    """Return stable fault class and whether an automatic retract is allowed."""

    text = str(error).lower()
    # Diagnostic field names such as ``safety_mode`` must not turn a bounded
    # PREPARE/compile timeout into a safety hard fault.  The controller safety
    # transition paths have their own explicit error identities.
    if "formal_acquisition_prepare_ack_timeout" in text:
        return "recoverable_runtime", True
    hard_tokens = {
        "force_guard": (
            "force_guard",
            "force limit",
            "force_guard_exceeded",
            "active_force_over",
        ),
        "torque_guard": (
            "torque_guard",
            "torque limit",
            "torque_guard_exceeded",
            "active_torque_over",
        ),
        "protective_stop": ("protective",),
        "safety_changed": ("safety",),
        "joint_fault": ("joint_fault", "joint mode", "joint acceleration"),
        "route_identity_changed": ("identity", "protocol", "lease_echo", "episode_echo"),
        "route_fault": ("route_fault", "route identity"),
        "sensor_fault": ("kunwei", "sensor", "delivery", "parse", "baseline"),
    }
    for fault_class, tokens in hard_tokens.items():
        if any(token in text for token in tokens):
            return fault_class, False
    return "recoverable_runtime", True


__all__ = [
    "FORMAL_ATTEMPT_RECEIPT_SCHEMA_V1",
    "FORMAL_CAMPAIGN_LEDGER_SCHEMA_V1",
    "FORMAL_CONTACT_ACQUISITION_SCHEMA_V1",
    "ContactAcquisitionContractV1",
    "ConsecutiveContactLatchV1",
    "FormalAttemptOutcome",
    "FormalAttemptPhase",
    "FormalAttemptReceiptV1",
    "FormalCampaignLedgerV1",
    "FormalCampaignProgressV1",
    "ZERO_SHA256",
    "classify_fault",
]
