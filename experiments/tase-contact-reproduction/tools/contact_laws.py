"""Python facade for the offline native six-law contact benchmark.

``ContactLaw`` owns one deterministic controller state.  Its public labels
are exactly ``LAC``, ``NAC``, ``SFC``, ``DSFC``, ``ISFC`` and ``MSFC``.  The
adapter calls the lower-level native law functions through a small C API;
there is no network or robot endpoint in this module.

The constructor fixes ``dt_s`` for the lifetime of an instance.  ``step``
accepts an optional ``dt_s`` only as an explicit consistency check, so a
caller cannot silently change the integration interval at a disturbance
boundary.  ``snapshot`` and ``restore`` include the complete mechanical
state and the MSFC structural memory needed for fair replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from build_contact_laws import (
    DEFAULT_SOURCE_DIR,
    SOURCE_NAMES,
    ContactLawBuild,
    ContactLawBuildError,
    build_shared_library,
)


PUBLIC_LAWS = ("LAC", "NAC", "SFC", "DSFC", "ISFC", "MSFC")
"""Selectable labels for the benchmark (RPSFC is intentionally absent)."""

PARAMETER_ORDER: dict[str, tuple[str, ...]] = {
    "LAC": ("m", "mu", "g"),
    "NAC": ("m", "mu", "g", "alpha", "sigma"),
    "SFC": ("m", "mu", "n", "g"),
    "DSFC": (
        "m",
        "g",
        "p",
        "a",
        "n",
        "mu",
        "max_iterations",
        "residual_tolerance_n",
        "relative_radius_tolerance",
    ),
    "ISFC": ("m", "mu", "n", "eta0", "g", "v_ref", "substeps"),
    "MSFC": (
        "m",
        "g",
        "p",
        "a",
        "n",
        "mu",
        "force_scale_n",
        "tau_force_s",
        "tau_recovery_s",
        "kappa_per_n2_s",
        "max_iterations",
        "residual_tolerance_n",
        "structure_tolerance",
        "minimum_metric_eigenvalue",
    ),
}

_INTEGER_PARAMETER_BOUNDS: dict[tuple[str, str], tuple[int, int]] = {
    ("DSFC", "max_iterations"): (1, 32),
    ("ISFC", "substeps"): (1, 1024),
    ("MSFC", "max_iterations"): (1, 32),
}
SNAPSHOT_SIZE = 22
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "contact_benchmark_laws.json"


class ContactLawError(RuntimeError):
    """Invalid configuration or fail-closed native law error."""


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ContactLawError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ContactLawError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ContactLawError(f"{name} must be a finite number")
    return result


def _normalise_law(law: str) -> str:
    if not isinstance(law, str) or law not in PUBLIC_LAWS:
        raise ContactLawError(
            f"law must be one of {', '.join(PUBLIC_LAWS)}; RPSFC is not selectable"
        )
    return law


def _normalise_parameters(
    law: str,
    parameters: Mapping[str, Any] | Sequence[Any],
) -> tuple[dict[str, float], tuple[float, ...]]:
    names = PARAMETER_ORDER[law]
    if isinstance(parameters, Mapping):
        expected = set(names)
        actual = set(parameters)
        missing = expected - actual
        extra = actual - expected
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"missing {sorted(missing)}")
            if extra:
                detail.append(f"unknown {sorted(extra)}")
            raise ContactLawError(f"{law} parameter keys do not match: {'; '.join(detail)}")
        values = tuple(_finite_float(parameters[name], f"{law} {name}") for name in names)
    else:
        if isinstance(parameters, (str, bytes, bytearray)):
            raise ContactLawError(f"{law} parameters must be a mapping or numeric sequence")
        try:
            raw_values = tuple(parameters)
        except TypeError as error:
            raise ContactLawError(f"{law} parameters must be a mapping or numeric sequence") from error
        if len(raw_values) != len(names):
            raise ContactLawError(f"{law} expects {len(names)} parameters in order {names}")
        values = tuple(_finite_float(value, f"{law} {name}") for name, value in zip(names, raw_values))
    result = dict(zip(names, values))
    for name, (lower, upper) in _INTEGER_PARAMETER_BOUNDS.items():
        if name[0] != law:
            continue
        value = result[name[1]]
        if value != math.floor(value) or not lower <= value <= upper:
            raise ContactLawError(
                f"{law} {name[1]} must be an integer in [{lower}, {upper}]"
            )
        result[name[1]] = float(int(value))
    return result, tuple(result[name] for name in names)


def _vector3(values: Sequence[Any], dimension: int, name: str) -> tuple[float, float, float]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ContactLawError(f"{name} must have dimension {dimension}")
    try:
        raw = tuple(values)
    except TypeError as error:
        raise ContactLawError(f"{name} must have dimension {dimension}") from error
    if len(raw) not in (dimension, 3):
        raise ContactLawError(f"{name} must have length {dimension} or 3")
    converted = tuple(_finite_float(value, f"{name}[{index}]") for index, value in enumerate(raw))
    if len(converted) == dimension:
        converted = converted + (0.0,) * (3 - dimension)
    return converted  # type: ignore[return-value]


def _active(values: Sequence[float], dimension: int) -> tuple[float, ...]:
    return tuple(float(value) for value in values[:dimension])


@dataclass(frozen=True)
class ContactLawStep:
    """One successful native tick, with vectors in the selected dimension."""

    law: str
    dimension: int
    dt_s: float
    force: tuple[float, ...]
    previous_state: tuple[float, ...]
    state: tuple[float, ...]
    command: tuple[float, ...]
    acceleration: tuple[float, ...]


@dataclass(frozen=True)
class ContactLawSnapshot:
    """Opaque, immutable 22-double replay token returned by ``snapshot``."""

    values: tuple[float, ...]
    # The native token is intentionally opaque.  Python adds this binding so a
    # token cannot silently move between laws, parameter sets, or source
    # builds that happen to share the same dimension and dt.
    binding_id: str | None = None

    def __post_init__(self) -> None:
        try:
            values = tuple(_finite_float(value, "snapshot") for value in self.values)
        except TypeError as error:
            raise ContactLawError("snapshot must be a 22-value sequence") from error
        if len(values) != SNAPSHOT_SIZE:
            raise ContactLawError(f"snapshot must contain {SNAPSHOT_SIZE} values")
        object.__setattr__(self, "values", values)
        if self.binding_id is not None:
            if not isinstance(self.binding_id, str) or len(self.binding_id) != 64:
                raise ContactLawError("snapshot binding_id must be a SHA-256 hex digest")
            try:
                int(self.binding_id, 16)
            except ValueError as error:
                raise ContactLawError("snapshot binding_id must be a SHA-256 hex digest") from error

    def as_tuple(self) -> tuple[float, ...]:
        return tuple(self.values)

    @property
    def parameter_identity(self) -> str | None:
        """SHA-256 binding of law, parameters, dt, and native source build."""

        return self.binding_id

    @property
    def dimension(self) -> int:
        return int(self.values[2])

    @property
    def dt_s(self) -> float:
        return float(self.values[3])

    @property
    def state(self) -> tuple[float, ...]:
        return _active(self.values[4:7], self.dimension)

    @property
    def force_history(self) -> tuple[float, ...]:
        return _active(self.values[7:10], self.dimension)

    @property
    def structure(self) -> tuple[tuple[float, ...], ...]:
        dimension = self.dimension
        flat = self.values[10:19]
        return tuple(
            tuple(float(flat[row * 3 + column]) for column in range(dimension))
            for row in range(dimension)
        )

    @property
    def metric_eigenvalues(self) -> tuple[float, ...]:
        return _active(self.values[19:22], self.dimension)


def _configure_library(library: ctypes.CDLL) -> None:
    c_double_p = ctypes.POINTER(ctypes.c_double)
    library.contact_law_create.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        c_double_p,
        ctypes.c_size_t,
        ctypes.c_double,
    ]
    library.contact_law_create.restype = ctypes.c_void_p
    library.contact_law_step.argtypes = [
        ctypes.c_void_p,
        c_double_p,
        c_double_p,
        c_double_p,
        c_double_p,
    ]
    library.contact_law_step.restype = ctypes.c_int
    library.contact_law_snapshot_size.argtypes = []
    library.contact_law_snapshot_size.restype = ctypes.c_size_t
    library.contact_law_snapshot.argtypes = [ctypes.c_void_p, c_double_p, ctypes.c_size_t]
    library.contact_law_snapshot.restype = ctypes.c_int
    library.contact_law_restore.argtypes = [ctypes.c_void_p, c_double_p, ctypes.c_size_t]
    library.contact_law_restore.restype = ctypes.c_int
    library.contact_law_reset.argtypes = [ctypes.c_void_p]
    library.contact_law_reset.restype = ctypes.c_int
    library.contact_law_last_error.argtypes = [ctypes.c_void_p]
    library.contact_law_last_error.restype = ctypes.c_char_p
    library.contact_law_destroy.argtypes = [ctypes.c_void_p]
    library.contact_law_destroy.restype = None


def _native_error(library: ctypes.CDLL, handle: ctypes.c_void_p | None) -> str:
    try:
        raw = library.contact_law_last_error(handle)
        if raw:
            return raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - only reached for a broken library ABI
        pass
    return "unknown native contact law error"


class ContactLaw:
    """Stateful offline adapter for one of the six public native laws."""

    def __init__(
        self,
        law: str,
        parameters: Mapping[str, Any] | Sequence[Any],
        dimension: int = 3,
        dt_s: float = 0.002,
        *,
        source_dir: Path | str = DEFAULT_SOURCE_DIR,
        build_root: Path | str | None = None,
        compiler: str = "g++",
        force_rebuild: bool = False,
        expected_source_hashes: Mapping[str, str] | None = None,
    ) -> None:
        self.law = _normalise_law(law)
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension not in (1, 2, 3):
            raise ContactLawError("dimension must be an integer in [1, 3]")
        self.dimension = dimension
        self.dt_s = _finite_float(dt_s, "dt_s")
        if self.dt_s <= 0.0:
            raise ContactLawError("dt_s must be positive")
        normalised, ordered = _normalise_parameters(self.law, parameters)
        self.parameters = normalised
        self.parameter_values = ordered
        try:
            self.build: ContactLawBuild = build_shared_library(
                source_dir=source_dir,
                build_root=build_root,
                compiler=compiler,
                force=force_rebuild,
                expected_source_hashes=expected_source_hashes,
            )
        except ContactLawBuildError as error:
            raise ContactLawError(str(error)) from error
        try:
            library = ctypes.CDLL(str(self.build.library_path))
        except OSError as error:
            raise ContactLawError(f"cannot load native contact law library: {error}") from error
        _configure_library(library)
        if library.contact_law_snapshot_size() != SNAPSHOT_SIZE:
            raise ContactLawError("native snapshot ABI size does not match Python adapter")
        parameter_array = (ctypes.c_double * len(ordered))(*ordered)
        handle = library.contact_law_create(
            self.law.encode("ascii"),
            self.dimension,
            parameter_array,
            len(ordered),
            self.dt_s,
        )
        if not handle:
            raise ContactLawError(_native_error(library, None))
        self._library = library
        self._handle = ctypes.c_void_p(handle)
        self._state_full = (0.0, 0.0, 0.0)
        self._binding_id = self._compute_binding_id()

    def _compute_binding_id(self) -> str:
        payload = {
            "schema": "ur10e.contact-law-snapshot-binding-v1",
            "law": self.law,
            "dimension": self.dimension,
            "dt_s": self.dt_s,
            "parameters": self.parameter_values,
            "native_build_fingerprint": self.build.fingerprint,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_config(
        cls,
        law: str,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        *,
        dimension: int | None = None,
        dt_s: float | None = None,
        **kwargs: Any,
    ) -> "ContactLaw":
        """Construct a law from ``contact_benchmark_laws.json``."""

        path = Path(config_path)
        try:
            root = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContactLawError(f"cannot read law configuration {path}: {error}") from error
        selected = _normalise_law(law)
        if not isinstance(root, dict) or not isinstance(root.get("laws"), dict):
            raise ContactLawError("law configuration must contain an object named laws")
        provenance = root.get("provenance")
        native_sources = provenance.get("native_sources") if isinstance(provenance, dict) else None
        declared_hashes = native_sources.get("files") if isinstance(native_sources, dict) else None
        if not isinstance(declared_hashes, dict) or set(declared_hashes) != set(SOURCE_NAMES):
            raise ContactLawError(
                "law configuration must declare SHA-256 hashes for the four native source files"
            )
        if "expected_source_hashes" in kwargs:
            raise ContactLawError("expected_source_hashes is controlled by law configuration")
        spec = root["laws"].get(selected)
        if not isinstance(spec, dict) or not isinstance(spec.get("parameters"), dict):
            raise ContactLawError(f"law configuration has no parameters for {selected}")
        if dimension is None:
            dimension = root.get("dimension_default", 3)
        if dt_s is None:
            dt_s = root.get("dt_s", 0.002)
        return cls(
            selected,
            spec["parameters"],
            dimension,
            dt_s,
            expected_source_hashes={str(name): str(value) for name, value in declared_hashes.items()},
            **kwargs,
        )

    def _require_open(self) -> ctypes.c_void_p:
        if self._handle is None:
            raise ContactLawError("contact law has been closed")
        return self._handle

    def _raise_native(self, operation: str) -> None:
        raise ContactLawError(f"{operation} failed: {_native_error(self._library, self._handle)}")

    def step(self, force: Sequence[Any], dt_s: float | None = None) -> ContactLawStep:
        """Advance one fixed-``dt_s`` tick and return state plus command.

        Supplying ``dt_s`` is useful to make an outer timestamp loop explicit;
        any value different from the constructor interval is rejected before
        native state is touched.
        """

        handle = self._require_open()
        if dt_s is not None:
            checked_dt = _finite_float(dt_s, "step dt_s")
            if checked_dt != self.dt_s:
                raise ContactLawError(
                    f"ContactLaw uses fixed dt_s={self.dt_s:.17g}; step received {checked_dt:.17g}"
                )
        force_full = _vector3(force, self.dimension, "force")
        force_array = (ctypes.c_double * 3)(*force_full)
        state_array = (ctypes.c_double * 3)()
        command_array = (ctypes.c_double * 3)()
        acceleration_array = (ctypes.c_double * 3)()
        previous = _active(self._state_full, self.dimension)
        status = self._library.contact_law_step(
            handle,
            force_array,
            state_array,
            command_array,
            acceleration_array,
        )
        if status != 0:
            self._raise_native("native step")
        state_full = tuple(float(value) for value in state_array)
        command_full = tuple(float(value) for value in command_array)
        acceleration_full = tuple(float(value) for value in acceleration_array)
        self._state_full = state_full
        return ContactLawStep(
            law=self.law,
            dimension=self.dimension,
            dt_s=self.dt_s,
            force=_active(force_full, self.dimension),
            previous_state=previous,
            state=_active(state_full, self.dimension),
            command=_active(command_full, self.dimension),
            acceleration=_active(acceleration_full, self.dimension),
        )

    def snapshot(self) -> ContactLawSnapshot:
        """Return a complete native replay token."""

        handle = self._require_open()
        output = (ctypes.c_double * SNAPSHOT_SIZE)()
        status = self._library.contact_law_snapshot(handle, output, SNAPSHOT_SIZE)
        if status != 0:
            self._raise_native("native snapshot")
        return ContactLawSnapshot(
            tuple(float(value) for value in output),
            binding_id=self._binding_id,
        )

    def restore(self, snapshot: ContactLawSnapshot) -> None:
        """Restore state and MSFC memory transactionally from ``snapshot``."""

        handle = self._require_open()
        if not isinstance(snapshot, ContactLawSnapshot):
            raise ContactLawError(
                "restore requires a ContactLawSnapshot returned by this binding"
            )
        if snapshot.binding_id != self._binding_id:
            raise ContactLawError("snapshot binding does not match law, parameters, or native source")
        values = snapshot.values
        if len(values) != SNAPSHOT_SIZE:
            raise ContactLawError(f"snapshot must contain {SNAPSHOT_SIZE} values")
        input_array = (ctypes.c_double * SNAPSHOT_SIZE)(*values)
        status = self._library.contact_law_restore(handle, input_array, SNAPSHOT_SIZE)
        if status != 0:
            self._raise_native("native restore")
        self._state_full = tuple(float(value) for value in values[4:7])

    def reset(self) -> None:
        """Reset mechanical state and MSFC memory to their native zero state."""

        handle = self._require_open()
        status = self._library.contact_law_reset(handle)
        if status != 0:
            self._raise_native("native reset")
        self._state_full = (0.0, 0.0, 0.0)

    @property
    def state(self) -> tuple[float, ...]:
        """Current controller state in the selected dimension."""

        self._require_open()
        return _active(self._state_full, self.dimension)

    @property
    def identity(self) -> str:
        """SHA-256 identity used to bind snapshots to this controller."""

        return self._binding_id

    def close(self) -> None:
        handle = self._handle
        if handle is not None:
            self._library.contact_law_destroy(handle)
            self._handle = None

    def __enter__(self) -> "ContactLaw":
        self._require_open()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown path
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "ContactLaw",
    "ContactLawError",
    "ContactLawSnapshot",
    "ContactLawStep",
    "DEFAULT_CONFIG_PATH",
    "PARAMETER_ORDER",
    "PUBLIC_LAWS",
    "SNAPSHOT_SIZE",
]
