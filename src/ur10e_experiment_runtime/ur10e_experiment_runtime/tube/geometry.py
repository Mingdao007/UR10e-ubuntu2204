"""Pure signed-distance geometry for the modular tube core.

The geometry layer is deliberately independent of sequence numbers,
timestamps, freshness, or controller progress.  It consumes only a pose, an
EOAT proxy, and a frozen swept reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Protocol, Sequence, runtime_checkable

from .contracts import (
    EoatProxyV1,
    Pose3V1,
    ReferencePoseSampleV1,
    SweptReferenceV1,
    TubeContractError,
    _canonical_value,
    _dot,
    _finite_float,
    _identity_document_sha256,
    _mat_transpose_vec,
    _norm,
    _sha256,
    _sub,
    _text,
    _tuple3,
    canonical_sha256,
)


class GeometryError(TubeContractError):
    """Raised when a geometry strategy or solver input is invalid."""


def _positive_bounds(values: Any, *, name: str) -> tuple[float, float, float]:
    try:
        material = tuple(values)
    except TypeError as exc:
        raise GeometryError(f"{name} must contain three positive bounds") from exc
    if len(material) != 3:
        raise GeometryError(f"{name} must contain exactly three positive bounds")
    return tuple(
        _finite_float(item, name=f"{name}[{index}]", positive=True)
        for index, item in enumerate(material)
    )  # type: ignore[return-value]


def _pose_displacements(
    actual_pose: Pose3V1,
    proxy: EoatProxyV1,
    reference_pose: Pose3V1,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    translation_world = _sub(actual_pose.position_m, reference_pose.position_m)
    reference_rotation = reference_pose.rotation_matrix()
    translation_reference = _mat_transpose_vec(reference_rotation, translation_world)
    orientation_components = proxy.orientation_displacement_components_m(
        actual_pose,
        reference_pose,
    )
    if not all(math.isfinite(item) for item in (*translation_reference, *orientation_components)):
        raise GeometryError("geometry solver input produced non-finite displacement")
    return translation_reference, orientation_components


def _circle_pose_field(
    radius_m: float,
    actual_pose: Pose3V1,
    proxy: EoatProxyV1,
    reference_pose: Pose3V1,
) -> float:
    translation_reference, orientation_components = _pose_displacements(
        actual_pose,
        proxy,
        reference_pose,
    )
    return _norm(translation_reference) + _norm(orientation_components) - radius_m


def _ellipse_pose_field(
    bounds_m: tuple[float, float, float],
    actual_pose: Pose3V1,
    proxy: EoatProxyV1,
    reference_pose: Pose3V1,
) -> float:
    translation_reference, orientation_components = _pose_displacements(
        actual_pose,
        proxy,
        reference_pose,
    )
    normalized_squared = sum(
        (
            (abs(translation_reference[index]) + orientation_components[index])
            / bounds_m[index]
        )
        ** 2
        for index in range(3)
    )
    return min(bounds_m) * (math.sqrt(normalized_squared) - 1.0)


@runtime_checkable
class SdfLeafV1(Protocol):
    """Protocol for finite leaf constraints accepted by a composite root."""

    constraint_id: str
    sha256: str

    def evaluate_point(self, point_reference_m: tuple[float, float, float]) -> float:
        """Return a signed field in metres, negative on the allowed side."""


@dataclass(frozen=True, slots=True, init=False)
class HalfSpaceConstraintV1:
    """Allowed half-space ``dot(normal, point) <= offset``."""

    constraint_id: str
    normal_xyz: tuple[float, float, float]
    offset_m: float
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __init__(
        self,
        constraint_id: str,
        normal_xyz: Sequence[float] | None = None,
        offset_m: float | None = None,
        *,
        normal: Sequence[float] | None = None,
        offset: float | None = None,
    ) -> None:
        selected_normal = normal_xyz if normal_xyz is not None else normal
        if selected_normal is None:
            raise GeometryError("normal_xyz is required")
        if normal_xyz is not None and normal is not None and tuple(normal_xyz) != tuple(normal):
            raise GeometryError("HalfSpaceConstraintV1 received conflicting normal aliases")
        selected_offset = offset_m if offset_m is not None else offset
        if selected_offset is None:
            raise GeometryError("offset_m is required")
        if offset_m is not None and offset is not None and float(offset_m) != float(offset):
            raise GeometryError("HalfSpaceConstraintV1 received conflicting offset aliases")
        identifier = _text(constraint_id, name="constraint_id")
        raw_normal = _tuple3(selected_normal, name="normal_xyz")
        norm = _norm(raw_normal)
        if norm <= 0.0 or not math.isfinite(norm):
            raise GeometryError("normal_xyz must have non-zero finite norm")
        unit_normal = tuple(item / norm for item in raw_normal)
        normalized_offset = _finite_float(selected_offset, name="offset_m") / norm
        document = {
            "schema": "ur10e.modular-tube/half-space/v1",
            "constraint_id": identifier,
            "normal_xyz": unit_normal,
            "offset_m": normalized_offset,
        }
        object.__setattr__(self, "constraint_id", identifier)
        object.__setattr__(self, "normal_xyz", unit_normal)
        object.__setattr__(self, "offset_m", normalized_offset)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/half-space/v1",
            "constraint_id": self.constraint_id,
            "normal_xyz": list(self.normal_xyz),
            "offset_m": self.offset_m,
        }

    def evaluate_point(self, point_reference_m: tuple[float, float, float]) -> float:
        point = _tuple3(point_reference_m, name="point_reference_m")
        result = _dot(self.normal_xyz, point) - self.offset_m
        if not math.isfinite(result):  # pragma: no cover - constructor proof
            raise GeometryError("half-space result is non-finite")
        return result


@dataclass(frozen=True, slots=True, init=False)
class ExclusionBoxConstraintV1:
    """Forbidden axis-aligned box represented as a positive violation field.

    Inside the exclusion box the result is the positive distance to the
    nearest face; outside it is the negative Euclidean distance to the box.
    This makes it safe to combine with other *intersection* constraints using
    ``max(child)`` without adding a union/negation operator.
    """

    constraint_id: str
    minimum_xyz: tuple[float, float, float]
    maximum_xyz: tuple[float, float, float]
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    def __init__(
        self,
        constraint_id: str,
        minimum_xyz: Sequence[float] | None = None,
        maximum_xyz: Sequence[float] | None = None,
        *,
        min_xyz: Sequence[float] | None = None,
        max_xyz: Sequence[float] | None = None,
        lower_m: Sequence[float] | None = None,
        upper_m: Sequence[float] | None = None,
    ) -> None:
        selected_min = minimum_xyz
        selected_max = maximum_xyz
        for alias in (min_xyz, lower_m):
            if alias is not None:
                if selected_min is not None and tuple(selected_min) != tuple(alias):
                    raise GeometryError("ExclusionBoxConstraintV1 received conflicting minimum aliases")
                selected_min = alias
        for alias in (max_xyz, upper_m):
            if alias is not None:
                if selected_max is not None and tuple(selected_max) != tuple(alias):
                    raise GeometryError("ExclusionBoxConstraintV1 received conflicting maximum aliases")
                selected_max = alias
        if selected_min is None or selected_max is None:
            raise GeometryError("minimum_xyz and maximum_xyz are required")
        identifier = _text(constraint_id, name="constraint_id")
        minimum = _tuple3(selected_min, name="minimum_xyz")
        maximum = _tuple3(selected_max, name="maximum_xyz")
        if any(low >= high for low, high in zip(minimum, maximum)):
            raise GeometryError("exclusion box minimum must be less than maximum")
        document = {
            "schema": "ur10e.modular-tube/exclusion-box/v1",
            "constraint_id": identifier,
            "minimum_xyz": minimum,
            "maximum_xyz": maximum,
        }
        object.__setattr__(self, "constraint_id", identifier)
        object.__setattr__(self, "minimum_xyz", minimum)
        object.__setattr__(self, "maximum_xyz", maximum)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

    @property
    def min_xyz(self) -> tuple[float, float, float]:
        return self.minimum_xyz

    @property
    def max_xyz(self) -> tuple[float, float, float]:
        return self.maximum_xyz

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/exclusion-box/v1",
            "constraint_id": self.constraint_id,
            "minimum_xyz": list(self.minimum_xyz),
            "maximum_xyz": list(self.maximum_xyz),
        }

    def evaluate_point(self, point_reference_m: tuple[float, float, float]) -> float:
        point = _tuple3(point_reference_m, name="point_reference_m")
        inside = all(
            low <= value <= high
            for value, low, high in zip(point, self.minimum_xyz, self.maximum_xyz)
        )
        if inside:
            return min(
                min(value - low, high - value)
                for value, low, high in zip(point, self.minimum_xyz, self.maximum_xyz)
            )
        squared = 0.0
        for value, low, high in zip(point, self.minimum_xyz, self.maximum_xyz):
            distance = max(low - value, 0.0, value - high)
            squared += distance * distance
        return -math.sqrt(squared)


@dataclass(frozen=True, slots=True, init=False)
class CircleSdfV1:
    """Isotropic 3-D pose-tube strategy in metres."""

    radius_m: float
    constraint_id: str
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    strategy_id = "circle_sdf_v1"

    def __init__(
        self,
        radius_m: float | None = None,
        *,
        threshold_m: float | None = None,
        constraint_id: str | None = None,
    ) -> None:
        if radius_m is not None and threshold_m is not None and float(radius_m) != float(threshold_m):
            raise GeometryError("CircleSdfV1 received conflicting radius aliases")
        selected = radius_m if radius_m is not None else threshold_m
        if selected is None:
            raise GeometryError("radius_m is required")
        radius = _finite_float(selected, name="radius_m", positive=True)
        identifier = _text(
            self.strategy_id if constraint_id is None else constraint_id,
            name="constraint_id",
        )
        document = {
            "schema": "ur10e.modular-tube/circle-sdf/v1",
            "strategy": self.strategy_id,
            "radius_m": radius,
            "constraint_id": identifier,
        }
        object.__setattr__(self, "radius_m", radius)
        object.__setattr__(self, "constraint_id", identifier)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

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
    def bounds_m(self) -> tuple[float, float, float]:
        return (self.radius_m, self.radius_m, self.radius_m)

    @property
    def threshold_m(self) -> float:
        return self.radius_m

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/circle-sdf/v1",
            "strategy": self.strategy_id,
            "radius_m": self.radius_m,
            "constraint_id": self.constraint_id,
        }

    def evaluate_pose(
        self,
        actual_pose: Pose3V1,
        proxy: EoatProxyV1,
        reference_pose: Pose3V1,
    ) -> float:
        return _circle_pose_field(
            self.radius_m,
            actual_pose,
            proxy,
            reference_pose,
        )


@dataclass(frozen=True, slots=True, init=False)
class EllipseSdfV1:
    """Along/lateral/normal anisotropic ellipsoid field in metres."""

    along_m: float
    lateral_m: float
    normal_m: float
    constraint_id: str
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    strategy_id = "ellipse_sdf_v1"

    def __init__(
        self,
        along_m: float | None = None,
        lateral_m: float | None = None,
        normal_m: float | None = None,
        *,
        bounds_m: Sequence[float] | None = None,
        half_axes_m: Sequence[float] | None = None,
        along_radius_m: float | None = None,
        lateral_radius_m: float | None = None,
        normal_radius_m: float | None = None,
        constraint_id: str | None = None,
    ) -> None:
        selected_bounds = bounds_m if bounds_m is not None else half_axes_m
        if selected_bounds is not None:
            if any(value is not None for value in (along_m, lateral_m, normal_m)):
                raise GeometryError("EllipseSdfV1 received both bounds_m and scalar bounds")
            along_m, lateral_m, normal_m = _positive_bounds(selected_bounds, name="bounds_m")
        scalar_aliases = (along_radius_m, lateral_radius_m, normal_radius_m)
        if any(value is not None for value in scalar_aliases):
            if any(value is None for value in scalar_aliases):
                raise GeometryError("all scalar ellipse radius aliases are required together")
            if any(value is not None for value in (along_m, lateral_m, normal_m)):
                raise GeometryError("EllipseSdfV1 received conflicting scalar bounds")
            along_m, lateral_m, normal_m = scalar_aliases  # type: ignore[assignment]
        if any(value is None for value in (along_m, lateral_m, normal_m)):
            raise GeometryError("along_m, lateral_m and normal_m are required")
        bounds = (
            _finite_float(along_m, name="along_m", positive=True),
            _finite_float(lateral_m, name="lateral_m", positive=True),
            _finite_float(normal_m, name="normal_m", positive=True),
        )
        identifier = _text(
            self.strategy_id if constraint_id is None else constraint_id,
            name="constraint_id",
        )
        document = {
            "schema": "ur10e.modular-tube/ellipse-sdf/v1",
            "strategy": self.strategy_id,
            "bounds_m": bounds,
            "constraint_id": identifier,
        }
        object.__setattr__(self, "along_m", bounds[0])
        object.__setattr__(self, "lateral_m", bounds[1])
        object.__setattr__(self, "normal_m", bounds[2])
        object.__setattr__(self, "constraint_id", identifier)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

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
    def bounds_m(self) -> tuple[float, float, float]:
        return (self.along_m, self.lateral_m, self.normal_m)

    @property
    def half_axes_m(self) -> tuple[float, float, float]:
        return self.bounds_m

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/ellipse-sdf/v1",
            "strategy": self.strategy_id,
            "bounds_m": list(self.bounds_m),
            "constraint_id": self.constraint_id,
        }

    def evaluate_pose(
        self,
        actual_pose: Pose3V1,
        proxy: EoatProxyV1,
        reference_pose: Pose3V1,
    ) -> float:
        return _ellipse_pose_field(
            self.bounds_m,
            actual_pose,
            proxy,
            reference_pose,
        )


def _reject_forbidden_composite_child(child: Any) -> None:
    operator = getattr(child, "operator", None)
    if isinstance(operator, str) and operator.lower() in {
        "union",
        "negation",
        "negate",
        "subtraction",
        "subtract",
    }:
        raise GeometryError(f"composite_sdf_v1 rejects {operator} operators")
    strategy = getattr(child, "strategy_id", None)
    if isinstance(strategy, str) and any(
        token in strategy.lower() for token in ("union", "negat", "subtract")
    ):
        raise GeometryError(f"composite_sdf_v1 rejects forbidden strategy {strategy}")
    class_name = type(child).__name__.lower()
    if any(token in class_name for token in ("union", "negat", "subtract")):
        raise GeometryError(f"composite_sdf_v1 rejects forbidden child {type(child).__name__}")


@dataclass(frozen=True, slots=True, init=False)
class CompositeSdfV1:
    """Intersection-only composite field ``max(child)`` in metres."""

    children: tuple[Any, ...]
    operator: str
    root_id: str
    _identity_sha256: str = field(init=False, repr=False, compare=True)

    strategy_id = "composite_sdf_v1"

    def __init__(
        self,
        children: Sequence[Any],
        operator: str = "intersection",
        *,
        constraint_id: str = "composite_sdf_v1",
    ) -> None:
        selected_children = tuple(children)
        if not selected_children:
            raise GeometryError("composite_sdf_v1 cannot have empty children")
        if operator != "intersection":
            raise GeometryError("composite_sdf_v1 permits intersection only")
        identifier = _text(constraint_id, name="constraint_id")
        ids: set[str] = set()
        for child in selected_children:
            _reject_forbidden_composite_child(child)
            child_id = getattr(child, "constraint_id", None)
            child_sha = getattr(child, "sha256", None)
            evaluate = getattr(child, "evaluate_point", None)
            pose_evaluate = getattr(child, "evaluate_pose", None)
            canonical_document = getattr(child, "canonical_document", None)
            if not isinstance(child_id, str) or not child_id:
                raise GeometryError("composite children require non-empty constraint_id")
            if child_id in ids:
                raise GeometryError(f"composite child ID {child_id!r} is duplicated")
            if not isinstance(child_sha, str) or len(child_sha) != 64:
                raise GeometryError("composite children require canonical identities")
            if not callable(evaluate) and not callable(pose_evaluate):
                raise GeometryError("composite children require evaluate_point or evaluate_pose")
            if not callable(canonical_document):
                raise GeometryError("composite children require canonical_document")
            if isinstance(child, CompositeSdfV1):
                raise GeometryError("composite_sdf_v1 does not permit nested composite roots")
            ids.add(child_id)
        document = {
            "schema": "ur10e.modular-tube/composite-sdf/v1",
            "strategy": self.strategy_id,
            "operator": operator,
            "root_id": identifier,
            "children": [getattr(child, "canonical_document")() for child in selected_children],
        }
        object.__setattr__(self, "children", selected_children)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "root_id", identifier)
        object.__setattr__(self, "_identity_sha256", _identity_document_sha256(document))

    @property
    def constraint_id(self) -> str:
        return self.root_id

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    @property
    def sha256(self) -> str:
        return self._identity_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._identity_sha256

    def canonical_document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/composite-sdf/v1",
            "strategy": self.strategy_id,
            "operator": self.operator,
            "root_id": self.root_id,
            "children": [child.canonical_document() for child in self.children],
        }

    def evaluate_point(
        self,
        point_reference_m: tuple[float, float, float],
    ) -> tuple[float, str]:
        """Return ``(max_child_value, first_worst_child_id)``."""

        point = _tuple3(point_reference_m, name="point_reference_m")
        worst_value = -math.inf
        worst_id = self.children[0].constraint_id
        for child in self.children:
            if not callable(getattr(child, "evaluate_point", None)):
                raise GeometryError(
                    f"constraint {child.constraint_id} requires a full pose evaluation"
                )
            value = _finite_float(
                child.evaluate_point(point),
                name=f"constraint {child.constraint_id} result",
            )
            if value > worst_value:
                worst_value = value
                worst_id = child.constraint_id
        return worst_value, worst_id

    def evaluate_pose(
        self,
        actual_pose: Pose3V1,
        proxy: EoatProxyV1,
        reference_pose: Pose3V1,
        translation_reference: tuple[float, float, float] | None = None,
    ) -> tuple[float, str]:
        """Evaluate every child in one intersection using full pose inputs."""

        if translation_reference is None:
            translation_reference, _ = _pose_displacements(
                actual_pose,
                proxy,
                reference_pose,
            )
        point = _tuple3(translation_reference, name="translation_reference")
        worst_value = -math.inf
        worst_id = self.children[0].constraint_id
        for child in self.children:
            if isinstance(child, (CircleSdfV1, EllipseSdfV1)):
                value = child.evaluate_pose(actual_pose, proxy, reference_pose)
            else:
                value = child.evaluate_point(point)
            value = _finite_float(
                value,
                name=f"constraint {child.constraint_id} result",
            )
            if value > worst_value:
                worst_value = value
                worst_id = child.constraint_id
        return worst_value, worst_id


@dataclass(frozen=True, slots=True)
class SignedFieldSample:
    """One finite geometry result; zero is the stop boundary."""

    signed_distance_m: float
    strategy: str
    worst_constraint_id: str
    reference_sample_identity: str
    reference_sample_id: str

    def __post_init__(self) -> None:
        signed = _finite_float(self.signed_distance_m, name="signed_distance_m")
        strategy = _text(self.strategy, name="strategy")
        constraint = _text(self.worst_constraint_id, name="worst_constraint_id")
        sample_identity = _sha256(
            self.reference_sample_identity,
            name="reference_sample_identity",
        )
        sample_id = _text(self.reference_sample_id, name="reference_sample_id")
        object.__setattr__(self, "signed_distance_m", signed)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "worst_constraint_id", constraint)
        object.__setattr__(self, "reference_sample_identity", sample_identity)
        object.__setattr__(self, "reference_sample_id", sample_id)

    @property
    def signed_margin_m(self) -> float:
        return self.signed_distance_m

    @property
    def signed_field_m(self) -> float:
        return self.signed_distance_m

    @property
    def reference_sample_sha256(self) -> str:
        return self.reference_sample_identity

    @property
    def reference_identity(self) -> str:
        return self.reference_sample_identity

    @property
    def inside(self) -> bool:
        return self.signed_distance_m < 0.0

    @property
    def on_boundary(self) -> bool:
        return self.signed_distance_m == 0.0

    @property
    def outside(self) -> bool:
        return self.signed_distance_m > 0.0


SignedFieldSampleV1 = SignedFieldSample


def _validate_geometry_inputs(
    actual_pose: Pose3V1,
    proxy: EoatProxyV1,
    swept_reference: SweptReferenceV1,
) -> ReferencePoseSampleV1:
    if not isinstance(actual_pose, Pose3V1):
        raise GeometryError("actual_pose must be a Pose3V1")
    if not isinstance(proxy, EoatProxyV1):
        raise GeometryError("proxy must be an EoatProxyV1")
    if not isinstance(swept_reference, SweptReferenceV1):
        raise GeometryError("swept_reference must be a SweptReferenceV1")
    if proxy.T_tool_proxy is None:
        raise GeometryError(
            "T_tool_proxy=None is unqualified and cannot be evaluated"
        )
    sample = swept_reference.closest_sample(actual_pose)
    if actual_pose.frame_id != swept_reference.source_frame:
        raise GeometryError("actual pose frame does not match swept-reference frame")
    # ``proxy.source_frame`` identifies the source/model frame for audit and
    # identity binding.  A qualified ``T_tool_proxy`` maps the proxy into tool
    # coordinates, so it need not equal the world frame used by the swept
    # reference.
    return sample


class SignedDistanceGeometry:
    """Compile one explicit strategy and evaluate it without progress inputs."""

    __slots__ = ("strategy", "strategy_id", "identity_sha256", "_sealed")

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("SignedDistanceGeometry is immutable after compilation")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        strategy: CircleSdfV1 | EllipseSdfV1 | CompositeSdfV1 | str | None = None,
        *,
        root: Any = None,
        radius_m: float | None = None,
        threshold_m: float | None = None,
        bounds_m: Sequence[float] | None = None,
        children: Sequence[SdfLeafV1] | None = None,
    ) -> None:
        if strategy is not None and root is not None and strategy != root:
            raise GeometryError("SignedDistanceGeometry received conflicting strategy/root")
        selected = strategy if strategy is not None else root
        if isinstance(selected, str):
            selected_radius = radius_m if radius_m is not None else threshold_m
            if selected == CircleSdfV1.strategy_id:
                if selected_radius is None and bounds_m is not None:
                    raw_bounds = tuple(bounds_m)
                    if len(raw_bounds) != 3 or not (raw_bounds[0] == raw_bounds[1] == raw_bounds[2]):
                        raise GeometryError("circle_sdf_v1 requires one isotropic bound")
                    selected_radius = raw_bounds[0]
                selected = CircleSdfV1(selected_radius)
            elif selected == EllipseSdfV1.strategy_id:
                selected = EllipseSdfV1(bounds_m=bounds_m)
            elif selected == CompositeSdfV1.strategy_id:
                selected = CompositeSdfV1(() if children is None else children)
            else:
                raise GeometryError(f"unknown strategy {selected!r}")
        if not isinstance(selected, (CircleSdfV1, EllipseSdfV1, CompositeSdfV1)):
            raise GeometryError(
                "SignedDistanceGeometry requires exactly one circle, ellipse, or composite strategy"
            )
        self.strategy = selected
        self.strategy_id = selected.strategy_id
        self.identity_sha256 = selected.sha256
        self._sealed = True

    @property
    def canonical_sha256(self) -> str:
        return self.identity_sha256

    @property
    def root(self) -> CircleSdfV1 | EllipseSdfV1 | CompositeSdfV1:
        return self.strategy

    def evaluate(
        self,
        actual_pose: Pose3V1,
        proxy: EoatProxyV1,
        swept_reference: SweptReferenceV1,
    ) -> SignedFieldSample:
        sample = _validate_geometry_inputs(actual_pose, proxy, swept_reference)
        reference_pose = sample.pose
        translation_world = _sub(actual_pose.position_m, reference_pose.position_m)
        reference_rotation = reference_pose.rotation_matrix()
        translation_reference = _mat_transpose_vec(reference_rotation, translation_world)

        if isinstance(self.strategy, CircleSdfV1):
            signed = self.strategy.evaluate_pose(actual_pose, proxy, reference_pose)
            worst_constraint = self.strategy.constraint_id
        elif isinstance(self.strategy, EllipseSdfV1):
            signed = self.strategy.evaluate_pose(actual_pose, proxy, reference_pose)
            worst_constraint = self.strategy.constraint_id
        else:
            signed, worst_constraint = self.strategy.evaluate_pose(
                actual_pose,
                proxy,
                reference_pose,
                translation_reference,
            )

        if not math.isfinite(signed):  # pragma: no cover - defensive boundary
            raise GeometryError("signed-distance solver returned a non-finite value")
        return SignedFieldSample(
            signed_distance_m=signed,
            strategy=self.strategy_id,
            worst_constraint_id=worst_constraint,
            reference_sample_identity=sample.sha256,
            reference_sample_id=sample.sample_id,
        )

    def sample(
        self,
        actual_pose: Pose3V1,
        proxy: EoatProxyV1,
        swept_reference: SweptReferenceV1,
    ) -> SignedFieldSample:
        return self.evaluate(actual_pose, proxy, swept_reference)


CircleSDFV1 = CircleSdfV1
EllipseSDFV1 = EllipseSdfV1
CompositeSDFV1 = CompositeSdfV1
HalfSpaceV1 = HalfSpaceConstraintV1
ExclusionBoxV1 = ExclusionBoxConstraintV1
CircleSDF = CircleSdfV1
EllipseSDF = EllipseSdfV1
CompositeSDF = CompositeSdfV1
HalfSpaceConstraint = HalfSpaceConstraintV1
ExclusionBoxConstraint = ExclusionBoxConstraintV1
SignedDistanceGeometryV1 = SignedDistanceGeometry


__all__ = [
    "CircleSDFV1",
    "CircleSDF",
    "CircleSdfV1",
    "CompositeSDFV1",
    "CompositeSDF",
    "CompositeSdfV1",
    "EllipseSDFV1",
    "EllipseSDF",
    "EllipseSdfV1",
    "ExclusionBoxConstraintV1",
    "ExclusionBoxConstraint",
    "ExclusionBoxV1",
    "GeometryError",
    "HalfSpaceConstraintV1",
    "HalfSpaceConstraint",
    "HalfSpaceV1",
    "SdfLeafV1",
    "SignedDistanceGeometry",
    "SignedDistanceGeometryV1",
    "SignedFieldSample",
    "SignedFieldSampleV1",
]
