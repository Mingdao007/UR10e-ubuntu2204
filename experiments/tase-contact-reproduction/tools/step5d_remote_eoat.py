#!/usr/bin/env python3
"""Fail-closed Remote EOAT apply/readback for the legacy V3/r034 profile.

Default execution is offline.  Live execution requires ``--apply``, a
profile-bound physical-install acknowledgement, and a durable receipt path.
The only write seam is one Secondary-port URScript send; no Load/Play/motion,
zero, tare, rollback, or bridge action is owned here.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import socket
import time
from typing import Any, Callable, ContextManager, Mapping, Protocol, Sequence

from step5d_autotune_v3.atomic_io import atomic_bytes
from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.rtde_client import RTDEClient
from ur10e_parallel import ResourceProfile, writer_lease


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_PATH = ROOT / "config/step5d/legacy_old_eoat_v3_profile.json"
DEFAULT_ROBOT_HOST = "192.168.1.18"
DEFAULT_DASHBOARD_PORT, DEFAULT_RTDE_PORT, DEFAULT_SECONDARY_PORT = 29999, 30004, 30002
PROFILE_SCHEMA, PROFILE_REVISION = "step5d.remote-eoat-profile/v1", 1
RECEIPT_SCHEMA, RECEIPT_REVISION = "step5d.remote-eoat-apply-receipt/v1", 1
ACK_SCHEMA, ACK_REVISION = "step5d.remote-eoat-physical-install-ack/v1", 1
POLICY_SCHEMA, POLICY_REVISION = "step5d.remote-eoat-runtime-policy/v1", 1
PROFILE_ID, LINEAGE = "legacy-old-eoat-v3", "V3/r034"
RELEASE_MANIFEST_SHA256 = "9adf3791ec322bacbddce0c16a7fcfd588c9f232cde3ccb438baa9e9c97e2df5"
PRIOR_GET_SHA256 = "5887ded371e2e43aac60ae2340cabfbcb91ff7adf027c26e554a460864c3b625"
RELEASE_MANIFEST_PATH = f"config/step5d/releases/{RELEASE_MANIFEST_SHA256}/manifest.json"
EXPECTED_PAYLOAD_KG = 0.404
EXPECTED_COG_M = (-0.00044, -0.00094, 0.0268)
EXPECTED_TCP_M_RAD = (0.0, 0.0, 0.1221, 0.0, 0.0, 0.0)
EXPECTED_TOLERANCES = {
    "payload_kg": 0.0005,
    "cog_m": 0.00005,
    "tcp_m_rad": 0.00005,
    "stationary_linear_m_s": 0.0005,
    "stationary_angular_rad_s": 0.005,
}
ZERO_INERTIA = (0, 0, 0, 0, 0, 0)
DASHBOARD_COMMANDS = ("is in remote control", "safetymode", "robotmode", "running", "programState")
RTDE_SPEED_FIELD = "actual_TCP_speed"
RTDE_READBACK_FIELDS = ("payload", "payload_cog", "tcp_offset", RTDE_SPEED_FIELD)
WRITER_TASK = "step5d-remote-eoat-apply"
PHYSICAL_INSTALL_ACK_PREFIX = "PHYSICAL_INSTALL_ACK "
COMMAND_SEMANTICS = (
    "command_attempted enters the send transaction; command_sent means sendall completed; "
    "verified requires exact RTDE readback plus the post Dashboard gate; no field asserts setter execution"
)


class EoatError(RuntimeError):
    pass


class ProfileValidationError(EoatError):
    pass


class PhaseFailure(EoatError):
    def __init__(self, phase: str, detail: str, *, uncertain: bool = False) -> None:
        super().__init__(detail)
        self.phase, self.detail, self.uncertain = phase, detail, uncertain


def utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, role: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EoatError(f"{role} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or utc_datetime()).astimezone(timezone.utc).isoformat(timespec="milliseconds")


def utc_now() -> str:
    return _iso()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(exc: BaseException) -> str:
    detail = str(exc).strip().replace("\n", " ") or type(exc).__name__
    return f"{type(exc).__name__}: {detail[:400]}"


def _finite(value: Any, role: str, error: type[Exception] = EoatError) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise error(f"{role} must be finite numeric")
    return float(value)


def _vector(value: Any, size: int, role: str, error: type[Exception] = EoatError) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != size:
        raise error(f"{role} must be a finite {size}-element vector")
    return tuple(_finite(item, f"{role}[{index}]", error) for index, item in enumerate(value))


def _is_sha(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _real_file(path: Path, role: str, error: type[Exception] = EoatError) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise error(f"{role} must be a real regular file")
    resolved = candidate.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise error(f"{role} must resolve to a real regular file")
    return resolved


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


@dataclass(frozen=True)
class EoatTolerances:
    payload_kg: float
    cog_m: float
    tcp_m_rad: float
    stationary_linear_m_s: float
    stationary_angular_rad_s: float

    def as_dict(self) -> dict[str, float]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EoatProfile:
    schema: str
    revision: int
    profile_id: str
    lineage: str
    release_manifest_path: str
    release_manifest_sha256: str
    payload_kg: float
    cog_m: tuple[float, ...]
    tcp_m_rad: tuple[float, ...]
    prior_real_fresh_controller_get_sha256: str
    tolerances: EoatTolerances
    profile_sha256: str
    profile_path: str
    profile_file_sha256: str

    def receipt_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "revision": self.revision,
            "profile_id": self.profile_id,
            "lineage": self.lineage,
            "profile_sha256": self.profile_sha256,
            "profile_file_sha256": self.profile_file_sha256,
            "release_manifest_path": self.release_manifest_path,
            "release_manifest_sha256": self.release_manifest_sha256,
            "payload_kg": self.payload_kg,
            "cog_m": list(self.cog_m),
            "tcp_m_rad": list(self.tcp_m_rad),
            "prior_real_fresh_controller_get_sha256": self.prior_real_fresh_controller_get_sha256,
            "tolerances": self.tolerances.as_dict(),
            "profile_path": self.profile_path,
        }


def _profile_document(document: Mapping[str, Any], root: Path) -> EoatProfile:
    keys = {"schema", "revision", "profile_id", "lineage", "release", "payload_kg", "cog_m", "tcp_m_rad", "evidence", "tolerances", "profile_sha256"}
    if set(document) != keys or document.get("schema") != PROFILE_SCHEMA or document.get("revision") != PROFILE_REVISION:
        raise ProfileValidationError("profile schema/revision/keys differ")
    if document["profile_id"] != PROFILE_ID or document["lineage"] != LINEAGE:
        raise ProfileValidationError("profile id or lineage differs")
    declared = document["profile_sha256"]
    unsigned = dict(document)
    unsigned.pop("profile_sha256")
    if not _is_sha(declared) or canonical_sha256(unsigned) != declared:
        raise ProfileValidationError("profile_sha256 mismatch")
    release = document["release"]
    if not isinstance(release, Mapping) or set(release) != {"manifest_path", "manifest_sha256"}:
        raise ProfileValidationError("release binding schema differs")
    if release["manifest_path"] != RELEASE_MANIFEST_PATH or release["manifest_sha256"] != RELEASE_MANIFEST_SHA256:
        raise ProfileValidationError("release binding differs")
    root = Path(root).resolve(strict=True)
    manifest = _real_file(root / RELEASE_MANIFEST_PATH, "release manifest", ProfileValidationError)
    try:
        manifest.relative_to(root)
    except ValueError as exc:
        raise ProfileValidationError("release manifest is outside repository root") from exc
    if _sha256_file(manifest) != RELEASE_MANIFEST_SHA256:
        raise ProfileValidationError("release manifest file SHA-256 mismatch")
    payload = _finite(document["payload_kg"], "payload_kg", ProfileValidationError)
    cog = _vector(document["cog_m"], 3, "cog_m", ProfileValidationError)
    tcp = _vector(document["tcp_m_rad"], 6, "tcp_m_rad", ProfileValidationError)
    if not 0.0 < payload <= 100.0 or any(abs(v) > 10.0 for v in cog) or any(abs(v) > 10.0 for v in tcp[:3]) or any(abs(v) > 2.0 * math.pi for v in tcp[3:]):
        raise ProfileValidationError("profile finite physical bounds violated")
    if payload != EXPECTED_PAYLOAD_KG or cog != EXPECTED_COG_M or tcp != EXPECTED_TCP_M_RAD:
        raise ProfileValidationError("exact legacy-old-eoat-v3 values differ")
    evidence = document["evidence"]
    if not isinstance(evidence, Mapping) or set(evidence) != {"prior_real_fresh_controller_get_sha256"} or evidence["prior_real_fresh_controller_get_sha256"] != PRIOR_GET_SHA256:
        raise ProfileValidationError("prior fresh controller GET evidence differs")
    tolerance_doc = document["tolerances"]
    if not isinstance(tolerance_doc, Mapping) or set(tolerance_doc) != set(EXPECTED_TOLERANCES):
        raise ProfileValidationError("tolerance schema differs")
    tolerance_values = {}
    for name, expected in EXPECTED_TOLERANCES.items():
        value = _finite(tolerance_doc[name], f"tolerances.{name}", ProfileValidationError)
        if not 0.0 < value <= 1.0 or value != expected:
            raise ProfileValidationError(f"tolerances.{name} differs")
        tolerance_values[name] = value
    return EoatProfile(
        PROFILE_SCHEMA, PROFILE_REVISION, PROFILE_ID, LINEAGE, RELEASE_MANIFEST_PATH, RELEASE_MANIFEST_SHA256,
        payload, cog, tcp, PRIOR_GET_SHA256, EoatTolerances(**tolerance_values), declared, "", "",
    )


def load_profile(path: Path, *, root: Path = ROOT) -> EoatProfile:
    path = _real_file(Path(path), "profile", ProfileValidationError)
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileValidationError(f"profile JSON read failed: {_safe_error(exc)}") from exc
    if not isinstance(document, Mapping):
        raise ProfileValidationError("profile root must be an object")
    return replace(_profile_document(document, root), profile_path=str(path), profile_file_sha256=_sha256_bytes(raw))


def _number_text(value: float | int) -> str:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ProfileValidationError("non-finite render value")
    return "0" if parsed == 0.0 else repr(parsed)


def _vector_text(values: Sequence[float | int]) -> str:
    return "[" + ",".join(_number_text(v) for v in values) + "]"


def render_urscript(profile: EoatProfile) -> str:
    script = (
        "sec legacy_old_eoat_v3():\n"
        f"  set_target_payload({_number_text(profile.payload_kg)},{_vector_text(profile.cog_m)},{_vector_text(ZERO_INERTIA)})\n"
        f"  set_tcp(p{_vector_text(profile.tcp_m_rad)})\n"
        "end\n"
    )
    if script.count("sec ") != 1 or script.count("set_target_payload(") != 1 or script.count("set_tcp(") != 1:
        raise ProfileValidationError("rendered program must contain one sec/payload/TCP setter")
    return script


@dataclass(frozen=True)
class DashboardObservation:
    raw: Mapping[str, str]
    remote_control: bool
    safety: str
    robotmode: str
    running: bool
    program_state: str
    observed_at: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, observed_at: str | None = None) -> "DashboardObservation":
        if set(raw) != set(DASHBOARD_COMMANDS):
            raise EoatError("Dashboard observation fields differ")
        def text(name: str) -> str:
            value = raw[name]
            if not isinstance(value, str):
                raise EoatError(f"Dashboard {name} is not text")
            return value.strip()
        def after(name: str) -> str:
            line = text(name)
            if ":" not in line:
                raise EoatError(f"Dashboard {name} lacks value delimiter")
            return line.split(":", 1)[1].strip()
        remote, running = text("is in remote control").lower(), after("running").lower()
        if remote not in {"true", "false"} or running not in {"true", "false"}:
            raise EoatError("Dashboard boolean is invalid")
        return cls({k: text(k) for k in DASHBOARD_COMMANDS}, remote == "true", after("safetymode").upper(), after("robotmode").upper(), running == "true", text("programState").upper(), observed_at or utc_now())

    def as_dict(self) -> dict[str, Any]:
        return {"observed_at": self.observed_at, "raw": dict(self.raw), "remote_control": self.remote_control, "safety": self.safety, "robotmode": self.robotmode, "running": self.running, "program_state": self.program_state}


@dataclass(frozen=True)
class RTDEObservation:
    values: Mapping[str, Any]
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {"observed_at": self.observed_at, "values": _json_safe(self.values)}


class DashboardReader(Protocol):
    def read(self, host: str, port: int, timeout_s: float) -> DashboardObservation | Mapping[str, Any]: ...


class RTDEReader(Protocol):
    def read(self, host: str, port: int, fields: Sequence[str], timeout_s: float) -> RTDEObservation | Mapping[str, Any]: ...


class SecondaryWriter(Protocol):
    def send(self, host: str, port: int, script: str, timeout_s: float) -> None: ...


class RepositoryDashboardReader:
    def read(self, host: str, port: int, timeout_s: float) -> DashboardObservation:
        return DashboardObservation.from_raw(dashboard_exchange(host, list(DASHBOARD_COMMANDS), port=port, timeout=timeout_s))


class RepositoryRTDEReader:
    def __init__(self, *, frequency_hz: float = 10.0) -> None:
        if type(frequency_hz) not in (int, float) or not 0.0 < frequency_hz <= 500.0 or not math.isfinite(float(frequency_hz)):
            raise ValueError("RTDE frequency must be in (0,500]")
        self.frequency_hz = frequency_hz

    def read(self, host: str, port: int, fields: Sequence[str], timeout_s: float) -> RTDEObservation:
        with RTDEClient(host, port=port, timeout=timeout_s) as client:
            client.negotiate()
            recipe, types = client.setup_outputs(self.frequency_hz, list(fields))
            client.start()
            values = client.recv_recipe_sample(recipe, types)
        return RTDEObservation(dict(zip(fields, values)), utc_now())


class SecondarySocketWriter:
    def send(self, host: str, port: int, script: str, timeout_s: float) -> None:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            sock.sendall(script.encode("ascii"))
            sock.shutdown(socket.SHUT_WR)


def canonical_writer_lease(task: str) -> ContextManager[Mapping[str, Any]]:
    return writer_lease(ResourceProfile.from_env(), task, blocking=False)


@dataclass(frozen=True)
class RemoteTransport:
    dashboard: DashboardReader
    rtde: RTDEReader
    secondary: SecondaryWriter
    lease_factory: Callable[[str], ContextManager[Mapping[str, Any]]]


def default_transport() -> RemoteTransport:
    return RemoteTransport(RepositoryDashboardReader(), RepositoryRTDEReader(), SecondarySocketWriter(), canonical_writer_lease)


@dataclass(frozen=True)
class RuntimePolicy:
    dashboard_timeout_s: float = 2.0
    rtde_timeout_s: float = 2.0
    secondary_timeout_s: float = 2.0
    readback_timeout_s: float = 3.0
    readback_poll_interval_s: float = 0.1
    max_readback_polls: int = 10
    ack_max_age_s: float = 300.0

    def validate(self) -> None:
        for name in ("dashboard_timeout_s", "rtde_timeout_s", "secondary_timeout_s", "readback_timeout_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(float(value)) or not 0.0 < value <= 60.0:
                raise EoatError(f"{name} must be in (0,60]")
        if type(self.readback_poll_interval_s) not in (int, float) or not math.isfinite(float(self.readback_poll_interval_s)) or not 0.0 <= self.readback_poll_interval_s <= 5.0:
            raise EoatError("readback_poll_interval_s must be in [0,5]")
        if type(self.max_readback_polls) is not int or not 1 <= self.max_readback_polls <= 100:
            raise EoatError("max_readback_polls must be in [1,100]")
        if type(self.ack_max_age_s) not in (int, float) or not math.isfinite(float(self.ack_max_age_s)) or not 0.0 < self.ack_max_age_s <= 86400.0:
            raise EoatError("ack_max_age_s must be in (0,86400]")

    def as_dict(self) -> dict[str, Any]:
        return {"schema": POLICY_SCHEMA, "revision": POLICY_REVISION, **self.__dict__}


@dataclass(frozen=True)
class PhysicalInstallAck:
    profile_id: str
    profile_sha256: str
    release_manifest_sha256: str
    operator: str
    logged_at: str
    logged_at_utc: str
    age_s: float
    max_age_s: float
    ack_sha256: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def load_physical_install_ack(path: Path, profile: EoatProfile, *, now: datetime | None = None, max_age_s: float = 300.0) -> PhysicalInstallAck:
    if type(max_age_s) not in (int, float) or not math.isfinite(float(max_age_s)) or not 0.0 < max_age_s <= 86400.0:
        raise EoatError("ack_max_age_s must be in (0,86400]")
    path = _real_file(path, "physical-install acknowledgement")
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EoatError(f"physical-install acknowledgement read failed: {_safe_error(exc)}") from exc
    keys = {"schema", "revision", "profile_id", "profile_sha256", "release_manifest_sha256", "acknowledgement", "operator", "logged_at"}
    if not isinstance(document, Mapping) or set(document) != keys:
        raise EoatError("physical-install acknowledgement schema keys differ")
    if document["schema"] != ACK_SCHEMA or type(document["revision"]) is not int or document["revision"] != ACK_REVISION:
        raise EoatError("physical-install acknowledgement schema/revision differs")
    if document["profile_id"] != profile.profile_id or document["profile_sha256"] != profile.profile_sha256 or document["release_manifest_sha256"] != profile.release_manifest_sha256:
        raise EoatError("physical-install acknowledgement profile binding differs")
    if document["acknowledgement"] != PHYSICAL_INSTALL_ACK_PREFIX + profile.profile_id:
        raise EoatError("physical-install acknowledgement text is not exact")
    operator, logged_at = document["operator"], document["logged_at"]
    if not isinstance(operator, str) or not operator.strip() or "\n" in operator or not isinstance(logged_at, str) or not logged_at.strip():
        raise EoatError("physical-install acknowledgement operator/timestamp is invalid")
    try:
        logged = _utc(datetime.fromisoformat(logged_at.replace("Z", "+00:00")), "logged_at")
        current = _utc(now or utc_datetime(), "ack now")
    except ValueError as exc:
        raise EoatError("logged_at is not valid ISO-8601") from exc
    if logged > current:
        raise EoatError("physical-install acknowledgement is from the future")
    age_s = (current - logged).total_seconds()
    if age_s > max_age_s:
        raise EoatError(f"physical-install acknowledgement is stale: age_s={age_s:.3f}")
    return PhysicalInstallAck(profile.profile_id, profile.profile_sha256, profile.release_manifest_sha256, operator, logged_at, _iso(logged), age_s, float(max_age_s), _sha256_bytes(raw), str(path))


def _coerce_dashboard(value: DashboardObservation | Mapping[str, Any]) -> DashboardObservation:
    return value if isinstance(value, DashboardObservation) else DashboardObservation.from_raw(value)


def _coerce_rtde(value: RTDEObservation | Mapping[str, Any]) -> RTDEObservation:
    if isinstance(value, RTDEObservation):
        return value
    if not isinstance(value, Mapping):
        raise EoatError("RTDE adapter returned an invalid observation")
    values = value.get("values", value)
    if not isinstance(values, Mapping):
        raise EoatError("RTDE adapter values are not a mapping")
    stamp = value.get("observed_at")
    return RTDEObservation(dict(values), stamp if isinstance(stamp, str) and stamp else utc_now())


def _dashboard_gate(observation: DashboardObservation) -> None:
    state = observation.program_state.split(maxsplit=1)[0] if observation.program_state else ""
    checks = ((observation.remote_control, "Dashboard Remote control is not true"), (observation.safety == "NORMAL", "Dashboard Safety is not NORMAL"), (observation.robotmode == "RUNNING", "Dashboard robotmode is not RUNNING"), (not observation.running, "Dashboard program running is not false"), (state == "STOPPED", "Dashboard programState is not STOPPED"))
    for ok, message in checks:
        if not ok:
            raise EoatError(message)


def _stationary_error(speed: Any, tolerances: EoatTolerances) -> str | None:
    values = _vector(speed, 6, RTDE_SPEED_FIELD)
    linear = math.sqrt(sum(v * v for v in values[:3]))
    angular = math.sqrt(sum(v * v for v in values[3:]))
    if linear > tolerances.stationary_linear_m_s:
        return f"stationary linear norm {linear} exceeds {tolerances.stationary_linear_m_s}"
    if angular > tolerances.stationary_angular_rad_s:
        return f"stationary angular norm {angular} exceeds {tolerances.stationary_angular_rad_s}"
    return None


def _readback_errors(profile: EoatProfile, values: Mapping[str, Any]) -> list[str]:
    if set(values) != set(RTDE_READBACK_FIELDS):
        return ["RTDE readback fields differ"]
    errors: list[str] = []
    try:
        if abs(_finite(values["payload"], "payload") - profile.payload_kg) > profile.tolerances.payload_kg:
            errors.append("payload tolerance exceeded")
    except EoatError as exc:
        errors.append(str(exc))
    for name, expected, tolerance, size in (("payload_cog", profile.cog_m, profile.tolerances.cog_m, 3), ("tcp_offset", profile.tcp_m_rad, profile.tolerances.tcp_m_rad, 6)):
        try:
            actual = _vector(values[name], size, name)
            if max(abs(a - b) for a, b in zip(actual, expected)) > tolerance:
                errors.append(f"{name} tolerance exceeded")
        except EoatError as exc:
            errors.append(str(exc))
    try:
        stationary = _stationary_error(values[RTDE_SPEED_FIELD], profile.tolerances)
        if stationary:
            errors.append(stationary)
    except EoatError as exc:
        errors.append(str(exc))
    return errors


def _validate_endpoint(host: str, ports: Sequence[int], secondary_port: int) -> None:
    if not isinstance(host, str) or not host.strip() or any(c in host for c in "\r\n"):
        raise EoatError("robot host is invalid")
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise EoatError("controller port is outside [1,65535]")
    if secondary_port != DEFAULT_SECONDARY_PORT:
        raise EoatError(f"Secondary script port is fixed at {DEFAULT_SECONDARY_PORT}")


def _validate_receipt_target(path: Path) -> None:
    path = Path(path).expanduser()
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise EoatError("receipt target is not a regular file")
    parent = path.parent
    while True:
        if parent.is_symlink() or parent.exists() and not parent.is_dir():
            raise EoatError("receipt parent is unsafe")
        if parent == parent.parent:
            break
        parent = parent.parent


def seal_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(record)
    body.pop("receipt_sha256", None)
    return {**body, "receipt_sha256": canonical_sha256(body)}


def write_receipt(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    sealed = seal_receipt(record)
    atomic_bytes(Path(path), canonical_json_bytes(sealed) + b"\n")
    return sealed


def _new_record(profile_path: Path, policy: RuntimePolicy, mode: str, host: str, ports: tuple[int, int, int], started_at: str) -> dict[str, Any]:
    dashboard_port, rtde_port, secondary_port = ports
    return {
        "schema": RECEIPT_SCHEMA, "revision": RECEIPT_REVISION, "mode": mode, "started_at": started_at, "finished_at": None,
        "phase": "initialization", "status": "running", "blockers": [],
        "controller": {"host": host, "dashboard_port": dashboard_port, "rtde_port": rtde_port, "secondary_port": secondary_port},
        "profile": {"profile_path": str(profile_path), "profile_id": None, "profile_sha256": None, "release_manifest_sha256": None},
        "release_manifest_sha256": None, "writer_lease_owner": None, "physical_install_ack": None, "policy": policy.as_dict(),
        "script": None, "script_sha256": None, "command_semantics": COMMAND_SEMANTICS,
        "command_attempted": False, "send_seam_entered": False, "command_sent": False, "command_send_state": "not_attempted", "verified": False, "verification_basis": None,
        "checkpoint_at": None, "observations": {"pre": {}, "post": {}}, "receipt_path": None,
    }


def _finish(record: dict[str, Any], receipt_path: Path | None) -> dict[str, Any]:
    record["finished_at"] = utc_now()
    record["receipt_path"] = str(receipt_path) if receipt_path else None
    try:
        return write_receipt(receipt_path, record) if receipt_path else seal_receipt(record)
    except Exception as exc:
        record["status"] = "uncertain_controller_state" if record["send_seam_entered"] else "failed_closed"
        record["phase"] = "receipt"
        record["blockers"].append("receipt_write_failed: " + _safe_error(exc))
        return seal_receipt(record)


def _attach_profile(record: dict[str, Any], profile: EoatProfile, script: str) -> None:
    record["profile"], record["release_manifest_sha256"] = profile.receipt_dict(), profile.release_manifest_sha256
    record["script"], record["script_sha256"] = script, _sha256_bytes(script.encode("ascii"))


def _checkpoint(record: dict[str, Any], receipt_path: Path) -> None:
    record.update(phase="send_secondary_script", status="uncertain_controller_state", command_attempted=True, command_sent=False, send_seam_entered=False, command_send_state="not_sent", verified=False, checkpoint_at=utc_now())
    try:
        write_receipt(receipt_path, record)
    except Exception as exc:
        raise PhaseFailure("send_checkpoint", "pre-send checkpoint write failed: " + _safe_error(exc)) from exc


def _poll_readback(profile: EoatProfile, transport: RemoteTransport, host: str, port: int, policy: RuntimePolicy, record: dict[str, Any], monotonic: Callable[[], float], sleep: Callable[[float], None]) -> None:
    deadline, entries, last = monotonic() + policy.readback_timeout_s, [], "no RTDE readback"
    record["observations"]["post"]["rtde_readback"] = entries
    for attempt in range(1, policy.max_readback_polls + 1):
        remaining = deadline - monotonic()
        if attempt > 1 and remaining <= 0.0:
            break
        try:
            observation = _coerce_rtde(transport.rtde.read(host, port, RTDE_READBACK_FIELDS, min(policy.rtde_timeout_s, max(0.001, remaining))))
            errors = _readback_errors(profile, observation.values)
            entry = {**observation.as_dict(), "attempt": attempt, "verification_errors": errors}
            entries.append(entry)
            if not errors:
                return
            last = "; ".join(errors)
        except Exception as exc:
            last = _safe_error(exc)
            entries.append({"attempt": attempt, "error": last})
        remaining = deadline - monotonic()
        if attempt < policy.max_readback_polls and remaining > 0.0 and policy.readback_poll_interval_s:
            sleep(min(policy.readback_poll_interval_s, remaining))
    raise EoatError(f"bounded RTDE readback failed after {len(entries)} poll(s): {last}")


def execute(*, profile_path: Path = DEFAULT_PROFILE_PATH, root: Path = ROOT, apply: bool = False, physical_install_ack: Path | None = None, receipt_path: Path | None = None, robot_host: str = DEFAULT_ROBOT_HOST, dashboard_port: int = DEFAULT_DASHBOARD_PORT, rtde_port: int = DEFAULT_RTDE_PORT, secondary_port: int = DEFAULT_SECONDARY_PORT, policy: RuntimePolicy | None = None, transport: RemoteTransport | None = None, wall_clock: Callable[[], datetime] = utc_datetime, monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    selected_policy = policy or RuntimePolicy()
    ports = (dashboard_port, rtde_port, secondary_port)
    wall_error = None
    try:
        wall_now = _utc(wall_clock(), "wall_clock")
    except Exception as exc:
        wall_now = utc_datetime()
        wall_error = _safe_error(exc)
    record = _new_record(Path(profile_path), selected_policy, "live_apply" if apply else "offline", robot_host, ports, _iso(wall_now))
    try:
        if wall_error:
            raise PhaseFailure("authorization", "wall_clock injection is invalid: " + wall_error)
        _validate_endpoint(robot_host, ports, secondary_port)
        selected_policy.validate()
        if apply:
            if physical_install_ack is None:
                raise PhaseFailure("authorization", "--apply requires a physical-install acknowledgement path")
            if receipt_path is None:
                raise PhaseFailure("authorization", "--apply requires a durable receipt path")
            _validate_receipt_target(receipt_path)
        profile = load_profile(Path(profile_path), root=Path(root))
        script = render_urscript(profile)
        _attach_profile(record, profile, script)
    except PhaseFailure as exc:
        record["phase"], record["status"] = exc.phase, "failed_closed"
        record["blockers"].append(exc.detail)
        return _finish(record, receipt_path)
    except Exception as exc:
        record["phase"], record["status"] = "profile_validation", "failed_closed"
        record["blockers"].append(_safe_error(exc))
        return _finish(record, receipt_path)
    if not apply:
        record["phase"], record["status"] = "offline_complete", "offline_validated"
        return _finish(record, receipt_path)
    try:
        record["physical_install_ack"] = load_physical_install_ack(Path(physical_install_ack), profile, now=wall_now, max_age_s=selected_policy.ack_max_age_s).as_dict()
    except Exception as exc:
        record["phase"], record["status"] = "authorization", "failed_closed"
        record["blockers"].append(_safe_error(exc))
        return _finish(record, receipt_path)
    selected_transport = transport or default_transport()
    try:
        with selected_transport.lease_factory(WRITER_TASK) as owner:
            if not isinstance(owner, Mapping) or owner.get("task") != WRITER_TASK:
                raise PhaseFailure("lease", "writer lease owner task differs")
            record["writer_lease_owner"], record["phase"] = _json_safe(owner), "pre_dashboard"
            try:
                observation = _coerce_dashboard(selected_transport.dashboard.read(robot_host, dashboard_port, selected_policy.dashboard_timeout_s))
                record["observations"]["pre"]["dashboard"] = observation.as_dict()
                _dashboard_gate(observation)
            except Exception as exc:
                raise PhaseFailure("pre_dashboard", _safe_error(exc)) from exc
            record["phase"] = "pre_rtde_stationary"
            try:
                observation = _coerce_rtde(selected_transport.rtde.read(robot_host, rtde_port, (RTDE_SPEED_FIELD,), selected_policy.rtde_timeout_s))
                record["observations"]["pre"]["rtde"] = observation.as_dict()
                if set(observation.values) != {RTDE_SPEED_FIELD}:
                    raise EoatError("pre-write RTDE fields differ")
                stationary = _stationary_error(observation.values[RTDE_SPEED_FIELD], profile.tolerances)
                if stationary:
                    raise EoatError(stationary)
            except Exception as exc:
                raise PhaseFailure("pre_rtde_stationary", _safe_error(exc)) from exc
            _checkpoint(record, receipt_path)
            record["send_seam_entered"], record["command_send_state"] = True, "unknown"
            try:
                selected_transport.secondary.send(robot_host, secondary_port, script, selected_policy.secondary_timeout_s)
            except Exception as exc:
                raise PhaseFailure("send_secondary_script", _safe_error(exc), uncertain=True) from exc
            record["command_sent"], record["command_send_state"] = True, "sendall_completed"
            readback_error = None
            record["phase"] = "post_rtde_readback"
            try:
                _poll_readback(profile, selected_transport, robot_host, rtde_port, selected_policy, record, monotonic, sleep)
            except Exception as exc:
                readback_error = _safe_error(exc)
            dashboard_error = None
            record["phase"] = "post_dashboard_gate"
            try:
                observation = _coerce_dashboard(selected_transport.dashboard.read(robot_host, dashboard_port, selected_policy.dashboard_timeout_s))
                record["observations"]["post"]["dashboard"] = observation.as_dict()
                _dashboard_gate(observation)
            except Exception as exc:
                dashboard_error = _safe_error(exc)
                record["observations"]["post"]["dashboard_error"] = dashboard_error
            if readback_error:
                raise PhaseFailure("post_rtde_readback", readback_error, uncertain=True)
            if dashboard_error:
                raise PhaseFailure("post_dashboard_gate", dashboard_error, uncertain=True)
            entries = record["observations"]["post"].get("rtde_readback", [])
            if not entries or entries[-1].get("verification_errors"):
                raise PhaseFailure("post_verify", "exact RTDE readback was not verified", uncertain=True)
            record["verified"], record["verification_basis"] = True, "exact RTDE readback within profile tolerances + post Dashboard gate"
        record["phase"], record["status"] = "complete", "applied_and_verified"
        return _finish(record, receipt_path)
    except PhaseFailure as exc:
        record["phase"] = exc.phase
        record["status"] = "uncertain_controller_state" if record["send_seam_entered"] or exc.uncertain else "failed_closed"
        record["blockers"].append(exc.detail)
        return _finish(record, receipt_path)
    except Exception as exc:
        record["status"] = "uncertain_controller_state" if record["send_seam_entered"] else "failed_closed"
        record["blockers"].append(_safe_error(exc))
        return _finish(record, receipt_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--robot-host", default=DEFAULT_ROBOT_HOST)
    parser.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT)
    parser.add_argument("--rtde-port", type=int, default=DEFAULT_RTDE_PORT)
    parser.add_argument("--secondary-port", type=int, default=DEFAULT_SECONDARY_PORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--physical-install-ack", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--readback-timeout-s", type=float, default=3.0)
    parser.add_argument("--readback-poll-interval-s", type=float, default=0.1)
    parser.add_argument("--max-readback-polls", type=int, default=10)
    parser.add_argument("--ack-max-age-s", type=float, default=300.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = execute(
        profile_path=args.profile, root=args.root, apply=args.apply, physical_install_ack=args.physical_install_ack, receipt_path=args.receipt,
        robot_host=args.robot_host, dashboard_port=args.dashboard_port, rtde_port=args.rtde_port, secondary_port=args.secondary_port,
        policy=RuntimePolicy(readback_timeout_s=args.readback_timeout_s, readback_poll_interval_s=args.readback_poll_interval_s, max_readback_polls=args.max_readback_polls, ack_max_age_s=args.ack_max_age_s),
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))
    return 0 if result["status"] in {"offline_validated", "applied_and_verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
