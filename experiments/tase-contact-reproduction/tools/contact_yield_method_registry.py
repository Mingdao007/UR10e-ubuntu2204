"""Typed live-method registry for the yield live entry.

Native SFC/DSFC/MSFC keep the current yield-native source binding.  TASE_RNN
and TASE_QP are registered as unavailable; they are not executable stubs and
do not borrow native hashes, RNN campaign fingerprints, or a fake runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from contact_yield_protocol import PERIOD_S
from yield_native_route import CLAIM_SCOPE as NATIVE_CLAIM_SCOPE
from yield_native_route import METHODS as NATIVE_METHODS
from yield_native_route import SCHEMA as NATIVE_SCHEMA


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = EXPERIMENT_ROOT / "config" / "yield_live_entry_v1.json"
SCHEMA = "yield-live-entry-v1"
CONTACT_PROGRAM = "step5d_contact_six_qp_v1"
HOME_PROGRAM = "step5d_contact_home_v1"
NATIVE_FAMILY = "native_yield"
REGISTERED_UNAVAILABLE = ("TASE_RNN", "TASE_QP")
DIAGNOSTIC_DURATIONS_S = (2.0, 10.0)


class MethodRegistryError(RuntimeError):
    """Method registry or live-entry config failed closed."""


class MethodUnavailableError(MethodRegistryError):
    """A registered method has no executable implementation in this entry."""


@dataclass(frozen=True)
class MethodRecord:
    name: str
    family: str
    available: bool
    provider: str | None
    source_binding: str | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "family": self.family,
            "available": self.available,
        }
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.source_binding is not None:
            payload["source_binding"] = self.source_binding
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MethodRegistryError(f"{name} must be an object")
    return value


def load_live_entry_config(path: Path | str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MethodRegistryError(f"yield live-entry config is unreadable: {exc}") from exc
    payload = dict(_require_mapping(document, "yield live-entry config"))
    if payload.get("schema") != SCHEMA:
        raise MethodRegistryError("yield live-entry schema differs")
    if payload.get("program") != CONTACT_PROGRAM:
        raise MethodRegistryError("yield live-entry TP program is not step5d_contact_six_qp_v1")
    if payload.get("home_program") != HOME_PROGRAM:
        raise MethodRegistryError("yield live-entry Home program is not step5d_contact_home_v1")
    if payload.get("physical_qualification") is not False:
        raise MethodRegistryError("live-entry config cannot declare physical qualification")
    if payload.get("machine_evidence_fresh") is not False:
        raise MethodRegistryError("live-entry config cannot declare fresh machine evidence")
    if not isinstance(payload.get("user_standing_live_authority"), bool):
        raise MethodRegistryError("standing live authority must be explicitly boolean")
    task = _require_mapping(payload.get("task"), "task")
    if float(task.get("normal_force_n")) != 5.0:
        raise MethodRegistryError("live-entry task force is not 5 N")
    if float(task.get("along_amplitude_m")) != 0.08:
        raise MethodRegistryError("live-entry along amplitude is not 80 mm")
    if float(task.get("lateral_amplitude_m")) != 0.02:
        raise MethodRegistryError("live-entry lateral amplitude is not 20 mm")
    if float(task.get("omega_rad_s")) != 0.1:
        raise MethodRegistryError("live-entry omega is not 0.1 rad/s")
    if not math_isclose(float(task.get("period_s")), PERIOD_S):
        raise MethodRegistryError("live-entry period is not the formal PATH period")
    durations = _require_mapping(payload.get("durations"), "durations")
    diagnostic = tuple(float(item) for item in durations.get("diagnostic_s") or ())
    if diagnostic != DIAGNOSTIC_DURATIONS_S:
        raise MethodRegistryError("live-entry diagnostic durations are not 2 s and 10 s")
    if not math_isclose(float(durations.get("full_period_s")), PERIOD_S):
        raise MethodRegistryError("live-entry full duration is not the formal PATH period")
    methods = _require_mapping(payload.get("methods"), "methods")
    if tuple(methods) != NATIVE_METHODS + REGISTERED_UNAVAILABLE:
        raise MethodRegistryError("live-entry method set differs")
    return payload


def math_isclose(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= 1e-12


def load_method_records(config: Mapping[str, Any] | None = None) -> dict[str, MethodRecord]:
    payload = config if config is not None else load_live_entry_config()
    methods = _require_mapping(payload.get("methods"), "methods")
    records: dict[str, MethodRecord] = {}
    for name, raw in methods.items():
        item = _require_mapping(raw, f"{name} method")
        family = str(item.get("family") or "")
        available = item.get("available")
        if not isinstance(available, bool):
            raise MethodRegistryError(f"{name} availability is not typed")
        if name in NATIVE_METHODS:
            if family != NATIVE_FAMILY or available is not True:
                raise MethodRegistryError(f"native method {name} lost its native source binding")
            if item.get("source_binding") != "yield_native_route_v1":
                raise MethodRegistryError(f"native method {name} source binding differs")
            if item.get("provider") != "YieldContactProvider":
                raise MethodRegistryError(f"native method {name} provider differs")
            records[name] = MethodRecord(
                name=name,
                family=family,
                available=True,
                provider="YieldContactProvider",
                source_binding="yield_native_route_v1",
                reason=None,
            )
            continue
        if name not in REGISTERED_UNAVAILABLE:
            raise MethodRegistryError(f"unknown live method {name}")
        if available is not False:
            raise MethodRegistryError(f"{name} cannot be marked executable in this entry")
        if item.get("provider") or item.get("source_binding"):
            raise MethodRegistryError(f"{name} must not carry a native or RNN executable binding")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason:
            raise MethodRegistryError(f"{name} unavailable reason is missing")
        records[name] = MethodRecord(
            name=name,
            family=family,
            available=False,
            provider=None,
            source_binding=None,
            reason=reason,
        )
    if records["TASE_RNN"].family != "tase_rnn" or records["TASE_QP"].family != "tase_qp":
        raise MethodRegistryError("unavailable TASE families were erased")
    return records


def resolve_method(name: str, *, config: Mapping[str, Any] | None = None) -> MethodRecord:
    records = load_method_records(config)
    if name not in records:
        raise MethodRegistryError(f"unknown live method {name!r}")
    record = records[name]
    if not record.available:
        raise MethodUnavailableError(
            f"{name} is registered but unavailable: {record.reason}"
        )
    if record.family != NATIVE_FAMILY:
        raise MethodUnavailableError(f"{name} is not a native yield method")
    return record


def native_status_payload(records: Mapping[str, MethodRecord] | None = None) -> dict[str, Any]:
    loaded = records if records is not None else load_method_records()
    return {
        "native_methods": [loaded[name].as_dict() for name in NATIVE_METHODS],
        "registered_unavailable": [loaded[name].as_dict() for name in REGISTERED_UNAVAILABLE],
        "native_route_schema": NATIVE_SCHEMA,
        "native_claim_scope": NATIVE_CLAIM_SCOPE,
        "native_source_binding_retained": True,
        "rnn_hash_or_profile_when_native": False,
    }
