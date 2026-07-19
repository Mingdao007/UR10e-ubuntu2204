"""Source-owned component allowlist for ExperimentSpec resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from .contracts import RegistryError


class ComponentKind(str, Enum):
    TRAJECTORY = "trajectory"
    CONTROLLER = "controller"
    STAGE_ADAPTER = "stage_adapter"
    BACKEND = "backend"
    OBSERVER = "observer"
    SAFETY = "safety"
    EVIDENCE = "evidence"


SpecValidator = Callable[[Mapping[str, Any]], None]
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")


@dataclass(frozen=True)
class RegistryEntry:
    kind: ComponentKind
    component_id: str
    version: str
    component: object
    validator: SpecValidator | None = None

    def validate_spec(self, document: Mapping[str, Any]) -> None:
        validator = self.validator
        if validator is None:
            candidate = getattr(self.component, "validate_spec", None)
            if callable(candidate):
                validator = candidate
        if validator is not None:
            try:
                validator(document)
            except RegistryError:
                raise
            except Exception as exc:
                raise RegistryError(
                    f"component rejected ExperimentSpec: {self.kind.value}/"
                    f"{self.component_id}@{self.version}: {exc}"
                ) from exc


@dataclass(frozen=True)
class _DeclarativeComponent:
    """Allowlisted semantic identity with no execution behavior."""

    component_id: str
    version: str
    execution_enabled: bool = False


class ComponentRegistry:
    """Explicit registry; configuration can never import Python objects."""

    def __init__(self) -> None:
        self._entries: dict[tuple[ComponentKind, str, str], RegistryEntry] = {}

    @staticmethod
    def _kind(value: ComponentKind | str) -> ComponentKind:
        try:
            return value if isinstance(value, ComponentKind) else ComponentKind(value)
        except ValueError as exc:
            raise RegistryError(f"unknown component kind: {value!r}") from exc

    def register(
        self,
        kind: ComponentKind | str,
        component_id: str,
        version: str,
        component: object,
        *,
        validator: SpecValidator | None = None,
    ) -> None:
        resolved_kind = self._kind(kind)
        if not _IDENTIFIER_RE.fullmatch(component_id):
            raise RegistryError(f"invalid component id: {component_id!r}")
        if not _VERSION_RE.fullmatch(version):
            raise RegistryError(f"invalid component version: {version!r}")
        key = (resolved_kind, component_id, version)
        if key in self._entries:
            raise RegistryError(
                f"duplicate component registration: {resolved_kind.value}/"
                f"{component_id}@{version}"
            )
        self._entries[key] = RegistryEntry(
            kind=resolved_kind,
            component_id=component_id,
            version=version,
            component=component,
            validator=validator,
        )

    def entry(
        self,
        kind: ComponentKind | str,
        component_id: str,
        version: str,
    ) -> RegistryEntry:
        resolved_kind = self._kind(kind)
        key = (resolved_kind, component_id, version)
        try:
            return self._entries[key]
        except KeyError as exc:
            raise RegistryError(
                f"component is not allowlisted: {resolved_kind.value}/"
                f"{component_id}@{version}"
            ) from exc

    def resolve(
        self,
        kind: ComponentKind | str,
        component_id: str,
        version: str,
    ) -> object:
        return self.entry(kind, component_id, version).component

    def validate_spec(self, document: Mapping[str, Any]) -> None:
        components = document.get("components")
        if not isinstance(components, Mapping):
            raise RegistryError("ExperimentSpec components must be an object")

        selected: dict[tuple[ComponentKind, str, str], RegistryEntry] = {}
        for kind in ComponentKind:
            reference = components.get(kind.value)
            if not isinstance(reference, Mapping):
                raise RegistryError(f"missing component reference: {kind.value}")
            component_id = str(reference.get("id", ""))
            version = str(reference.get("version", ""))
            entry = self.entry(kind, component_id, version)
            selected[(kind, component_id, version)] = entry

        lanes = document.get("lanes")
        if not isinstance(lanes, Mapping):
            raise RegistryError("ExperimentSpec lanes must be an object")
        for lane_id, lane in lanes.items():
            if not isinstance(lane, Mapping):
                raise RegistryError(f"lane {lane_id!r} must be an object")
            component_id = str(lane.get("backend_id", ""))
            version = str(lane.get("backend_version", ""))
            entry = self.entry(ComponentKind.BACKEND, component_id, version)
            selected[(ComponentKind.BACKEND, component_id, version)] = entry

        for entry in selected.values():
            entry.validate_spec(document)

    def registered_keys(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                f"{kind.value}/{component_id}@{version}"
                for kind, component_id, version in self._entries
            )
        )


def _register_declarative(
    registry: ComponentRegistry,
    kind: ComponentKind,
    component_id: str,
    version: str = "1",
) -> None:
    registry.register(
        kind,
        component_id,
        version,
        _DeclarativeComponent(component_id=component_id, version=version),
    )


def build_default_registry(*, include_stage_adapters: bool = True) -> ComponentRegistry:
    """Build the reviewed allowlist without importing anything from config."""

    registry = ComponentRegistry()
    for component_id in ("cycloid_v1", "eight_v1"):
        _register_declarative(registry, ComponentKind.TRAJECTORY, component_id)
    _register_declarative(
        registry,
        ComponentKind.CONTROLLER,
        "strict_rnn_speedj_autotune_v1",
    )
    for component_id in (
        "offline_scaffold_v1",
        "replay_scaffold_v1",
        "gazebo_scaffold_v1",
        "ursim_scaffold_v1",
        "hil_scaffold_v1",
        "live_autotune_scaffold_v1",
    ):
        _register_declarative(registry, ComponentKind.BACKEND, component_id)
    from .failure_to_guard import FailureToGuardOutcomeClassifier

    classifier = FailureToGuardOutcomeClassifier()
    registry.register(
        ComponentKind.OBSERVER,
        classifier.component_id,
        classifier.version,
        classifier,
    )
    for component_id in ("no_motion_v1", "live_authorization_required_v1"):
        _register_declarative(registry, ComponentKind.SAFETY, component_id)
    _register_declarative(registry, ComponentKind.EVIDENCE, "jsonl_manifest_v1")

    if include_stage_adapters:
        try:
            from .stage_adapters import register_stage_adapters
        except ModuleNotFoundError as exc:
            expected = f"{__package__}.stage_adapters"
            if exc.name != expected:
                raise
        else:
            register_stage_adapters(registry)
    return registry
