"""Immutable Step5d physical prior shared by TP and bridge adapters."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .identity import canonical_sha256


@dataclass(frozen=True)
class PhysicalPriorArtifact:
    prior_id: str
    reaction_normal_b: tuple[float, float, float]
    approach_axis_b: tuple[float, float, float]
    precontact_xyz_m: tuple[float, float, float]
    precontact_rotvec_rad: tuple[float, float, float]
    load_gate_n: float = 8.0
    load_gate_dwell_s: float = 0.10
    normal_rate_limit_rad_s: float = 0.05

    def __post_init__(self) -> None:
        for name, vector in (
            ("reaction_normal_b", self.reaction_normal_b),
            ("approach_axis_b", self.approach_axis_b),
            ("precontact_xyz_m", self.precontact_xyz_m),
            ("precontact_rotvec_rad", self.precontact_rotvec_rad),
        ):
            if len(vector) != 3 or not all(math.isfinite(value) for value in vector):
                raise ValueError(f"{name} must be a finite 3-vector")
        if abs(math.sqrt(sum(v * v for v in self.reaction_normal_b)) - 1.0) > 1e-8:
            raise ValueError("reaction normal must be unit length")
        if abs(math.sqrt(sum(v * v for v in self.approach_axis_b)) - 1.0) > 1e-8:
            raise ValueError("approach axis must be unit length")
        if max(abs(a + b) for a, b in zip(self.reaction_normal_b, self.approach_axis_b)) > 1e-8:
            raise ValueError("approach axis must oppose reaction normal")
        if self.load_gate_n <= 0 or self.load_gate_dwell_s <= 0 or self.normal_rate_limit_rad_s <= 0:
            raise ValueError("prior gate and rate limits must be positive")
        if max(abs(a - b) for a, b in zip(self._tool_z_axis_b(), self.approach_axis_b)) > 2e-9:
            raise ValueError("rotvec tool +Z does not reproduce approach axis")

    def _tool_z_axis_b(self) -> tuple[float, float, float]:
        rx, ry, rz = self.precontact_rotvec_rad
        theta = math.sqrt(rx * rx + ry * ry + rz * rz)
        if theta == 0.0:
            return (0.0, 0.0, 1.0)
        kx, ky, kz = rx / theta, ry / theta, rz / theta
        sine, cosine = math.sin(theta), math.cos(theta)
        return (
            ky * sine + kx * kz * (1.0 - cosine),
            -kx * sine + ky * kz * (1.0 - cosine),
            cosine + kz * kz * (1.0 - cosine),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema": "ur-exp/physical-prior-v1",
            "prior_id": self.prior_id,
            "reaction_normal_b": list(self.reaction_normal_b),
            "approach_axis_b": list(self.approach_axis_b),
            "precontact_xyz_m": list(self.precontact_xyz_m),
            "precontact_rotvec_rad": list(self.precontact_rotvec_rad),
            "load_gate_n": self.load_gate_n,
            "load_gate_dwell_s": self.load_gate_dwell_s,
            "normal_rate_limit_rad_s": self.normal_rate_limit_rad_s,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self.identity_payload())


STEP5D_V3_PHYSICAL_PRIOR = PhysicalPriorArtifact(
    prior_id="step5d_v3_physical_prior_contact_0p1_20260719",
    reaction_normal_b=(-0.043955267, 0.020079909, 0.998831683),
    approach_axis_b=(0.043955267, -0.020079909, -0.998831683),
    precontact_xyz_m=(0.487834547, 0.129337053, 0.022863519),
    precontact_rotvec_rad=(3.120752062, 0.0, 0.068626833),
)
