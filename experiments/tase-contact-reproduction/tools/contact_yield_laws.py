"""Native-law adapters in a fixed base frame, plus the matched radial SFC ablation.

Original SFC remains the geometrically anisotropic baseline (axis-decoupled
componentwise damping).  SFC_RADIAL uses the same (m, mu, n, g) with a
Euclidean residual; it is an ablation, not the baseline.  DSFC and MSFC call
the mature native 3D laws.  Law state is never rotated into the tool frame.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from contact_laws import (
    SNAPSHOT_SIZE,
    ContactLaw,
    ContactLawSnapshot,
    PARAMETER_ORDER,
)
from contact_yield_math import finite_scalar
from contact_yield_protocol import (
    LAW_CONFIG_PATH,
    method_role,
    native_law_label,
)


class YieldLawError(RuntimeError):
    pass


def _vector3(values: Sequence[Any], name: str) -> tuple[float, float, float]:
    try:
        raw = tuple(float(item) for item in values)
    except (TypeError, ValueError) as error:
        raise YieldLawError(f"{name} must be a length-3 finite vector") from error
    if len(raw) != 3 or not all(math.isfinite(item) for item in raw):
        raise YieldLawError(f"{name} must be a length-3 finite vector")
    return raw  # type: ignore[return-value]


def _parameters_for(method: str, parameters: Mapping[str, Any] | None) -> dict[str, float]:
    native = native_law_label(method)
    names = PARAMETER_ORDER[native]
    if parameters is None:
        root = json.loads(LAW_CONFIG_PATH.read_text(encoding="utf-8"))
        parameters = root["laws"][native]["parameters"]
    missing = set(names) - set(parameters)
    extra = set(parameters) - set(names)
    if missing or extra:
        raise YieldLawError(f"{method} parameters do not match {native}: missing={sorted(missing)} extra={sorted(extra)}")
    return {name: float(parameters[name]) for name in names}


class RadialSfcLaw:
    """Matched radial ablation of paper SFC.  Not the baseline.

    Explicit Euler with previous-state Euclidean damping:
    ``m v' = f - mu ||v||^{n-1} v``, ``command = g v``.  The 1D restriction
    matches native ``sfc_step``.  Extra Python state is stored beside a 22-slot
    token so replay does not invent a native 3D radial SFC.
    """

    def __init__(self, parameters: Mapping[str, Any], *, dt_s: float, identity: str) -> None:
        self.m = finite_scalar(parameters["m"], "m")
        self.mu = finite_scalar(parameters["mu"], "mu")
        self.n = finite_scalar(parameters["n"], "n")
        self.g = finite_scalar(parameters["g"], "g")
        self.dt_s = finite_scalar(dt_s, "dt_s")
        if min(self.m, self.mu, self.g, self.dt_s) <= 0.0 or self.n <= 1.0:
            raise YieldLawError("radial SFC parameters must be positive with n>1")
        self.identity = identity
        self._state = np.zeros(3)
        self._force = np.zeros(3)

    def step(self, force: Sequence[Any], dt_s: float) -> tuple[float, float, float]:
        elapsed = finite_scalar(dt_s, "dt_s")
        if not 0.0 < elapsed <= 0.004:
            raise YieldLawError("elapsed dt_s outside (0, 4ms]")
        force_vec = np.asarray(_vector3(force, "force"), dtype=float)
        speed = float(np.linalg.norm(self._state))
        damping = self.mu * (speed ** (self.n - 1.0)) * self._state if speed > 0.0 else np.zeros(3)
        acceleration = (force_vec - damping) / self.m
        self._state = self._state + elapsed * acceleration
        self._force = force_vec
        command = self.g * self._state
        if not np.all(np.isfinite(command)):
            raise YieldLawError("radial SFC produced a nonfinite command")
        return tuple(float(value) for value in command)

    def snapshot(self) -> ContactLawSnapshot:
        values = [0.0] * SNAPSHOT_SIZE
        values[0] = 20260920.0
        values[1] = 3.0  # radial ablation marker, not a native kind
        values[2] = 3.0
        values[3] = self.dt_s
        values[4:7] = [float(value) for value in self._state]
        values[7:10] = [float(value) for value in self._force]
        return ContactLawSnapshot(tuple(values), binding_id=self.identity)

    def restore(self, snapshot: ContactLawSnapshot) -> None:
        if snapshot.binding_id != self.identity:
            raise YieldLawError("radial SFC snapshot binding does not match")
        values = snapshot.values
        if values[2] != 3.0:
            raise YieldLawError("radial SFC snapshot dimension mismatch")
        self._state = np.array(values[4:7], dtype=float)
        self._force = np.array(values[7:10], dtype=float)

    def reset(self) -> None:
        self._state[:] = 0.0
        self._force[:] = 0.0

    @property
    def state(self) -> tuple[float, ...]:
        return tuple(float(value) for value in self._state)

    def close(self) -> None:
        return None


class YieldLaw:
    """Uniform 3D base-frame law used by the yield-recovery controller."""

    def __init__(
        self,
        method: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        dt_s: float = 0.002,
        source_dir: Path | str | None = None,
        build_root: Path | str | None = None,
    ) -> None:
        self.method = method
        self.role = method_role(method)
        self.native_law = native_law_label(method)
        self.parameters = _parameters_for(method, parameters)
        self.dt_s = finite_scalar(dt_s, "dt_s")
        kwargs: dict[str, Any] = {"dimension": 3, "dt_s": self.dt_s}
        if source_dir is not None:
            kwargs["source_dir"] = source_dir
        if build_root is not None:
            kwargs["build_root"] = build_root
        if method == "SFC_RADIAL":
            native = ContactLaw(self.native_law, self.parameters, **kwargs)
            identity = hashlib.sha256(
                json.dumps(
                    {
                        "schema": "ur10e.contact-yield-radial-sfc-v1",
                        "method": method,
                        "role": self.role,
                        "native_identity": native.identity,
                        "parameters": [self.parameters[name] for name in PARAMETER_ORDER["SFC"]],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            native.close()
            self._native: ContactLaw | None = None
            self._radial: RadialSfcLaw | None = RadialSfcLaw(
                self.parameters, dt_s=self.dt_s, identity=identity
            )
            self.identity = identity
            self.build_fingerprint = "python-radial-sfc-ablation-matched-to-native-1d"
        else:
            self._radial = None
            self._native = ContactLaw(self.native_law, self.parameters, **kwargs)
            self.identity = self._native.identity
            self.build_fingerprint = self._native.build.fingerprint

    def step(self, force_base_n: Sequence[Any], dt_s: float) -> tuple[float, float, float]:
        force = _vector3(force_base_n, "force_base_n")
        if self._radial is not None:
            return self._radial.step(force, dt_s)
        assert self._native is not None
        result = self._native.step_elapsed(force, dt_s=dt_s)
        return tuple(float(value) for value in result.command)

    def snapshot(self) -> ContactLawSnapshot:
        if self._radial is not None:
            return self._radial.snapshot()
        assert self._native is not None
        return self._native.snapshot()

    def restore(self, snapshot: ContactLawSnapshot) -> None:
        if self._radial is not None:
            self._radial.restore(snapshot)
            return
        assert self._native is not None
        self._native.restore(snapshot)

    def reset(self) -> None:
        if self._radial is not None:
            self._radial.reset()
            return
        assert self._native is not None
        self._native.reset()

    @property
    def state(self) -> tuple[float, ...]:
        if self._radial is not None:
            return self._radial.state
        assert self._native is not None
        return self._native.state

    def close(self) -> None:
        if self._native is not None:
            self._native.close()
            self._native = None

    def __enter__(self) -> "YieldLaw":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
