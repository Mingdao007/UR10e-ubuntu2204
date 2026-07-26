#!/usr/bin/env python3
"""Shared pre-contact pose contract helpers for UR TP package generation."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POSE_CONTRACT_TABLE_PATH = ROOT / "config" / "step_pose_contract_table.json"
PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID = "pre_contact_search_gravity_down_v1"


def load_pose_contract_table(path: Path = POSE_CONTRACT_TABLE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def contract_by_id(contract_id: str, path: Path = POSE_CONTRACT_TABLE_PATH) -> dict[str, Any]:
    table = load_pose_contract_table(path)
    for contract in table["contracts"]:
        if contract.get("id") == contract_id:
            return contract
    raise KeyError(f"pose contract not found: {contract_id}")


def norm3(values: tuple[float, float, float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def normalize3(values: tuple[float, float, float]) -> tuple[float, float, float]:
    n = norm3(values)
    if n <= 0.0 or not math.isfinite(n):
        raise ValueError(f"cannot normalize vector: {values}")
    return (values[0] / n, values[1] / n, values[2] / n)


def dot3(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sum(a[idx] * b[idx] for idx in range(3))


def rotvec_to_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    theta = math.sqrt(rx * rx + ry * ry + rz * rz)
    if theta < 1e-12:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    kx, ky, kz = rx / theta, ry / theta, rz / theta
    c = math.cos(theta)
    s = math.sin(theta)
    v = 1.0 - c
    return [
        [c + kx * kx * v, kx * ky * v - kz * s, kx * kz * v + ky * s],
        [ky * kx * v + kz * s, c + ky * ky * v, ky * kz * v - kx * s],
        [kz * kx * v - ky * s, kz * ky * v + kx * s, c + kz * kz * v],
    ]


def tool_z_axis_from_rotvec(rotvec: tuple[float, float, float]) -> tuple[float, float, float]:
    matrix = rotvec_to_matrix(*rotvec)
    return normalize3((matrix[0][2], matrix[1][2], matrix[2][2]))


def axis_error_rad(
    source: tuple[float, float, float],
    target: tuple[float, float, float],
) -> float:
    src = normalize3(source)
    dst = normalize3(target)
    return math.acos(max(-1.0, min(1.0, dot3(src, dst))))


def contract_target_rotvec_rad(
    contract_id: str = PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID,
) -> tuple[float, float, float]:
    contract = contract_by_id(contract_id)
    values = tuple(float(value) for value in contract["target_rotvec_rad"])
    if len(values) != 3:
        raise ValueError(f"target_rotvec_rad must contain three values: {values}")
    return values  # type: ignore[return-value]


def contract_target_axis_base(
    contract_id: str = PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID,
) -> tuple[float, float, float]:
    contract = contract_by_id(contract_id)
    values = tuple(float(value) for value in contract["target_axis_base"])
    if len(values) != 3:
        raise ValueError(f"target_axis_base must contain three values: {values}")
    return values  # type: ignore[return-value]


def validate_contract_axis(
    contract_id: str = PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID,
) -> float:
    contract = contract_by_id(contract_id)
    rotvec = contract_target_rotvec_rad(contract_id)
    target_axis = contract_target_axis_base(contract_id)
    error = axis_error_rad(tool_z_axis_from_rotvec(rotvec), target_axis)
    max_error = math.radians(float(contract["generation_axis_error_max_deg"]))
    if error > max_error:
        raise ValueError(
            f"pose contract {contract_id} axis error {error:.6f} rad exceeds {max_error:.6f} rad"
        )
    return error
