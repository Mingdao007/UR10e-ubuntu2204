"""Explicit source loading and geometry validation for the inherited Step6 XY frame."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .contracts import SAFE_FRAME_SHA256, SAFE_FRAME_SOURCE, STEP6_R001_CONTRACT, require_finite_float
from .errors import ContractInvariantError, SafeFrameValidationError


_BASIS_TOLERANCE: Final[float] = 1e-9
DEFAULT_SAFE_FRAME_PATH: Final[Path] = Path(__file__).resolve().parents[2] / SAFE_FRAME_SOURCE


def _vector(value: object, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SafeFrameValidationError(f"{label} must contain exactly two values")
    try:
        return tuple(require_finite_float(item, f"{label}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]
    except ContractInvariantError as exc:
        raise SafeFrameValidationError(str(exc)) from exc


def _validate_xy_basis(
    origin_xy_m: tuple[float, float],
    u_along_xy: tuple[float, float],
    p_lateral_xy: tuple[float, float],
) -> None:
    for label, vector in (
        ("origin_xy_m", origin_xy_m),
        ("u_along_xy", u_along_xy),
        ("p_lateral_xy", p_lateral_xy),
    ):
        if len(vector) != 2:
            raise SafeFrameValidationError(f"{label} must contain exactly two values")
        for index, value in enumerate(vector):
            try:
                require_finite_float(value, f"{label}[{index}]")
            except ContractInvariantError as exc:
                raise SafeFrameValidationError(str(exc)) from exc

    u_norm = math.hypot(*u_along_xy)
    p_norm = math.hypot(*p_lateral_xy)
    dot = u_along_xy[0] * p_lateral_xy[0] + u_along_xy[1] * p_lateral_xy[1]
    determinant = u_along_xy[0] * p_lateral_xy[1] - u_along_xy[1] * p_lateral_xy[0]
    if not math.isclose(u_norm, 1.0, rel_tol=0.0, abs_tol=_BASIS_TOLERANCE):
        raise SafeFrameValidationError(f"u_along_xy is not unit length: {u_norm!r}")
    if not math.isclose(p_norm, 1.0, rel_tol=0.0, abs_tol=_BASIS_TOLERANCE):
        raise SafeFrameValidationError(f"p_lateral_xy is not unit length: {p_norm!r}")
    if not math.isclose(dot, 0.0, rel_tol=0.0, abs_tol=_BASIS_TOLERANCE):
        raise SafeFrameValidationError(f"Step6 XY basis is not orthogonal: dot={dot!r}")
    if not math.isclose(determinant, 1.0, rel_tol=0.0, abs_tol=_BASIS_TOLERANCE):
        raise SafeFrameValidationError(f"Step6 XY basis orientation is not positive: det={determinant!r}")


@dataclass(frozen=True, slots=True)
class InheritedXYFrame:
    """Immutable, already-validated XY frame data from the inherited source."""

    source_path: str
    source_sha256: str
    origin_xy_m: tuple[float, float]
    u_along_xy: tuple[float, float]
    p_lateral_xy: tuple[float, float]

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, str) or not self.source_path:
            raise SafeFrameValidationError("source_path must be a non-empty string")
        if not isinstance(self.source_sha256, str) or len(self.source_sha256) != 64:
            raise SafeFrameValidationError("source_sha256 must be a 64-character hex digest")
        try:
            int(self.source_sha256, 16)
        except ValueError as exc:
            raise SafeFrameValidationError("source_sha256 must be hexadecimal") from exc
        for label, vector in (
            ("origin_xy_m", self.origin_xy_m),
            ("u_along_xy", self.u_along_xy),
            ("p_lateral_xy", self.p_lateral_xy),
        ):
            if not isinstance(vector, tuple) or len(vector) != 2:
                raise SafeFrameValidationError(f"{label} must be an immutable two-value tuple")
        _validate_xy_basis(self.origin_xy_m, self.u_along_xy, self.p_lateral_xy)


def _rotation_matrix(value: object) -> tuple[tuple[float, float], tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SafeFrameValidationError("basis.rotation_matrix must be a 2x2 matrix")
    rows: list[tuple[float, float]] = []
    for row_index, row in enumerate(value):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise SafeFrameValidationError("basis.rotation_matrix must be a 2x2 matrix")
        try:
            rows.append(tuple(require_finite_float(item, f"basis.rotation_matrix[{row_index}]") for item in row))
        except ContractInvariantError as exc:
            raise SafeFrameValidationError(str(exc)) from exc
    return (rows[0], rows[1])


def load_inherited_xy_frame(
    path: Path = DEFAULT_SAFE_FRAME_PATH,
    *,
    expected_sha256: str = SAFE_FRAME_SHA256,
) -> InheritedXYFrame:
    """Load the exact inherited source, verify its digest, schema, and XY geometry."""

    if expected_sha256 != STEP6_R001_CONTRACT.safe_frame_sha256:
        raise SafeFrameValidationError(
            "the Step6 r001 loader is locked to the inherited safe-frame SHA-256"
        )
    if not isinstance(path, Path):
        raise SafeFrameValidationError(f"safe-frame path must be a pathlib.Path, got {path!r}")
    if not path.is_file():
        raise SafeFrameValidationError(f"missing inherited safe-frame source: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SafeFrameValidationError(f"cannot read inherited safe-frame source: {path}") from exc
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise SafeFrameValidationError(
            "inherited safe-frame SHA-256 mismatch: "
            f"expected={expected_sha256}, actual={actual_sha256}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeFrameValidationError("inherited safe-frame source is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise SafeFrameValidationError("inherited safe-frame root must be an object")
    if payload.get("status") != "ok":
        raise SafeFrameValidationError("inherited safe-frame status must be 'ok'")
    if payload.get("version") != 1:
        raise SafeFrameValidationError("inherited safe-frame version must be 1")
    basis = payload.get("basis")
    if not isinstance(basis, Mapping):
        raise SafeFrameValidationError("inherited safe-frame basis object is required")
    origin_xy_m = _vector(basis.get("origin_xy_m"), "basis.origin_xy_m")
    u_along_xy = _vector(basis.get("u_along_xy"), "basis.u_along_xy")
    p_lateral_xy = _vector(basis.get("p_lateral_xy"), "basis.p_lateral_xy")
    rotation_matrix = _rotation_matrix(basis.get("rotation_matrix"))
    expected_rotation_matrix = (
        (u_along_xy[0], p_lateral_xy[0]),
        (u_along_xy[1], p_lateral_xy[1]),
    )
    for row_index in range(2):
        for column_index in range(2):
            if not math.isclose(
                rotation_matrix[row_index][column_index],
                expected_rotation_matrix[row_index][column_index],
                rel_tol=0.0,
                abs_tol=_BASIS_TOLERANCE,
            ):
                raise SafeFrameValidationError(
                    "basis.rotation_matrix does not match the declared XY basis"
                )
    return InheritedXYFrame(
        source_path=str(path.resolve()),
        source_sha256=actual_sha256,
        origin_xy_m=origin_xy_m,
        u_along_xy=u_along_xy,
        p_lateral_xy=p_lateral_xy,
    )


load_step6_r001_frame = load_inherited_xy_frame
