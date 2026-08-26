"""Hash-chained raw-evidence ledger for the 60 s Figure-eight objective."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r005.observations import ObservationRecord

from .core import (
    FIGURE8_DURATION_S,
    FIGURE8_FULL_BIN_COUNT,
    FORMAL_BIN_COUNT,
    MetricFingerprintV1,
    make_metric_result,
)
from .physical_candidate import FigureEightPhysicalCandidateV1


LEDGER_SCHEMA = "step6.autotune/figure8-physical-ledger-v1"
RAW_SCHEMA = "step6.autotune/figure8-raw-path-artifact-v1"
GENESIS_SHA256 = "0" * 64


class FigureEightPhysicalLedgerError(RuntimeError):
    """A Figure-eight raw artifact or physical-ledger binding is invalid."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FigureEightPhysicalLedgerError("Figure-eight evidence is not canonical") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha(value: Any, role: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FigureEightPhysicalLedgerError(f"{role} must be a lowercase SHA-256")
    return value


def _row_sha256(row: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical({key: value for key, value in row.items() if key != "row_sha256"}))


@dataclass(frozen=True)
class FigureEightPhysicalRecordV1:
    campaign_fingerprint: str
    epoch: int
    attempt_sequence: int
    kind: str
    candidate: FigureEightPhysicalCandidateV1
    safe_return: bool
    binding_ok: bool
    safety_gate: bool
    contact_gate: bool
    return_gate: bool
    motion_gate: bool
    timing_gate: bool
    identity_gate: bool
    duration_s: float
    metrics: Mapping[str, Any]
    metric_result: Mapping[str, Any] | None
    raw_artifact: Mapping[str, Any] | None
    sealed: bool = True

    def __post_init__(self) -> None:
        _require_sha(self.campaign_fingerprint, "Figure-eight campaign fingerprint")
        if self.epoch <= 0 or self.attempt_sequence <= 0:
            raise FigureEightPhysicalLedgerError("Figure-eight attempt identity is invalid")
        if not isinstance(self.candidate, FigureEightPhysicalCandidateV1):
            raise FigureEightPhysicalLedgerError("Figure-eight physical candidate is not typed")
        if not isinstance(self.metrics, Mapping):
            raise FigureEightPhysicalLedgerError("Figure-eight metrics are not a mapping")
        if not math.isfinite(float(self.duration_s)) or float(self.duration_s) < 0.0:
            raise FigureEightPhysicalLedgerError("Figure-eight duration is invalid")
        object.__setattr__(self, "duration_s", float(self.duration_s))
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    @property
    def candidate_uid(self) -> str:
        return self.candidate.candidate_uid

    @property
    def mae_n(self) -> float | None:
        if not isinstance(self.metric_result, Mapping):
            return None
        try:
            value = float(self.metric_result["formal_mae_n"])
        except (KeyError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0.0 else None

    @property
    def eligible(self) -> bool:
        metric = self.metric_result or {}
        return bool(
            self.sealed
            and self.safe_return
            and self.binding_ok
            and self.safety_gate
            and self.contact_gate
            and self.return_gate
            and self.motion_gate
            and self.timing_gate
            and self.identity_gate
            and self.duration_s >= FIGURE8_DURATION_S
            and self.raw_artifact is not None
            and metric.get("sealed") is True
            and metric.get("exact") is True
            and metric.get("observed_formal_bin_count") == FORMAL_BIN_COUNT
            and metric.get("observed_full_bin_count") == FIGURE8_FULL_BIN_COUNT
            and self.mae_n is not None
        )

    @property
    def trial_admission_passed(self) -> bool:
        return self.eligible

    @property
    def observation_uid(self) -> str:
        return _sha256_bytes(_canonical(self.payload()))

    def payload(self) -> dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "record_type": "observation",
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "attempt_sequence": self.attempt_sequence,
            "kind": self.kind,
            "candidate": self.candidate.canonical,
            "candidate_uid": self.candidate_uid,
            "safe_return": self.safe_return,
            "binding_ok": self.binding_ok,
            "safety_gate": self.safety_gate,
            "contact_gate": self.contact_gate,
            "return_gate": self.return_gate,
            "motion_gate": self.motion_gate,
            "timing_gate": self.timing_gate,
            "identity_gate": self.identity_gate,
            "duration_s": self.duration_s,
            "metrics": dict(self.metrics),
            "metric_result": None if self.metric_result is None else dict(self.metric_result),
            "raw_artifact": None if self.raw_artifact is None else dict(self.raw_artifact),
            "mae_n": self.mae_n,
            "eligible": self.eligible,
            "trial_admission_passed": self.trial_admission_passed,
            "sealed": self.sealed,
        }


class FigureEightPhysicalLedgerV1:
    """One-writer append ledger with exact raw-curve recomputation."""

    def __init__(
        self,
        path: Path,
        *,
        campaign_fingerprint: str,
        eoat_sha256: str,
        tail_rows: int = 5,
    ) -> None:
        self.path = Path(path)
        self.campaign_fingerprint = _require_sha(
            campaign_fingerprint, "Figure-eight campaign fingerprint"
        )
        self.eoat_sha256 = _require_sha(eoat_sha256, "Figure-eight EOAT identity")
        self.tail_rows = int(tail_rows)
        if self.tail_rows < 1:
            raise FigureEightPhysicalLedgerError("Figure-eight tail_rows must be positive")
        self.raw_root = self.path.parent / "figure8_raw_path_artifacts"
        self._records: tuple[FigureEightPhysicalRecordV1, ...] = ()
        self._head_sha256 = GENESIS_SHA256
        if not self.path.exists():
            self._create_header()
        self.fresh_process_verify()

    @property
    def records(self) -> tuple[FigureEightPhysicalRecordV1, ...]:
        return self._records

    @property
    def head_sha256(self) -> str:
        return self._head_sha256

    def _create_header(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "schema": LEDGER_SCHEMA,
            "record_type": "header",
            "version": 1,
            "campaign_fingerprint": self.campaign_fingerprint,
            "eoat_sha256": self.eoat_sha256,
            "metric_id": "figure8-force-mae-v1",
            "duration_s": FIGURE8_DURATION_S,
            "formal_window_s": [5.0, FIGURE8_DURATION_S],
            "required_formal_bins": FORMAL_BIN_COUNT,
            "required_full_bins": FIGURE8_FULL_BIN_COUNT,
            "raw_gap_policy": "preserve_no_interpolation",
        }
        try:
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(_canonical(header).decode("utf-8") + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass

    def _strict_rows(self) -> list[dict[str, Any]]:
        if self.path.is_symlink() or not self.path.is_file():
            raise FigureEightPhysicalLedgerError("Figure-eight ledger is not a regular file")
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FigureEightPhysicalLedgerError(
                    f"Figure-eight ledger row {number} is invalid JSON"
                ) from exc
            if not isinstance(row, dict):
                raise FigureEightPhysicalLedgerError(
                    f"Figure-eight ledger row {number} is not an object"
                )
            rows.append(row)
        return rows

    def _artifact_path(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise FigureEightPhysicalLedgerError("Figure-eight raw artifact path escapes")
        target = (self.path.parent / relative).resolve()
        root = self.path.parent.resolve()
        if root not in target.parents:
            raise FigureEightPhysicalLedgerError("Figure-eight raw artifact path escapes")
        if target.is_symlink() or not target.is_file():
            raise FigureEightPhysicalLedgerError("Figure-eight raw artifact is unavailable")
        return target

    @staticmethod
    def _metric_samples(raw_samples: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
        return [
            {
                "time_s": float(sample["path_time_s"]),
                "normal_load_n": float(sample["filtered_normal_n"]),
            }
            for sample in raw_samples
        ]

    def _verify_artifact(
        self, binding: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        path = self._artifact_path(str(binding.get("relative_path", "")))
        raw_bytes = path.read_bytes()
        if (
            _sha256_bytes(raw_bytes) != binding.get("byte_sha256")
            or len(raw_bytes) != binding.get("byte_size")
        ):
            raise FigureEightPhysicalLedgerError("Figure-eight raw artifact bytes differ")
        try:
            artifact = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FigureEightPhysicalLedgerError("Figure-eight raw artifact is invalid") from exc
        if artifact.get("schema") != RAW_SCHEMA:
            raise FigureEightPhysicalLedgerError("Figure-eight raw artifact schema differs")
        raw_samples = artifact.get("samples")
        if not isinstance(raw_samples, list) or len(raw_samples) != binding.get("sample_count"):
            raise FigureEightPhysicalLedgerError("Figure-eight raw sample count differs")
        try:
            metric = make_metric_result(self._metric_samples(raw_samples)).as_dict()
            metric_error = None
        except Exception as exc:
            metric = None
            metric_error = f"{type(exc).__name__}: {exc}"
        if metric is None:
            if (
                binding.get("metric_status") != "incomplete_raw_preserved"
                or binding.get("metric_result_sha256") is not None
                or binding.get("metric_result") is not None
                or binding.get("metric_error") != metric_error
            ):
                raise FigureEightPhysicalLedgerError(
                    "Figure-eight incomplete raw metric binding differs"
                )
        elif (
            binding.get("metric_status") != "sealed_exact"
            or _sha256_bytes(_canonical(metric)) != binding.get("metric_result_sha256")
            or metric != binding.get("metric_result")
            or binding.get("metric_error") is not None
        ):
            raise FigureEightPhysicalLedgerError("Figure-eight raw metric binding differs")
        return artifact, metric

    def _record_from_row(self, row: Mapping[str, Any]) -> FigureEightPhysicalRecordV1:
        return FigureEightPhysicalRecordV1(
            campaign_fingerprint=str(row["campaign_fingerprint"]),
            epoch=int(row["epoch"]),
            attempt_sequence=int(row["attempt_sequence"]),
            kind=str(row["kind"]),
            candidate=FigureEightPhysicalCandidateV1.from_canonical(row["candidate"]),
            safe_return=bool(row["safe_return"]),
            binding_ok=bool(row["binding_ok"]),
            safety_gate=bool(row["safety_gate"]),
            contact_gate=bool(row["contact_gate"]),
            return_gate=bool(row["return_gate"]),
            motion_gate=bool(row["motion_gate"]),
            timing_gate=bool(row["timing_gate"]),
            identity_gate=bool(row["identity_gate"]),
            duration_s=float(row["duration_s"]),
            metrics=row.get("metrics", {}),
            metric_result=row.get("metric_result"),
            raw_artifact=row.get("raw_artifact"),
            sealed=bool(row.get("sealed", False)),
        )

    def fresh_process_verify(self) -> dict[str, Any]:
        rows = self._strict_rows()
        if not rows:
            raise FigureEightPhysicalLedgerError("Figure-eight ledger has no header")
        header = rows[0]
        if (
            header.get("schema") != LEDGER_SCHEMA
            or header.get("record_type") != "header"
            or header.get("campaign_fingerprint") != self.campaign_fingerprint
            or header.get("eoat_sha256") != self.eoat_sha256
        ):
            raise FigureEightPhysicalLedgerError("Figure-eight ledger header differs")
        previous = GENESIS_SHA256
        seen: set[int] = set()
        observation_rows = rows[1:]
        verify_indices = set(
            range(max(0, len(observation_rows) - self.tail_rows), len(observation_rows))
        )
        records: list[FigureEightPhysicalRecordV1] = []
        for index, row in enumerate(observation_rows):
            if (
                row.get("schema") != LEDGER_SCHEMA
                or row.get("record_type") != "observation"
                or row.get("previous_sha256") != previous
                or row.get("row_sha256") != _row_sha256(row)
            ):
                raise FigureEightPhysicalLedgerError(
                    f"Figure-eight ledger chain differs at observation {index + 1}"
                )
            sequence = int(row.get("attempt_sequence", 0))
            if sequence <= 0 or sequence in seen:
                raise FigureEightPhysicalLedgerError("Figure-eight attempt sequence repeats")
            seen.add(sequence)
            if row.get("campaign_fingerprint") != self.campaign_fingerprint:
                raise FigureEightPhysicalLedgerError("Figure-eight row fingerprint differs")
            if row.get("kind") == "QUALIFICATION":
                raise FigureEightPhysicalLedgerError(
                    "Figure-eight campaign ledger does not accept qualification rows"
                )
            binding = row.get("raw_artifact")
            if not isinstance(binding, Mapping):
                raise FigureEightPhysicalLedgerError(
                    "Figure-eight PATH row lacks raw artifact"
                )
            if index in verify_indices:
                _artifact, metric = self._verify_artifact(binding)
                if metric != row.get("metric_result"):
                    raise FigureEightPhysicalLedgerError(
                        "Figure-eight cold metric differs from ledger row"
                    )
            record = self._record_from_row(row)
            if row.get("eligible") is not record.eligible:
                raise FigureEightPhysicalLedgerError(
                    "Figure-eight derived eligibility differs"
                )
            records.append(record)
            previous = str(row["row_sha256"])
        self._records = tuple(records)
        self._head_sha256 = previous
        return {
            "schema": "step6.autotune/figure8-physical-ledger-cold-read-v1",
            "record_count": len(records),
            "tail_raw_rows_verified": len(verify_indices),
            "ledger_head_sha256": previous,
        }

    def _write_raw_artifact(
        self, record: ObservationRecord
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        raw_samples = [sample.as_dict() for sample in record.raw_path_samples]
        try:
            metric = make_metric_result(self._metric_samples(raw_samples)).as_dict()
            metric_error = None
        except Exception as exc:
            metric = None
            metric_error = f"{type(exc).__name__}: {exc}"
        artifact = {
            "schema": RAW_SCHEMA,
            "version": 1,
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": record.epoch,
            "attempt_sequence": record.attempt_sequence,
            "kind": record.kind,
            "execution_id": record.metrics.get("execution_id"),
            "candidate_uid": record.candidate.candidate_uid,
            # Even an incomplete curve is bound to the immutable Figure-eight
            # objective.  Do not dereference the absent sealed result here;
            # preserving the raw gap is the whole point of this branch.
            "metric_fingerprint": (
                metric["metric_fingerprint"]
                if metric is not None
                else MetricFingerprintV1.figure8().as_dict()
            ),
            "samples": raw_samples,
        }
        raw_bytes = _canonical(artifact) + b"\n"
        relative = Path("figure8_raw_path_artifacts") / (
            f"attempt-{record.attempt_sequence:06d}-{record.candidate.candidate_uid}.json"
        )
        target = self.path.parent / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != raw_bytes:
                raise FigureEightPhysicalLedgerError(
                    "Figure-eight raw artifact path already contains different bytes"
                )
        else:
            temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
            try:
                with temporary.open("xb") as stream:
                    stream.write(raw_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        binding = {
            "schema": "step6.autotune/figure8-raw-artifact-binding-v1",
            "relative_path": relative.as_posix(),
            "byte_sha256": _sha256_bytes(raw_bytes),
            "byte_size": len(raw_bytes),
            "sample_count": len(raw_samples),
            "metric_status": (
                "sealed_exact" if metric is not None else "incomplete_raw_preserved"
            ),
            "metric_result_sha256": (
                None if metric is None else _sha256_bytes(_canonical(metric))
            ),
            "metric_result": metric,
            "metric_error": metric_error,
        }
        self._verify_artifact(binding)
        return binding, metric

    def append(self, record: ObservationRecord) -> FigureEightPhysicalRecordV1:
        if not isinstance(record, ObservationRecord):
            raise FigureEightPhysicalLedgerError("Figure-eight append requires ObservationRecord")
        if record.campaign_fingerprint != self.campaign_fingerprint:
            raise FigureEightPhysicalLedgerError("Figure-eight append fingerprint differs")
        if not record.sealed:
            record = record.seal()
        if not isinstance(record.candidate, FigureEightPhysicalCandidateV1):
            raise FigureEightPhysicalLedgerError("Figure-eight append candidate is not typed")
        if any(
            existing.attempt_sequence == record.attempt_sequence
            for existing in self._records
        ):
            raise FigureEightPhysicalLedgerError("Figure-eight attempt already exists")
        if self._records and record.attempt_sequence <= self._records[-1].attempt_sequence:
            raise FigureEightPhysicalLedgerError("Figure-eight attempt sequence regressed")
        if record.kind == "QUALIFICATION":
            raise FigureEightPhysicalLedgerError(
                "Figure-eight campaign ledger does not accept qualification rows"
            )
        binding, metric = self._write_raw_artifact(record)
        bounded_metrics = dict(record.metrics)
        bounded_metrics.pop("force_objective", None)
        exact_seal = bool(
            metric is not None
            and metric.get("sealed") is True
            and metric.get("exact") is True
            and metric.get("observed_formal_bin_count") == FORMAL_BIN_COUNT
            and metric.get("observed_full_bin_count") == FIGURE8_FULL_BIN_COUNT
        )
        sealed = FigureEightPhysicalRecordV1(
            campaign_fingerprint=self.campaign_fingerprint,
            epoch=record.epoch,
            attempt_sequence=record.attempt_sequence,
            kind=record.kind,
            candidate=record.candidate,
            safe_return=record.safe_return,
            binding_ok=record.binding_ok,
            safety_gate=record.safety_gate,
            contact_gate=record.contact_gate,
            return_gate=record.return_gate,
            motion_gate=record.motion_gate,
            timing_gate=record.timing_gate,
            identity_gate=record.identity_gate,
            duration_s=record.duration_s,
            metrics=bounded_metrics,
            metric_result=metric,
            raw_artifact=binding,
            sealed=exact_seal,
        )
        row = {
            **sealed.payload(),
            "previous_sha256": self._head_sha256,
        }
        row["row_sha256"] = _row_sha256(row)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical(row).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.fresh_process_verify()
        return self._records[-1]


__all__ = [
    "FigureEightPhysicalLedgerError",
    "FigureEightPhysicalLedgerV1",
    "FigureEightPhysicalRecordV1",
]
