"""Immutable, finite-validated contracts for the modular tube core.

This module intentionally contains no runtime feedback, transport, ROS, or
controller types.  The objects here are small value contracts whose canonical
identities can be used by a producer/consumer boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence


class TubeContractError(ValueError):
    """Base error for invalid modular-tube contract data."""


class IdentityError(TubeContractError):
    """Raised when an identity is malformed or has drifted."""


def _finite_float(value: Any, *, name: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TubeContractError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise TubeContractError(f"{name} must be finite")
    if positive and result <= 0.0:
        raise TubeContractError(f"{name} must be positive")
    return 0.0 if result == 0.0 else result


def _nonnegative_float(value: Any, *, name: str) -> float:
    result = _finite_float(value, name=name)
    if result < 0.0:
        raise TubeContractError(f"{name} must be non-negative")
    return result


def _positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TubeContractError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TubeContractError(f"{name} must be a non-negative integer")
    return value


def _text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise TubeContractError(f"{name} must be a non-empty canonical string")
    return value


def _sha256(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IdentityError(f"{name} must be a lowercase SHA-256")
    return value


def _tuple3(values: Any, *, name: str, nonnegative: bool = False) -> tuple[float, float, float]:
    if isinstance(values, (str, bytes)):
        raise TubeContractError(f"{name} must contain three finite numbers")
    try:
        material = tuple(values)
    except TypeError as exc:
        raise TubeContractError(f"{name} must contain three finite numbers") from exc
    if len(material) != 3:
        raise TubeContractError(f"{name} must contain exactly three numbers")
    result = tuple(
        _finite_float(item, name=f"{name}[{index}]")
        for index, item in enumerate(material)
    )
    if nonnegative and any(item < 0.0 for item in result):
        raise TubeContractError(f"{name} must be non-negative")
    return result  # type: ignore[return-value]


def _tuple4(values: Any, *, name: str) -> tuple[float, float, float, float]:
    if isinstance(values, (str, bytes)):
        raise TubeContractError(f"{name} must contain four finite numbers")
    try:
        material = tuple(values)
    except TypeError as exc:
        raise TubeContractError(f"{name} must contain four finite numbers") from exc
    if len(material) != 4:
        raise TubeContractError(f"{name} must contain exactly four numbers")
    result = tuple(
        _finite_float(item, name=f"{name}[{index}]")
        for index, item in enumerate(material)
    )
    return result  # type: ignore[return-value]


def _normalize_quaternion(values: Any, *, name: str) -> tuple[float, float, float, float]:
    qx, qy, qz, qw = _tuple4(values, name=name)
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if not math.isfinite(norm) or norm <= 0.0:
        raise TubeContractError(f"{name} must have non-zero finite norm")
    normalized = (qx / norm, qy / norm, qz / norm, qw / norm)
    # q and -q represent the same rotation.  This makes the identity
    # canonical instead of depending on the caller's quaternion sign.
    if normalized[3] < 0.0 or (
        normalized[3] == 0.0
        and next((item for item in normalized[:3] if item != 0.0), 0.0) < 0.0
    ):
        normalized = tuple(-item for item in normalized)  # type: ignore[assignment]
    return normalized  # type: ignore[return-value]


def _canonical_value(value: Any) -> Any:
    """Return a JSON-compatible canonical value or reject it.

    The helper deliberately does not stringify unknown objects.  A producer
    must bind explicit value contracts rather than whatever a runtime object
    happens to expose through ``repr``.
    """

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TubeContractError("canonical data cannot contain non-finite floats")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TubeContractError("canonical mapping keys must be strings")
            result[key] = _canonical_value(item)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    raise TubeContractError(
        f"unsupported canonical data type {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Encode canonical JSON using only deterministic standard-library rules."""

    canonical = _canonical_value(value)
    try:
        return json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive boundary
        raise TubeContractError("value is not canonical JSON data") from exc


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of canonical JSON data."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identity_document_sha256(document: Mapping[str, Any]) -> str:
    return canonical_sha256(document)


def _identity_of(value: Any, *, name: str) -> str:
    if isinstance(value, str):
        return _sha256(value, name=name)
    identity = getattr(value, "sha256", None)
    if not isinstance(identity, str):
        identity = getattr(value, "identity_sha256", None)
    return _sha256(identity, name=name)


def _rotation_matrix(quaternion_xyzw: tuple[float, float, float, float]) -> tuple[float, ...]:
    qx, qy, qz, qw = quaternion_xyzw
    return (
        1.0 - 2.0 * (qy * qy + qz * qz),
        2.0 * (qx * qy - qz * qw),
        2.0 * (qx * qz + qy * qw),
        2.0 * (qx * qy + qz * qw),
        1.0 - 2.0 * (qx * qx + qz * qz),
        2.0 * (qy * qz - qx * qw),
        2.0 * (qx * qz - qy * qw),
        2.0 * (qy * qz + qx * qw),
        1.0 - 2.0 * (qx * qx + qy * qy),
    )


def _mat_vec(matrix: tuple[float, ...], vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        matrix[0] * vector[0] + matrix[1] * vector[1] + matrix[2] * vector[2],
        matrix[3] * vector[0] + matrix[4] * vector[1] + matrix[5] * vector[2],
        matrix[6] * vector[0] + matrix[7] * vector[1] + matrix[8] * vector[2],
    )


def _mat_transpose_vec(matrix: tuple[float, ...], vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        matrix[0] * vector[0] + matrix[3] * vector[1] + matrix[6] * vector[2],
        matrix[1] * vector[0] + matrix[4] * vector[1] + matrix[7] * vector[2],
        matrix[2] * vector[0] + matrix[5] * vector[1] + matrix[8] * vector[2],
    )


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _sub(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(_dot(vector, vector))


@dataclass(frozen=True, slots=True, init=False)
class Pose3V1:
    """A finite SE(3) pose with canonical quaternion sign."""

    position_m: tuple[float, float, float]
    quaternion_xyzw: tuple[float, float, float, float]
    frame_id: str
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __init__(
        self,
        position_m: Sequence[float] | None = None,
        quaternion_xyzw: Sequence[float] | None = None,
        frame_id: str = "base",
        *,
        position: Sequence[float] | None = None,
        orientation: Sequence[float] | None = None,
        translation_m: Sequence[float] | None = None,
        rotation_xyzw: Sequence[float] | None = None,
        x_m: float | None = None,
        y_m: float | None = None,
        z_m: float | None = None,
        qx: float | None = None,
        qy: float | None = None,
        qz: float | None = None,
        qw: float | None = None,
    ) -> None:
        selected_position = position_m
        for alias in (position, translation_m):
            if alias is not None:
                if selected_position is not None and tuple(selected_position) != tuple(alias):
                    raise TubeContractError("Pose3V1 received conflicting position aliases")
                selected_position = alias
        if selected_position is None and any(value is not None for value in (x_m, y_m, z_m)):
            if any(value is None for value in (x_m, y_m, z_m)):
                raise TubeContractError("x_m, y_m and z_m must be supplied together")
            selected_position = (x_m, y_m, z_m)  # type: ignore[assignment]
        if selected_position is None:
            selected_position = (0.0, 0.0, 0.0)

        selected_quaternion = quaternion_xyzw
        for alias in (orientation, rotation_xyzw):
            if alias is not None:
                if selected_quaternion is not None and tuple(selected_quaternion) != tuple(alias):
                    raise TubeContractError("Pose3V1 received conflicting orientation aliases")
                selected_quaternion = alias
        scalar_quaternion = (qx, qy, qz, qw)
        if any(value is not None for value in scalar_quaternion):
            if any(value is None for value in scalar_quaternion):
                raise TubeContractError("qx, qy, qz and qw must be supplied together")
            if selected_quaternion is not None and tuple(selected_quaternion) != scalar_quaternion:
                raise TubeContractError("Pose3V1 received conflicting quaternion aliases")
            selected_quaternion = scalar_quaternion  # type: ignore[assignment]
        if selected_quaternion is None:
            selected_quaternion = (0.0, 0.0, 0.0, 1.0)

        position_tuple = _tuple3(selected_position, name="position_m")
        quaternion_tuple = _normalize_quaternion(
            selected_quaternion,
            name="quaternion_xyzw",
        )
        frame = _text(frame_id, name="frame_id")
        document = {
            "schema": "ur10e.modular-tube/pose3/v1",
            "frame_id": frame,
            "position_m": position_tuple,
            "quaternion_xyzw": quaternion_tuple,
        }
        object.__setattr__(self, "position_m", position_tuple)
        object.__setattr__(self, "quaternion_xyzw", quaternion_tuple)
        object.__setattr__(self, "frame_id", frame)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

    @property
    def x_m(self) -> float:
        return self.position_m[0]

    @property
    def y_m(self) -> float:
        return self.position_m[1]

    @property
    def z_m(self) -> float:
        return self.position_m[2]

    @property
    def qx(self) -> float:
        return self.quaternion_xyzw[0]

    @property
    def qy(self) -> float:
        return self.quaternion_xyzw[1]

    @property
    def qz(self) -> float:
        return self.quaternion_xyzw[2]

    @property
    def qw(self) -> float:
        return self.quaternion_xyzw[3]

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def fingerprint(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/pose3/v1",
            "frame_id": self.frame_id,
            "position_m": list(self.position_m),
            "quaternion_xyzw": list(self.quaternion_xyzw),
        }

    def rotation_matrix(self) -> tuple[float, ...]:
        return _rotation_matrix(self.quaternion_xyzw)

    def transform_point(self, point_tool_m: Sequence[float]) -> tuple[float, float, float]:
        point = _tuple3(point_tool_m, name="point_tool_m")
        rotated = _mat_vec(self.rotation_matrix(), point)
        return _tuple3(
            (
                self.position_m[0] + rotated[0],
                self.position_m[1] + rotated[1],
                self.position_m[2] + rotated[2],
            ),
            name="transformed point",
        )


@dataclass(frozen=True, slots=True, init=False)
class EoatProxyV1:
    """A conservative tool-frame oriented bounding box proxy.

    ``T_tool_proxy`` is the pose of the OBB center/frame in tool coordinates.
    ``None`` is an explicit unqualified/offline-evidence value.  It never
    means an identity transform and cannot be registered or evaluated.
    """

    source_sha256: str
    proxy_id: str
    source_frame: str
    half_extents_m: tuple[float, float, float]
    T_tool_proxy: Pose3V1 | None
    scope_exclusions: tuple[str, ...]
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __init__(
        self,
        source_sha256: str | None = None,
        proxy_id: str = "",
        source_frame: str = "",
        half_extents_m: Sequence[float] | None = None,
        T_tool_proxy: Pose3V1 | None = None,
        scope_exclusions: Sequence[str] = (),
        *,
        source_id: str | None = None,
        t_tool_proxy: Pose3V1 | None = None,
        obb_half_extents_m: Sequence[float] | None = None,
        half_sizes_m: Sequence[float] | None = None,
    ) -> None:
        if source_sha256 is not None and source_id is not None and source_sha256 != source_id:
            raise IdentityError("EoatProxyV1 received conflicting source hash aliases")
        selected_source_sha256 = source_sha256 if source_sha256 is not None else source_id
        if T_tool_proxy is not None and t_tool_proxy is not None and T_tool_proxy != t_tool_proxy:
            raise TubeContractError("EoatProxyV1 received conflicting T_tool_proxy aliases")
        transform = T_tool_proxy if T_tool_proxy is not None else t_tool_proxy
        selected_extents = half_extents_m
        for alias in (obb_half_extents_m, half_sizes_m):
            if alias is not None:
                if selected_extents is not None and tuple(selected_extents) != tuple(alias):
                    raise TubeContractError("EoatProxyV1 received conflicting extent aliases")
                selected_extents = alias
        if selected_extents is None:
            raise TubeContractError("half_extents_m is required")
        source = _sha256(selected_source_sha256, name="source_sha256")
        proxy = _text(proxy_id, name="proxy_id")
        frame = _text(source_frame, name="source_frame")
        extents = _tuple3(selected_extents, name="half_extents_m")
        if any(item <= 0.0 for item in extents):
            raise TubeContractError("half_extents_m must be strictly positive")
        exclusions = tuple(scope_exclusions)
        if any(not isinstance(item, str) or not item or item.strip() != item for item in exclusions):
            raise TubeContractError("scope_exclusions must contain canonical strings")
        if len(set(exclusions)) != len(exclusions):
            raise TubeContractError("scope_exclusions must not contain duplicates")
        if transform is not None and not isinstance(transform, Pose3V1):
            raise TubeContractError("T_tool_proxy must be a Pose3V1 or None")
        document = {
            "schema": "ur10e.modular-tube/eoat-proxy/v1",
            "source_sha256": source,
            "proxy_id": proxy,
            "source_frame": frame,
            "half_extents_m": extents,
            "T_tool_proxy": None if transform is None else transform.canonical_document(),
            "scope_exclusions": exclusions,
        }
        object.__setattr__(self, "source_sha256", source)
        object.__setattr__(self, "proxy_id", proxy)
        object.__setattr__(self, "source_frame", frame)
        object.__setattr__(self, "half_extents_m", extents)
        object.__setattr__(self, "T_tool_proxy", transform)
        object.__setattr__(self, "scope_exclusions", exclusions)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

    @property
    def t_tool_proxy(self) -> Pose3V1 | None:
        return self.T_tool_proxy

    @property
    def source_id(self) -> str:
        """Compatibility alias for the validated source SHA-256."""

        return self.source_sha256

    @property
    def obb_half_extents_m(self) -> tuple[float, float, float]:
        return self.half_extents_m

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def proxy_sha256(self) -> str:
        """Compatibility alias for the complete canonical proxy identity."""

        return self._identity_sha256

    @property
    def fingerprint(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/eoat-proxy/v1",
            "source_sha256": self.source_sha256,
            "proxy_id": self.proxy_id,
            "source_frame": self.source_frame,
            "half_extents_m": list(self.half_extents_m),
            "T_tool_proxy": (
                None
                if self.T_tool_proxy is None
                else self.T_tool_proxy.canonical_document()
            ),
            "scope_exclusions": list(self.scope_exclusions),
        }

    def support_m(self, direction_xyz: Sequence[float]) -> float:
        """Return the OBB support function ``max(dot(direction, point))``."""

        direction = _tuple3(direction_xyz, name="support direction")
        if direction == (0.0, 0.0, 0.0):
            return 0.0
        transform = self.T_tool_proxy
        if transform is None:
            raise TubeContractError(
                "T_tool_proxy=None is unqualified and cannot provide an active support function"
            )
        center = transform.position_m
        rotation = transform.rotation_matrix()
        local_direction = _mat_transpose_vec(rotation, direction)
        return _dot(direction, center) + sum(
            extent * abs(local_direction[index])
            for index, extent in enumerate(self.half_extents_m)
        )

    def support_bound_m(self, direction_xyz: Sequence[float]) -> float:
        direction = _tuple3(direction_xyz, name="support direction")
        return max(abs(self.support_m(direction)), abs(self.support_m(tuple(-item for item in direction))))

    def orientation_displacement_components_m(
        self,
        actual_pose: Pose3V1,
        reference_pose: Pose3V1,
    ) -> tuple[float, float, float]:
        """Conservatively bound OBB displacement in reference-frame axes.

        Each component is obtained from the OBB support function.  Taking the
        Euclidean norm of the component bounds is conservative for the vector
        orientation displacement and, unlike an angle-only check, captures an
        offset OBB center through ``T_tool_proxy``.
        """

        if not isinstance(actual_pose, Pose3V1) or not isinstance(reference_pose, Pose3V1):
            raise TubeContractError("orientation displacement requires Pose3V1 values")
        actual_rotation = actual_pose.rotation_matrix()
        reference_rotation = reference_pose.rotation_matrix()
        components: list[float] = []
        for axis_index in range(3):
            axis = tuple(
                reference_rotation[row * 3 + axis_index] for row in range(3)
            )
            actual_local_direction = _mat_transpose_vec(actual_rotation, axis)
            reference_local_direction = _mat_transpose_vec(reference_rotation, axis)
            direction = tuple(
                actual_local_direction[index] - reference_local_direction[index]
                for index in range(3)
            )
            components.append(self.support_bound_m(direction))
        result = tuple(components)
        if not all(math.isfinite(item) for item in result):  # pragma: no cover - constructor proof
            raise TubeContractError("orientation support bound is non-finite")
        return result  # type: ignore[return-value]

    def orientation_displacement_bound_m(
        self,
        actual_pose: Pose3V1,
        reference_pose: Pose3V1,
    ) -> float:
        components = self.orientation_displacement_components_m(actual_pose, reference_pose)
        return _norm(components)


@dataclass(frozen=True, slots=True, init=False)
class ReferencePoseSampleV1:
    """One immutable reference pose sample in a swept reference."""

    sample_id: str
    pose: Pose3V1
    along_m: float
    source_frame: str
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __init__(
        self,
        sample_id: str | None = None,
        pose: Pose3V1 | None = None,
        along_m: float = 0.0,
        source_frame: str | None = None,
        *,
        reference_sample_id: str | None = None,
        sample_uid: str | None = None,
        reference_pose: Pose3V1 | None = None,
        s_m: float | None = None,
    ) -> None:
        id_aliases = tuple(
            value for value in (sample_id, reference_sample_id, sample_uid)
            if value is not None
        )
        if len(set(id_aliases)) > 1:
            raise TubeContractError("ReferencePoseSampleV1 received conflicting ID aliases")
        selected_id = id_aliases[0] if id_aliases else None
        if selected_id is None:
            raise TubeContractError("sample_id is required")
        selected_pose = pose if pose is not None else reference_pose
        if selected_pose is None or not isinstance(selected_pose, Pose3V1):
            raise TubeContractError("pose must be a Pose3V1")
        if s_m is not None:
            if along_m != 0.0 and float(along_m) != float(s_m):
                raise TubeContractError("ReferencePoseSampleV1 received conflicting along aliases")
            along_m = s_m
        identifier = _text(selected_id, name="sample_id")
        along = _finite_float(along_m, name="along_m")
        frame = selected_pose.frame_id if source_frame is None else _text(source_frame, name="source_frame")
        document = {
            "schema": "ur10e.modular-tube/reference-sample/v1",
            "sample_id": identifier,
            "pose": selected_pose.canonical_document(),
            "along_m": along,
            "source_frame": frame,
        }
        object.__setattr__(self, "sample_id", identifier)
        object.__setattr__(self, "pose", selected_pose)
        object.__setattr__(self, "along_m", along)
        object.__setattr__(self, "source_frame", frame)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

    @property
    def reference_sample_id(self) -> str:
        return self.sample_id

    @property
    def sample_uid(self) -> str:
        return self.sample_id

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def fingerprint(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/reference-sample/v1",
            "sample_id": self.sample_id,
            "pose": self.pose.canonical_document(),
            "along_m": self.along_m,
            "source_frame": self.source_frame,
        }


@dataclass(frozen=True, slots=True, init=False)
class SweptReferenceV1:
    """A non-empty, immutable ordered collection of reference samples."""

    samples: tuple[ReferencePoseSampleV1, ...]
    reference_id: str
    source_frame: str
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __init__(
        self,
        samples: Sequence[ReferencePoseSampleV1],
        reference_id: str = "swept-reference-v1",
        source_frame: str | None = None,
        *,
        reference_uid: str | None = None,
    ) -> None:
        material = tuple(samples)
        if not material:
            raise TubeContractError("SweptReferenceV1 requires at least one sample")
        if any(not isinstance(item, ReferencePoseSampleV1) for item in material):
            raise TubeContractError("all swept-reference samples must be ReferencePoseSampleV1")
        sample_ids = tuple(item.sample_id for item in material)
        if len(set(sample_ids)) != len(sample_ids):
            raise TubeContractError("swept-reference sample IDs must be unique")
        identifier = reference_id if reference_uid is None else reference_uid
        identifier = _text(identifier, name="reference_id")
        selected_frame = material[0].source_frame if source_frame is None else _text(source_frame, name="source_frame")
        if any(item.source_frame != selected_frame for item in material):
            raise TubeContractError("all swept-reference samples must use one source frame")
        document = {
            "schema": "ur10e.modular-tube/swept-reference/v1",
            "reference_id": identifier,
            "source_frame": selected_frame,
            "samples": [item.canonical_document() for item in material],
        }
        object.__setattr__(self, "samples", material)
        object.__setattr__(self, "reference_id", identifier)
        object.__setattr__(self, "source_frame", selected_frame)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

    @property
    def reference_uid(self) -> str:
        return self.reference_id

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    @property
    def reference_sha256(self) -> str:
        return self._identity_sha256

    @property
    def fingerprint(self) -> str:
        return self._identity_sha256

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/swept-reference/v1",
            "reference_id": self.reference_id,
            "source_frame": self.source_frame,
            "samples": [item.canonical_document() for item in self.samples],
        }

    def closest_sample(self, actual_pose: Pose3V1) -> ReferencePoseSampleV1:
        if not isinstance(actual_pose, Pose3V1):
            raise TubeContractError("actual_pose must be a Pose3V1")
        best_sample = self.samples[0]
        best_distance = math.inf
        for sample in self.samples:
            displacement = _sub(actual_pose.position_m, sample.pose.position_m)
            distance = _dot(displacement, displacement)
            if distance < best_distance:
                best_distance = distance
                best_sample = sample
        return best_sample

    def select(self, actual_pose: Pose3V1) -> ReferencePoseSampleV1:
        return self.closest_sample(actual_pose)


@dataclass(frozen=True, slots=True)
class ProxyRegistryV1:
    """Immutable activation registry for EOAT proxies.

    ``register`` returns a new registry.  A constructed proxy is never
    implicitly registered, and a registry entry is content-addressed by the
    complete proxy identity rather than by a mutable proxy ID.
    """

    proxies: tuple[EoatProxyV1, ...] = ()
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __post_init__(self) -> None:
        material = tuple(self.proxies)
        if any(not isinstance(item, EoatProxyV1) for item in material):
            raise TubeContractError("ProxyRegistryV1 accepts only EoatProxyV1 values")
        if any(item.T_tool_proxy is None for item in material):
            raise TubeContractError("ProxyRegistryV1 cannot register an unqualified proxy")
        by_id: dict[str, str] = {}
        for proxy in material:
            prior = by_id.get(proxy.proxy_id)
            if prior is not None and prior != proxy.sha256:
                raise IdentityError(f"proxy ID {proxy.proxy_id!r} is registered with drift")
            by_id[proxy.proxy_id] = proxy.sha256
        canonical = {
            "schema": "ur10e.modular-tube/proxy-registry/v1",
            "proxies": [item.canonical_document() for item in material],
        }
        object.__setattr__(self, "proxies", material)
        object.__setattr__(self, "_identity_sha256", canonical_sha256(canonical))

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    def register(self, proxy: EoatProxyV1) -> "ProxyRegistryV1":
        if not isinstance(proxy, EoatProxyV1):
            raise TubeContractError("only EoatProxyV1 can be registered")
        if proxy.T_tool_proxy is None:
            raise TubeContractError("ProxyRegistryV1 cannot register an unqualified proxy")
        for current in self.proxies:
            if current.proxy_id == proxy.proxy_id:
                if current.sha256 == proxy.sha256:
                    return self
                raise IdentityError(f"proxy ID {proxy.proxy_id!r} cannot be rebound")
        return ProxyRegistryV1(self.proxies + (proxy,))

    def add(self, proxy: EoatProxyV1) -> "ProxyRegistryV1":
        return self.register(proxy)

    def contains(self, proxy: EoatProxyV1 | str) -> bool:
        if isinstance(proxy, EoatProxyV1) and proxy.T_tool_proxy is None:
            return False
        identity = proxy.sha256 if isinstance(proxy, EoatProxyV1) else _sha256(proxy, name="proxy_sha256")
        return any(item.sha256 == identity for item in self.proxies)

    def is_registered(self, proxy: EoatProxyV1 | str) -> bool:
        return self.contains(proxy)

    def resolve(self, proxy_sha256: str) -> EoatProxyV1 | None:
        identity = _sha256(proxy_sha256, name="proxy_sha256")
        for proxy in self.proxies:
            if proxy.sha256 == identity:
                return proxy
        return None


EoatProxyRegistryV1 = ProxyRegistryV1


__all__ = [
    "EoatProxyRegistryV1",
    "EoatProxyV1",
    "IdentityError",
    "Pose3V1",
    "ProxyRegistryV1",
    "ReferencePoseSampleV1",
    "SweptReferenceV1",
    "TubeContractError",
    "canonical_json_bytes",
    "canonical_sha256",
]
