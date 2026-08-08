"""Production dynamics primitives for the TacDiffusion formal V4 path.

The controller probe in this module has no motion or force API.  It exposes
``get_jacobian(q)`` and ``get_coriolis_and_centrifugal_torques(q, qd)`` one
column at a time so a calibrated host model can be checked against the exact
controller APIs used by Direct Torque.  Formal episode rows may bind to the
resulting immutable receipt; a source hash or a host model alone is not a
conformance receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Callable, Mapping, Sequence

import numpy as np

from .contracts import DynamicsConformanceBinding, DynamicsReceipt, DynamicsSample


FORMAL_DYNAMICS_PROBE_SCHEMA_V1 = "ur10e_tacdiffusion_dynamics_probe/v1"
FORMAL_DYNAMICS_RECEIPT_SCHEMA_V1 = (
    "ur10e_tacdiffusion_dynamics_conformance_receipt/v1"
)
FORMAL_DYNAMICS_PROBE_TOKEN = 4_000_204
FORMAL_DYNAMICS_PROBE_ACTIVE = 88
FORMAL_DYNAMICS_PROBE_COMPLETE = 89
FORMAL_TCP_OFFSET_TOOL0_M = (0.0, 0.0, 0.0874)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_formal_dynamics_probe_source() -> str:
    """Return a bounded, no-motion URScript dynamics read-back program."""

    return f'''def tacdiffusion_formal_dynamics_probe_v1():
  local probe_schema = "{FORMAL_DYNAMICS_PROBE_SCHEMA_V1}"
  local probe_token = {FORMAL_DYNAMICS_PROBE_TOKEN}
  local q = get_actual_joint_positions()
  local qd = get_actual_joint_speeds()
  local jacobian = get_jacobian(q)
  local coriolis = get_coriolis_and_centrifugal_torques(q, qd)
  local column = 0
  while column < 6:
    write_output_integer_register(24, {FORMAL_DYNAMICS_PROBE_ACTIVE})
    write_output_integer_register(25, column)
    write_output_integer_register(32, probe_token)
    local axis = 0
    while axis < 6:
      write_output_float_register(26 + axis, jacobian[axis, column])
      write_output_float_register(32 + axis, coriolis[axis])
      axis = axis + 1
    end
    sync()
    column = column + 1
  end
  write_output_integer_register(24, {FORMAL_DYNAMICS_PROBE_COMPLETE})
  write_output_integer_register(25, 6)
  write_output_integer_register(32, probe_token)
  sync()
end
tacdiffusion_formal_dynamics_probe_v1()
'''


def validate_formal_dynamics_probe_source(source: str) -> None:
    required = (
        f'probe_schema = "{FORMAL_DYNAMICS_PROBE_SCHEMA_V1}"',
        f"probe_token = {FORMAL_DYNAMICS_PROBE_TOKEN}",
        "local jacobian = get_jacobian(q)",
        "local coriolis = get_coriolis_and_centrifugal_torques(q, qd)",
        "column < 6",
        "jacobian[axis, column]",
        "tacdiffusion_formal_dynamics_probe_v1()",
    )
    if any(token not in source for token in required):
        raise ValueError("formal dynamics probe source is incomplete")
    forbidden = (
        r"\bdirect_torque\s*\(",
        r"\bget_tcp_force\s*\(",
        r"\bzero_ftsensor\s*\(",
        r"\b(movej|movel|movec|speedj|speedl|servoj|force_mode|stopj)\s*\(",
        r"\bread_input_(?:float|integer)_register\s*\(",
    )
    if any(re.search(pattern, source, flags=re.IGNORECASE) for pattern in forbidden):
        raise ValueError("formal dynamics probe contains a forbidden API")
    if len(re.findall(r"(?m)^\s*def\s+", source)) != 1:
        raise ValueError("formal dynamics probe requires one top-level program")


@dataclass(frozen=True)
class FormalDynamicsConformanceV1:
    """Hash-bound comparison of controller and calibrated-host dynamics."""

    controller_jacobian_6x6: Sequence[Sequence[float]]
    host_jacobian_6x6: Sequence[Sequence[float]]
    controller_coriolis_nm: Sequence[float]
    host_coriolis_nm: Sequence[float]
    q_rad: Sequence[float]
    qd_rad_s: Sequence[float]
    controller_source_sha256: str
    calibrated_model_sha256: str
    calibration_identity: str
    tcp_identity: str = "tool0_tcp_z_0p0874"
    jacobian_max_abs_tolerance: float = 2.0e-3
    coriolis_max_abs_tolerance_nm: float = 5.0e-2
    schema_version: str = FORMAL_DYNAMICS_RECEIPT_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_DYNAMICS_RECEIPT_SCHEMA_V1:
            raise ValueError("unsupported formal dynamics receipt schema")
        for name in ("controller_jacobian_6x6", "host_jacobian_6x6"):
            value = np.asarray(getattr(self, name), dtype=float)
            if value.shape != (6, 6) or not np.isfinite(value).all():
                raise ValueError(f"{name} must be a finite 6x6 matrix")
            object.__setattr__(self, name, tuple(tuple(float(x) for x in row) for row in value))
        for name in ("controller_coriolis_nm", "host_coriolis_nm", "q_rad", "qd_rad_s"):
            value = tuple(float(x) for x in getattr(self, name))
            if len(value) != 6 or not all(math.isfinite(x) for x in value):
                raise ValueError(f"{name} must contain six finite values")
            object.__setattr__(self, name, value)
        for name in ("controller_source_sha256", "calibrated_model_sha256"):
            value = str(getattr(self, name))
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if not self.calibration_identity.strip() or not self.tcp_identity.strip():
            raise ValueError("formal dynamics identities are required")
        if self.jacobian_max_abs_tolerance <= 0.0 or self.coriolis_max_abs_tolerance_nm <= 0.0:
            raise ValueError("formal dynamics tolerances must be positive")

    @property
    def jacobian_max_abs_error(self) -> float:
        return float(
            np.max(
                np.abs(
                    np.asarray(self.controller_jacobian_6x6)
                    - np.asarray(self.host_jacobian_6x6)
                )
            )
        )

    @property
    def coriolis_max_abs_error_nm(self) -> float:
        return float(
            np.max(
                np.abs(
                    np.asarray(self.controller_coriolis_nm)
                    - np.asarray(self.host_coriolis_nm)
                )
            )
        )

    @property
    def accepted(self) -> bool:
        return bool(
            self.jacobian_max_abs_error <= self.jacobian_max_abs_tolerance
            and self.coriolis_max_abs_error_nm <= self.coriolis_max_abs_tolerance_nm
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "controller_jacobian_6x6": [list(row) for row in self.controller_jacobian_6x6],
            "host_jacobian_6x6": [list(row) for row in self.host_jacobian_6x6],
            "controller_coriolis_nm": list(self.controller_coriolis_nm),
            "host_coriolis_nm": list(self.host_coriolis_nm),
            "q_rad": list(self.q_rad),
            "qd_rad_s": list(self.qd_rad_s),
            "controller_source_sha256": self.controller_source_sha256,
            "calibrated_model_sha256": self.calibrated_model_sha256,
            "calibration_identity": self.calibration_identity,
            "tcp_identity": self.tcp_identity,
            "jacobian_max_abs_tolerance": self.jacobian_max_abs_tolerance,
            "coriolis_max_abs_tolerance_nm": self.coriolis_max_abs_tolerance_nm,
            "jacobian_max_abs_error": self.jacobian_max_abs_error,
            "coriolis_max_abs_error_nm": self.coriolis_max_abs_error_nm,
            "accepted": self.accepted,
            "motion_performed": False,
            "ur_force_fields_read": False,
        }

    @property
    def receipt_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return _sha256_bytes(encoded)

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"receipt_sha256": self.receipt_sha256}


def load_formal_dynamics_conformance_receipt(
    payload: Mapping[str, object],
) -> FormalDynamicsConformanceV1:
    receipt = FormalDynamicsConformanceV1(
        controller_jacobian_6x6=payload["controller_jacobian_6x6"],
        host_jacobian_6x6=payload["host_jacobian_6x6"],
        controller_coriolis_nm=payload["controller_coriolis_nm"],
        host_coriolis_nm=payload["host_coriolis_nm"],
        q_rad=payload["q_rad"],
        qd_rad_s=payload["qd_rad_s"],
        controller_source_sha256=str(payload["controller_source_sha256"]),
        calibrated_model_sha256=str(payload["calibrated_model_sha256"]),
        calibration_identity=str(payload["calibration_identity"]),
        tcp_identity=str(payload.get("tcp_identity", "")),
        jacobian_max_abs_tolerance=float(payload["jacobian_max_abs_tolerance"]),
        coriolis_max_abs_tolerance_nm=float(payload["coriolis_max_abs_tolerance_nm"]),
        schema_version=str(payload.get("schema_version", "")),
    )
    if payload.get("receipt_sha256") != receipt.receipt_sha256:
        raise ValueError("formal dynamics receipt hash mismatch")
    if not receipt.accepted:
        raise ValueError("formal dynamics receipt is not accepted")
    return receipt


class ProductionDynamicsRuntimeV1:
    """Construct per-row, previous-tick production dynamics receipts.

    The numeric providers must be the same calibrated-host implementation
    accepted by ``FormalDynamicsConformanceV1``.  They are injected so the
    contract remains independently testable and so importing this module does
    not require the ROS/Pinocchio environment.
    """

    JOINT_DAMPING = (1.5, 1.5, 1.2, 0.3, 0.3, 0.2)

    def __init__(
        self,
        conformance: FormalDynamicsConformanceV1,
        *,
        jacobian_provider: Callable[[np.ndarray], np.ndarray],
        coriolis_provider: Callable[[np.ndarray, np.ndarray], np.ndarray],
        source_hashes: Mapping[str, str],
    ) -> None:
        if not isinstance(conformance, FormalDynamicsConformanceV1) or not conformance.accepted:
            raise ValueError("production dynamics requires accepted conformance")
        hashes = {str(key): str(value) for key, value in source_hashes.items()}
        required_hashes = {
            "controller_conformance": conformance.receipt_sha256,
            "calibrated_model": conformance.calibrated_model_sha256,
        }
        if any(hashes.get(key) != value for key, value in required_hashes.items()):
            raise ValueError("production dynamics source hashes do not bind conformance")
        self.conformance = conformance
        self.jacobian_provider = jacobian_provider
        self.coriolis_provider = coriolis_provider
        self.source_hashes = hashes
        self.previous_sample: DynamicsSample | None = None

    @staticmethod
    def _row_vector(row: Mapping[str, object], prefix: str) -> tuple[float, ...]:
        values = tuple(float(row[f"{prefix}_{axis}"]) for axis in range(6))
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"production dynamics {prefix} is non-finite")
        return values

    def produce(
        self,
        previous_row: Mapping[str, object],
        *,
        sequence: int,
        timestamp_s: float,
    ) -> tuple[DynamicsSample, DynamicsReceipt]:
        q = self._row_vector(previous_row, "actual_q")
        qd = self._row_vector(previous_row, "actual_qd")
        commanded = self._row_vector(previous_row, "commanded_joint_torque_nm")
        actual_current = self._row_vector(previous_row, "actual_current_as_torque")
        jacobian = np.asarray(self.jacobian_provider(np.asarray(q, dtype=float)), dtype=float)
        coriolis = np.asarray(
            self.coriolis_provider(np.asarray(q, dtype=float), np.asarray(qd, dtype=float)),
            dtype=float,
        )
        if jacobian.shape != (6, 6) or coriolis.shape != (6,):
            raise ValueError("production dynamics provider shape mismatch")
        damping = tuple(
            coefficient * velocity
            for coefficient, velocity in zip(self.JOINT_DAMPING, qd)
        )
        sample = DynamicsSample(
            sequence=int(sequence),
            timestamp_s=float(timestamp_s),
            previous_q=q,
            previous_qd=qd,
            previous_commanded_no_gravity_torque_nm=commanded,
            calibrated_jacobian=jacobian,
            coriolis_torque_nm=coriolis,
            joint_damping_torque_nm=damping,
            source_hashes=self.source_hashes,
            model_identity=self.conformance.calibrated_model_sha256,
            tcp_identity=self.conformance.tcp_identity,
            calibration_identity=self.conformance.calibration_identity,
            frame_id="tool0_tcp",
            canonical_tcp_frame_id="tool0_tcp",
            jacobian_frame_id="tool0_tcp",
            source_identity="calibrated_pinocchio_controller_conformed_v1",
            actual_current_torque_nm_shadow=actual_current,
        )
        binding = DynamicsConformanceBinding(
            binding_id=f"{self.conformance.receipt_sha256[:16]}_{sequence}",
            controller_receipt_identity=FORMAL_DYNAMICS_RECEIPT_SCHEMA_V1,
            controller_receipt_sha256=self.conformance.receipt_sha256,
            sample_fingerprint_sha256=sample.fingerprint_sha256,
            model_identity=sample.model_identity,
            tcp_identity=sample.tcp_identity,
            calibration_identity=sample.calibration_identity,
        )
        receipt = DynamicsReceipt.from_sample(
            sample,
            previous_sample=self.previous_sample,
            source_kind="production",
            conformance_binding=binding,
        )
        self.previous_sample = sample
        return sample, receipt


__all__ = [
    "FORMAL_DYNAMICS_PROBE_ACTIVE",
    "FORMAL_DYNAMICS_PROBE_COMPLETE",
    "FORMAL_DYNAMICS_PROBE_SCHEMA_V1",
    "FORMAL_DYNAMICS_PROBE_TOKEN",
    "FORMAL_DYNAMICS_RECEIPT_SCHEMA_V1",
    "FORMAL_TCP_OFFSET_TOOL0_M",
    "FormalDynamicsConformanceV1",
    "ProductionDynamicsRuntimeV1",
    "build_formal_dynamics_probe_source",
    "load_formal_dynamics_conformance_receipt",
    "validate_formal_dynamics_probe_source",
]
