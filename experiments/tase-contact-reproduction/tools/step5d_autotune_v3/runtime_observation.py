"""Publish machine-observed Step5d runtime attestations.

This module performs no network I/O and never writes readiness.  The live
supervisor supplies observations it already owns; this module verifies their
shape, materializes immutable evidence, and delegates the state derivation to
``governance``.
"""

from __future__ import annotations

import copy
import csv
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import tempfile
import time
from typing import Any, Mapping, Sequence

from .delivery_observation import DeliveryObservationError, fresh_get_provenance
from .governance import (
    OBSERVED_ATTESTATION_SCHEMA,
    CurrentReleaseSnapshot,
    GovernanceError,
    build_campaign_lease,
    load_current_observation,
    publish_observed_attestation,
    validate_campaign_lease,
    validate_observed_attestation,
)
from .release_contract import (
    production_process_role_paths,
    production_process_tree_fingerprint,
    release_contract_scope_for_release,
    resolve_process_argv_paths,
    validate_release_contract_result,
)
from .release_identity import load_current_release
from .runtime_gate import loaded_program_paths


class RuntimeObservationError(RuntimeError):
    """An observation is unsafe, malformed, or cannot be content-bound."""


_EVENT_FIELDS = (
    "waiting_for_play_at_unix_ns",
    "play_observed_at_unix_ns",
    "play_identity_recheck",
    "first_arm_ack",
    "trial_completion",
    "next_arm_ack",
    "campaign_terminal",
)
_EVENT_ORDER = {
    "waiting_for_play": 0,
    "play_observed": 1,
    "play_identity_recheck": 2,
    "first_arm_ack": 3,
    "trial_completion": 4,
    "next_arm_ack": 5,
    "campaign_terminal": 6,
}
_TRIPLET_SUFFIXES = (".script", ".txt", ".urp")
_CANONICAL_WRITER_SOURCE = "tools/run_step5d_autotune_v3_bridge.py"
_WRITER_SOURCE_PATHS = (
    _CANONICAL_WRITER_SOURCE,
    "tools/kunwei_rtde_bridge.py",
    "tools/step5d_p0_v8_bridge.py",
    "tools/step5d_p0_v9_bridge.py",
)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeObservationError(f"evidence is not canonical JSON: {exc}") from exc


def _strict_json(encoded: bytes, role: str) -> Mapping[str, Any]:
    def unique(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeObservationError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RuntimeObservationError(f"{role} contains non-finite {token}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeObservationError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeObservationError(f"{role} must be a JSON object")
    return value


def _sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeObservationError(f"{role} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeObservationError(f"{role} must be a positive integer")
    return value


def _optional_positive_int(value: Any, role: str) -> int | None:
    return None if value is None else _positive_int(value, role)


def _bounded_text(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise RuntimeObservationError(f"{role} must be bounded non-empty text")
    return value


def _root(path: Path, role: str, *, create: bool = False) -> Path:
    value = Path(path).expanduser().absolute()
    if value.exists():
        if value.is_symlink() or not value.is_dir():
            raise RuntimeObservationError(f"{role} must be a real directory")
        return value.resolve(strict=True)
    if not create:
        raise RuntimeObservationError(f"{role} does not exist")
    value.mkdir(parents=True, mode=0o700)
    return value.resolve(strict=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_once(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeObservationError(f"unsafe evidence path: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != encoded:
            raise RuntimeObservationError(f"content-addressed evidence conflicts: {path}")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _evidence_bytes(root: Path, role: str, encoded: bytes) -> dict[str, str]:
    if not role.replace("-", "_").isidentifier():
        raise RuntimeObservationError("evidence role is invalid")
    digest = hashlib.sha256(encoded).hexdigest()
    relative = PurePosixPath("governance", "evidence", role, f"{digest}.json")
    _write_once(root / relative, encoded)
    return {"path": relative.as_posix(), "sha256": digest}


def _evidence_json(root: Path, role: str, value: Mapping[str, Any]) -> dict[str, str]:
    return _evidence_bytes(root, role, _canonical_bytes(dict(value)))


def _reference_source(root: Path, value: Any, role: str) -> tuple[bytes, str]:
    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        expected = None
    elif isinstance(value, Mapping):
        if "path" not in value or "sha256" not in value:
            raise RuntimeObservationError(f"{role} reference fields differ")
        path = Path(str(value["path"]))
        expected = _sha256(value["sha256"], f"{role} reference")
    else:
        raise RuntimeObservationError(f"{role} reference is invalid")
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink() or not unresolved.is_file():
        raise RuntimeObservationError(f"{role} reference must be a real file")
    encoded = unresolved.read_bytes()
    observed = hashlib.sha256(encoded).hexdigest()
    if expected is not None and observed != expected:
        raise RuntimeObservationError(f"{role} reference SHA-256 differs")
    return encoded, observed


def _materialize_reference(root: Path, role: str, value: Any) -> dict[str, str]:
    encoded, _ = _reference_source(root, value, role)
    return _evidence_bytes(root, role, encoded)


def _release_value(release: Any, name: str) -> Any:
    if isinstance(release, Mapping):
        return release.get(name)
    return getattr(release, name, None)


def _validated_release(release: CurrentReleaseSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    if _release_value(release, "valid") is not True:
        raise RuntimeObservationError(
            f"current release is invalid: {_release_value(release, 'error')}"
        )
    result = {
        "manifest_sha256": _sha256(
            _release_value(release, "manifest_sha256"), "release manifest"
        ),
        "source_fingerprint": _sha256(
            _release_value(release, "source_fingerprint"), "release source fingerprint"
        ),
        "launcher_sha256": _sha256(
            _release_value(release, "launcher_sha256"), "release launcher"
        ),
        "safety_envelope_sha256": _sha256(
            _release_value(release, "safety_envelope_sha256"),
            "release safety envelope",
        ),
        "expected_loaded_program": _bounded_text(
            _release_value(release, "expected_loaded_program"),
            "expected loaded program",
        ),
    }
    triplet = _release_value(release, "expected_triplet_sha256")
    if not isinstance(triplet, Mapping) or set(triplet) != set(_TRIPLET_SUFFIXES):
        raise RuntimeObservationError("release triplet fields differ")
    result["expected_triplet_sha256"] = {
        suffix: _sha256(triplet[suffix], f"release triplet {suffix}")
        for suffix in _TRIPLET_SUFFIXES
    }
    identity = _release_value(release, "expected_tp_runtime_identity")
    result["expected_tp_runtime_identity"] = _tp_identity(identity, strict=True)
    return result


def _tp_identity(value: Any, *, strict: bool) -> dict[str, int] | None:
    fields = ("protocol_version", "digest_hi", "digest_lo")
    if not isinstance(value, Mapping) or set(value) != set(fields):
        if strict:
            raise RuntimeObservationError("TP runtime identity fields differ")
        return None
    result: dict[str, int] = {}
    for field_name in fields:
        raw = value[field_name]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or raw < 0
            or (field_name != "protocol_version" and raw >= 1 << 31)
        ):
            if strict:
                raise RuntimeObservationError(f"TP runtime identity {field_name} is invalid")
            return None
        result[field_name] = raw
    return result


def _triplet(value: Any, role: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(_TRIPLET_SUFFIXES):
        raise RuntimeObservationError(f"{role} triplet fields differ")
    return {
        suffix: _sha256(value[suffix], f"{role} {suffix}")
        for suffix in _TRIPLET_SUFFIXES
    }


def _release_contract_binding(
    *,
    experiment_root: Path,
    campaign_root: Path,
    release: Mapping[str, Any],
    release_contract_evidence: Any,
) -> tuple[dict[str, Any], str]:
    encoded, reference_sha = _reference_source(
        campaign_root, release_contract_evidence, "release contract"
    )
    payload = _strict_json(encoded, "release contract")
    try:
        identity = load_current_release(experiment_root)
        scope = release_contract_scope_for_release(experiment_root, identity)
        validate_release_contract_result(
            payload,
            expected_scope=scope,
        )
    except Exception as exc:
        raise RuntimeObservationError(f"release contract binding is invalid: {exc}") from exc
    if scope["release_manifest_sha256"] != release["manifest_sha256"]:
        raise RuntimeObservationError("release contract manifest differs")
    process_fingerprint = production_process_tree_fingerprint(experiment_root)
    environment_sha = _sha256(
        scope.get("control_environment_sha256"),
        "release contract environment",
    )
    if hashlib.sha256(encoded).hexdigest() != reference_sha:
        raise RuntimeObservationError("release contract reference changed during read")
    reference = _evidence_bytes(campaign_root, "release_contract", encoded)
    return (
        {
            "evidence": reference,
            "completed_at_unix_ns": _positive_int(
                payload.get("completed_at_unix_ns"), "contract completion"
            ),
            "manifest_sha256": release["manifest_sha256"],
            "source_fingerprint": release["source_fingerprint"],
            "launcher_sha256": release["launcher_sha256"],
            "environment_sha256": environment_sha,
            "process_tree_fingerprint": process_fingerprint,
        },
        environment_sha,
    )


def _unix_ns(value: str, role: str) -> int:
    if not isinstance(value, str) or not value:
        raise RuntimeObservationError(f"{role} must be a zoned timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeObservationError(f"{role} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeObservationError(f"{role} must include a timezone")
    return int(parsed.timestamp() * 1_000_000_000)


def _attestation_lease(
    lease: Any,
    *,
    release: Mapping[str, Any],
    expires_at_unix_ns: int | None,
) -> dict[str, Any]:
    if isinstance(lease, Mapping):
        try:
            result = validate_campaign_lease(lease)
        except GovernanceError as exc:
            raise RuntimeObservationError(f"campaign lease is invalid: {exc}") from exc
    else:
        required = (
            "campaign_id",
            "manifest_sha256",
            "campaign_fingerprint",
            "safety_envelope_sha256",
            "supervisor_pid",
            "supervisor_starttime",
            "issued_at",
        )
        missing = [name for name in required if not hasattr(lease, name)]
        if missing or expires_at_unix_ns is None:
            raise RuntimeObservationError(
                f"runtime campaign lease lacks attestation fields: {missing}"
            )
        try:
            result = build_campaign_lease(
                campaign_id=getattr(lease, "campaign_id"),
                manifest_sha256=getattr(lease, "manifest_sha256"),
                campaign_fingerprint=getattr(lease, "campaign_fingerprint"),
                safety_envelope_sha256=getattr(lease, "safety_envelope_sha256"),
                issued_at_unix_ns=_unix_ns(getattr(lease, "issued_at"), "lease issuance"),
                expires_at_unix_ns=_positive_int(
                    expires_at_unix_ns, "lease attestation expiry"
                ),
                supervisor_pid=getattr(lease, "supervisor_pid"),
                supervisor_starttime_ticks=getattr(lease, "supervisor_starttime"),
            )
        except GovernanceError as exc:
            raise RuntimeObservationError(f"campaign lease is invalid: {exc}") from exc
    if (
        result["manifest_sha256"] != release["manifest_sha256"]
        or result["safety_envelope_sha256"] != release["safety_envelope_sha256"]
    ):
        raise RuntimeObservationError("campaign lease release binding differs")
    return result


def _proc_observation(pid: int, role: str, expected_script: Path) -> dict[str, Any]:
    pid = _positive_int(pid, f"{role} PID")
    proc = Path(f"/proc/{pid}")
    try:
        stat = (proc / "stat").read_text(encoding="ascii")
        suffix = stat.rsplit(")", 1)[1].split()
        ppid = int(suffix[1])
        starttime = int(suffix[19])
        argv = [
            item.decode("utf-8", errors="surrogateescape")
            for item in (proc / "cmdline").read_bytes().split(b"\0")
            if item
        ]
        executable = str((proc / "exe").resolve(strict=True))
        resolved_arguments = {
            path
            for path in resolve_process_argv_paths(proc, argv)
            if path is not None
        }
    except (OSError, UnicodeError, IndexError, ValueError) as exc:
        raise RuntimeObservationError(f"cannot inspect {role} process {pid}: {exc}") from exc
    expected = str(expected_script.resolve(strict=True))
    if expected not in argv and expected not in resolved_arguments:
        raise RuntimeObservationError(f"{role} does not execute {expected_script.name}")
    return {
        "role": role,
        "pid": pid,
        "ppid": ppid,
        "starttime_ticks": _positive_int(starttime, f"{role} starttime"),
        "executable": executable,
        "argv": argv,
        "source": expected,
        "source_sha256": hashlib.sha256(expected_script.read_bytes()).hexdigest(),
    }


def _proc_entry_exists(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _discover_writer_processes(
    experiment_root: Path, *, proc_root: Path = Path("/proc")
) -> list[dict[str, Any]]:
    """Enumerate sanctioned bridge writer entrypoints from Linux process truth."""

    root = experiment_root.resolve(strict=True)
    expected = {
        str((root / relative).resolve(strict=True)): relative
        for relative in _WRITER_SOURCE_PATHS
        if (root / relative).is_file()
    }
    try:
        proc_entries = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdecimal()),
            key=lambda entry: int(entry.name),
        )
    except OSError as exc:
        raise RuntimeObservationError(f"cannot enumerate writer processes: {exc}") from exc

    observations: list[dict[str, Any]] = []
    for proc in proc_entries:
        pid = int(proc.name)
        try:
            argv = [
                item.decode("utf-8", errors="surrogateescape")
                for item in (proc / "cmdline").read_bytes().split(b"\0")
                if item
            ]
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeObservationError(
                f"cannot inspect process {pid} during writer discovery: {exc}"
            ) from exc
        if not argv:
            continue

        python_process = Path(argv[0]).name.startswith(("python", "pypy"))
        matched: set[str] = set()
        resolved_argv0: str | None = None
        script_arguments = [
            (index, argument)
            for index, argument in enumerate(argv)
            if argument.endswith(".py")
        ]
        try:
            resolved_scripts = resolve_process_argv_paths(
                proc,
                [argument for _, argument in script_arguments],
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeObservationError(
                f"cannot inspect process {pid} cwd during writer discovery: {exc}"
            ) from exc
        for (index, _argument), resolved in zip(
            script_arguments,
            resolved_scripts,
            strict=True,
        ):
            assert resolved is not None
            if index == 0:
                resolved_argv0 = resolved
            if resolved in expected:
                matched.add(expected[resolved])
        direct_writer = resolved_argv0 in expected
        if not matched or (not python_process and not direct_writer):
            continue

        try:
            stat = (proc / "stat").read_text(encoding="ascii")
            suffix = stat.rsplit(")", 1)[1].split()
            ppid = int(suffix[1])
            starttime = int(suffix[19])
            executable = str((proc / "exe").resolve(strict=True))
        except FileNotFoundError:
            continue
        except (OSError, IndexError, ValueError) as exc:
            raise RuntimeObservationError(
                f"cannot bind writer process {pid} identity: {exc}"
            ) from exc
        observations.append(
            {
                "pid": _positive_int(pid, "discovered writer PID"),
                "ppid": ppid,
                "starttime_ticks": _positive_int(
                    starttime, "discovered writer starttime"
                ),
                "executable": executable,
                "argv": argv,
                "matched_sources": sorted(matched),
            }
        )
    return observations


def _process_evidence(
    experiment_root: Path,
    process_pids: Mapping[str, int],
    *,
    cached_processes: Mapping[str, Mapping[str, Any]] | None = None,
    exited_processes: Mapping[str, int] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    role_paths = production_process_role_paths()
    if not isinstance(process_pids, Mapping) or set(process_pids) != set(role_paths):
        raise RuntimeObservationError("production process roles differ")
    exited = {} if exited_processes is None else dict(exited_processes)
    if not set(exited).issubset(role_paths):
        raise RuntimeObservationError("exited production process roles differ")
    if any(
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code != 0
        for exit_code in exited.values()
    ):
        raise RuntimeObservationError(
            "only a zero-exit production process may use cached provenance"
        )
    cache = {} if cached_processes is None else cached_processes
    processes: list[dict[str, Any]] = []
    for role in sorted(role_paths):
        pid = _positive_int(process_pids[role], f"{role} PID")
        expected_script = experiment_root / role_paths[role]
        try:
            process = _proc_observation(pid, role, expected_script)
        except RuntimeObservationError:
            if role not in exited or _proc_entry_exists(pid):
                raise
            cached = cache.get(role)
            expected_source = str(expected_script.resolve(strict=True))
            if (
                not isinstance(cached, Mapping)
                or cached.get("role") != role
                or cached.get("pid") != pid
                or cached.get("source") != expected_source
                or cached.get("source_sha256")
                != hashlib.sha256(expected_script.read_bytes()).hexdigest()
            ):
                raise RuntimeObservationError(
                    f"cached {role} process provenance is unavailable or differs"
                )
            process = copy.deepcopy(dict(cached))
            process["exit_code"] = exited[role]
            process["observed_terminal_exit"] = True
        processes.append(process)
    by_role = {process["role"]: process for process in processes}
    canonical_pid = by_role["canonical_launcher"]["pid"]
    supervisor_pid = by_role["launcher_supervisor"]["pid"]
    if (
        by_role["launcher_supervisor"]["ppid"] != canonical_pid
        or any(
            by_role[role]["ppid"] != supervisor_pid
            for role in ("bridge_wrapper", "campaign_runner")
        )
    ):
        raise RuntimeObservationError("production process parentage differs")
    return processes, production_process_tree_fingerprint(experiment_root)


@dataclass
class _CsvFollower:
    path: Path
    identity: tuple[int, int] | None = None
    offset: int = 0
    pending: bytes = b""
    fields: list[str] | None = None
    latest_row: dict[str, str] | None = None
    latest_row_observed_at_unix_ns: int | None = None

    def _reset(self, identity: tuple[int, int]) -> None:
        self.identity = identity
        self.offset = 0
        self.pending = b""
        self.fields = None
        self.latest_row = None
        self.latest_row_observed_at_unix_ns = None

    def poll(self, observed_at_unix_ns: int) -> tuple[dict[str, str] | None, int | None]:
        path = self.path
        if path.is_symlink() or not path.is_file():
            return self.latest_row, self.latest_row_observed_at_unix_ns
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self.identity != identity or stat.st_size < self.offset:
            self._reset(identity)
        with path.open("rb") as handle:
            handle.seek(self.offset)
            appended = handle.read()
            self.offset = handle.tell()
        if not appended:
            return self.latest_row, self.latest_row_observed_at_unix_ns
        chunks = (self.pending + appended).split(b"\n")
        self.pending = chunks.pop()
        observed_new_row = False
        for encoded_line in chunks:
            if encoded_line.endswith(b"\r"):
                encoded_line = encoded_line[:-1]
            if not encoded_line:
                continue
            try:
                values = next(csv.reader(io.StringIO(encoded_line.decode("utf-8"))))
            except (UnicodeError, csv.Error, StopIteration) as exc:
                raise RuntimeObservationError(f"bridge CSV contains an invalid row: {exc}") from exc
            if self.fields is None:
                if len(values) != len(set(values)) or any(not value for value in values):
                    raise RuntimeObservationError("bridge CSV header is invalid")
                self.fields = values
                continue
            if len(values) != len(self.fields):
                raise RuntimeObservationError("bridge CSV row cardinality differs")
            self.latest_row = dict(zip(self.fields, values, strict=True))
            observed_new_row = True
        if observed_new_row:
            self.latest_row_observed_at_unix_ns = observed_at_unix_ns
        return self.latest_row, self.latest_row_observed_at_unix_ns


def _integer_row(row: Mapping[str, Any] | None, name: str) -> int | None:
    if row is None:
        return None
    value = row.get(name)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _identity_from_row(row: Mapping[str, Any] | None) -> dict[str, int] | None:
    values = {
        "protocol_version": _integer_row(row, "ur_output_int_register_35"),
        "digest_hi": _integer_row(row, "ur_output_int_register_36"),
        "digest_lo": _integer_row(row, "ur_output_int_register_37"),
    }
    return _tp_identity(values, strict=False)


def _kunwei_observed_at(
    row: Mapping[str, Any] | None, rtde_observed_at_unix_ns: int | None
) -> int | None:
    if row is None or rtde_observed_at_unix_ns is None:
        return None
    try:
        age_s = float(row.get("sensor_age_s"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(age_s) or age_s < 0.0:
        return None
    value = rtde_observed_at_unix_ns - int(age_s * 1_000_000_000)
    return value if value > 0 else None


def _loaded_program(
    dashboard_result: Mapping[str, Any] | None,
    expected: str,
) -> str | None:
    if dashboard_result is None:
        return None
    raw = dashboard_result.get(
        "get loaded program", dashboard_result.get("loaded_program")
    )
    paths = loaded_program_paths(raw)
    if len(paths) != 1:
        return None
    observed = next(iter(paths))
    expected_path = PurePosixPath(expected).as_posix()
    return expected_path if observed == expected_path.lower() else observed


def _next_sequence(campaign_root: Path) -> int:
    pointer = campaign_root / "governance/current.json"
    if not pointer.exists():
        return 1
    if pointer.is_symlink() or not pointer.is_file():
        raise RuntimeObservationError("current observation pointer is unsafe")
    try:
        current, _ = load_current_observation(campaign_root)
    except GovernanceError as exc:
        raise RuntimeObservationError(f"current observation is invalid: {exc}") from exc
    return _positive_int(current["sequence"], "current observation sequence") + 1


@dataclass
class RuntimeObservationPublisher:
    experiment_root: Path
    campaign_root: Path
    run_id: str
    release: Mapping[str, Any]
    release_contract: Mapping[str, Any]
    bindings: Mapping[str, str]
    lease: dict[str, Any]
    sequence: int
    events: dict[str, Any] = field(
        default_factory=lambda: {name: None for name in _EVENT_FIELDS}
    )
    _last_event_order: int = field(default=-1, init=False)
    _followers: dict[Path, _CsvFollower] = field(default_factory=dict, init=False)
    _process_cache: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)

    @classmethod
    def start(
        cls,
        *,
        experiment_root: Path,
        campaign_root: Path,
        run_id: str,
        release: CurrentReleaseSnapshot | Mapping[str, Any],
        release_contract_evidence: Any,
        lease: Any,
        lease_expires_at_unix_ns: int | None = None,
    ) -> "RuntimeObservationPublisher":
        experiment = _root(experiment_root, "experiment root")
        campaign = _root(campaign_root, "campaign root", create=True)
        release_row = _validated_release(release)
        contract, environment_sha = _release_contract_binding(
            experiment_root=experiment,
            campaign_root=campaign,
            release=release_row,
            release_contract_evidence=release_contract_evidence,
        )
        lease_row = _attestation_lease(
            lease,
            release=release_row,
            expires_at_unix_ns=lease_expires_at_unix_ns,
        )
        bindings = {
            "manifest_sha256": release_row["manifest_sha256"],
            "source_fingerprint": release_row["source_fingerprint"],
            "launcher_sha256": release_row["launcher_sha256"],
            "environment_sha256": environment_sha,
            "process_tree_fingerprint": contract["process_tree_fingerprint"],
            "safety_envelope_sha256": release_row["safety_envelope_sha256"],
            "campaign_fingerprint": lease_row["campaign_fingerprint"],
        }
        return cls(
            experiment_root=experiment,
            campaign_root=campaign,
            run_id=_bounded_text(run_id, "run ID"),
            release=release_row,
            release_contract=contract,
            bindings=bindings,
            lease=lease_row,
            sequence=_next_sequence(campaign),
        )

    def update_lifecycle(
        self,
        event: str,
        *,
        observed_at_unix_ns: int,
        **details: Any,
    ) -> None:
        if event not in _EVENT_ORDER:
            raise RuntimeObservationError(f"unknown lifecycle event: {event}")
        order = _EVENT_ORDER[event]
        observed_at = _positive_int(observed_at_unix_ns, f"{event} timestamp")
        if order < self._last_event_order:
            raise RuntimeObservationError("lifecycle cannot move backwards")
        field_name = {
            "waiting_for_play": "waiting_for_play_at_unix_ns",
            "play_observed": "play_observed_at_unix_ns",
            "play_identity_recheck": "play_identity_recheck",
            "first_arm_ack": "first_arm_ack",
            "trial_completion": "trial_completion",
            "next_arm_ack": "next_arm_ack",
            "campaign_terminal": "campaign_terminal",
        }[event]
        if event in {"waiting_for_play", "play_observed"}:
            if details:
                raise RuntimeObservationError(f"{event} accepts no details")
            value: Any = observed_at
        elif event == "play_identity_recheck":
            required = {"readback_triplet_sha256", "loaded_program", "tp_runtime_identity"}
            if set(details) != required:
                raise RuntimeObservationError("Play identity recheck fields differ")
            value = {
                "observed_at_unix_ns": observed_at,
                "readback_triplet_sha256": _triplet(
                    details["readback_triplet_sha256"], "Play readback"
                ),
                "loaded_program": _bounded_text(
                    details["loaded_program"], "Play loaded program"
                ),
                "tp_runtime_identity": _tp_identity(
                    details["tp_runtime_identity"], strict=True
                ),
            }
        elif event in {"first_arm_ack", "next_arm_ack"}:
            if set(details) != {"sequence"}:
                raise RuntimeObservationError(f"{event} fields differ")
            value = {
                "sequence": _positive_int(details["sequence"], f"{event} sequence"),
                "observed_at_unix_ns": observed_at,
            }
        elif event == "trial_completion":
            if set(details) != {"trial_id", "evidence"}:
                raise RuntimeObservationError("trial completion fields differ")
            value = {
                "trial_id": _bounded_text(details["trial_id"], "trial ID"),
                "observed_at_unix_ns": observed_at,
                "evidence": _materialize_reference(
                    self.campaign_root, "trial_completion", details["evidence"]
                ),
            }
        else:
            if set(details) != {"reason", "runner_exit_code"}:
                raise RuntimeObservationError("campaign terminal fields differ")
            runner_exit_code = details["runner_exit_code"]
            if (
                details["reason"] != "campaign_complete"
                or isinstance(runner_exit_code, bool)
                or not isinstance(runner_exit_code, int)
                or runner_exit_code != 0
            ):
                raise RuntimeObservationError("campaign terminal outcome differs")
            value = {
                "observed_at_unix_ns": observed_at,
                "reason": "campaign_complete",
                "runner_exit_code": 0,
            }
        current = self.events[field_name]
        if current is not None and current != value:
            raise RuntimeObservationError(f"lifecycle event {event} is not idempotent")
        tentative = dict(self.events)
        tentative[field_name] = value
        timestamps = []
        for name in _EVENT_FIELDS:
            item = tentative[name]
            if item is None:
                continue
            timestamps.append(
                item if isinstance(item, int) else item["observed_at_unix_ns"]
            )
        if timestamps != sorted(timestamps):
            raise RuntimeObservationError("lifecycle timestamps are out of order")
        self.events = tentative
        self._last_event_order = max(self._last_event_order, order)

    def revoke_lease(self, *, observed_at_unix_ns: int, reason: str) -> None:
        timestamp = _positive_int(observed_at_unix_ns, "lease revocation")
        text = _bounded_text(reason, "lease revocation reason")
        if self.lease["revoked_at_unix_ns"] is not None:
            if (
                self.lease["revoked_at_unix_ns"] != timestamp
                or self.lease["revocation_reason"] != text
            ):
                raise RuntimeObservationError("lease revocation is not idempotent")
            return
        updated = dict(self.lease)
        updated["revoked_at_unix_ns"] = timestamp
        updated["revocation_reason"] = text
        try:
            self.lease = validate_campaign_lease(updated)
        except GovernanceError as exc:
            raise RuntimeObservationError(f"lease revocation is invalid: {exc}") from exc

    def publish(
        self,
        *,
        bridge_pid: int,
        bridge_starttime_ticks: int,
        bridge_csv: Path,
        process_pids: Mapping[str, int],
        writer_pids: Sequence[int],
        uploaded_triplet_sha256: Mapping[str, str] | None,
        readback_triplet_sha256: Mapping[str, str] | None,
        delivery_observation: Mapping[str, Any],
        dashboard_result: Mapping[str, Any] | None,
        controller_observed_at_unix_ns: int | None,
        rtde_output_fields: Sequence[str],
        rtde_output_types: Sequence[str],
        mailbox: Mapping[str, Any] | None,
        observed_at_unix_ns: int | None = None,
        external_blocker: Mapping[str, Any] | None = None,
        exited_processes: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        observed_at = _positive_int(
            time.time_ns() if observed_at_unix_ns is None else observed_at_unix_ns,
            "attestation observation",
        )
        processes, process_fingerprint = _process_evidence(
            self.experiment_root,
            process_pids,
            cached_processes=self._process_cache,
            exited_processes=exited_processes,
        )
        by_role = {process["role"]: process for process in processes}
        bridge_pid = _positive_int(bridge_pid, "bridge PID")
        bridge_starttime = _positive_int(
            bridge_starttime_ticks, "bridge starttime"
        )
        bridge_process = by_role["bridge_wrapper"]
        if (
            bridge_process["pid"] != bridge_pid
            or bridge_process["starttime_ticks"] != bridge_starttime
        ):
            raise RuntimeObservationError("bridge PID/starttime differs from process evidence")
        writer_values = sorted(
            {
                _positive_int(pid, "writer PID")
                for pid in writer_pids
            }
        )
        writer_processes = _discover_writer_processes(self.experiment_root)
        discovered_writer_pids = sorted(
            _positive_int(process.get("pid"), "discovered writer PID")
            for process in writer_processes
        )
        if discovered_writer_pids != sorted(set(discovered_writer_pids)):
            raise RuntimeObservationError("writer process discovery repeated a PID")
        if discovered_writer_pids != [bridge_pid]:
            raise RuntimeObservationError(
                "machine writer discovery did not prove exactly one production bridge"
            )
        discovered_bridge = writer_processes[0]
        if (
            discovered_bridge.get("starttime_ticks") != bridge_starttime
            or _CANONICAL_WRITER_SOURCE
            not in discovered_bridge.get("matched_sources", [])
        ):
            raise RuntimeObservationError(
                "machine writer discovery differs from production bridge identity"
            )
        if writer_values != discovered_writer_pids:
            raise RuntimeObservationError(
                "caller writer PID claim differs from machine discovery"
            )
        writer_values = discovered_writer_pids

        csv_path = Path(bridge_csv).expanduser().absolute()
        follower = self._followers.setdefault(csv_path, _CsvFollower(csv_path))
        latest_row, rtde_at = follower.poll(observed_at)
        kunwei_at = _kunwei_observed_at(latest_row, rtde_at)

        process_ref = _evidence_json(
            self.campaign_root,
            "process",
            {
                "schema": "step5d.autotune-v3/process-observation-evidence-v1",
                "process_tree_fingerprint": process_fingerprint,
                "processes": processes,
                "writer_pids": writer_values,
                "writer_processes": writer_processes,
                "bridge_csv": str(csv_path),
            },
        )

        if controller_observed_at_unix_ns is None:
            controller_at = None
        else:
            controller_at = _positive_int(
                controller_observed_at_unix_ns, "controller observation"
            )
        loaded_program = _loaded_program(
            dashboard_result, self.release["expected_loaded_program"]
        )
        uploaded = _triplet(uploaded_triplet_sha256, "uploaded")
        readback = _triplet(readback_triplet_sha256, "readback")
        delivery = dict(delivery_observation)
        if (
            delivery.get("release_manifest_sha256")
            != self.release["manifest_sha256"]
            or delivery.get("triplet_sha256") != uploaded
            or uploaded != readback
        ):
            raise RuntimeObservationError("delivery observation binding differs")
        try:
            fresh_get = fresh_get_provenance(delivery)
        except DeliveryObservationError as exc:
            raise RuntimeObservationError(
                f"delivery fresh-GET provenance is invalid: {exc}"
            ) from exc
        delivery_ref = _evidence_json(
            self.campaign_root, "delivery_observation", delivery
        )
        fields = list(rtde_output_fields)
        types = list(rtde_output_types)
        if any(not isinstance(value, str) or not value for value in fields + types):
            raise RuntimeObservationError("RTDE recipe fields/types must be strings")
        controller_evidence = {
            "schema": "step5d.autotune-v3/controller-observation-evidence-v1",
            "observed_at_unix_ns": controller_at,
            "fresh_get_observed_at_unix_ns": fresh_get["observed_at_unix_ns"],
            "delivery_transaction_id": fresh_get["transaction_id"],
            "dashboard_result": None if dashboard_result is None else dict(dashboard_result),
            "uploaded_triplet_sha256": uploaded,
            "readback_triplet_sha256": readback,
            "loaded_program": loaded_program,
            "rtde_output_fields": fields,
            "rtde_output_types": types,
            "latest_complete_bridge_csv_row": latest_row,
            "rtde_observed_at_unix_ns": rtde_at,
            "kunwei_observed_at_unix_ns": kunwei_at,
            "delivery_observation": delivery_ref,
        }
        controller_ref = _evidence_json(
            self.campaign_root, "controller", controller_evidence
        )

        if mailbox is None:
            mailbox_at = None
            pending_arm = None
            duplicate_arm = False
            mailbox_evidence: Mapping[str, Any] = {"available": False}
        else:
            if set(mailbox) != {
                "observed_at_unix_ns",
                "pending_arm_sequence",
                "duplicate_arm_detected",
            }:
                raise RuntimeObservationError("mailbox observation fields differ")
            mailbox_at = _optional_positive_int(
                mailbox["observed_at_unix_ns"], "mailbox observation"
            )
            pending_arm = _optional_positive_int(
                mailbox["pending_arm_sequence"], "pending ARM sequence"
            )
            duplicate_arm = mailbox["duplicate_arm_detected"]
            if not isinstance(duplicate_arm, bool):
                raise RuntimeObservationError("duplicate ARM observation must be boolean")
            mailbox_evidence = dict(mailbox)
        mailbox_ref = _evidence_json(
            self.campaign_root,
            "mailbox",
            {
                "schema": "step5d.autotune-v3/mailbox-observation-evidence-v1",
                **mailbox_evidence,
            },
        )

        external = None
        if external_blocker is not None:
            if set(external_blocker) != {
                "reason_code",
                "observed_at_unix_ns",
                "evidence",
            }:
                raise RuntimeObservationError("external blocker fields differ")
            external = {
                "reason_code": _bounded_text(
                    external_blocker["reason_code"], "external blocker reason"
                ),
                "observed_at_unix_ns": _positive_int(
                    external_blocker["observed_at_unix_ns"],
                    "external blocker observation",
                ),
                "evidence": _materialize_reference(
                    self.campaign_root,
                    "external_blocker",
                    external_blocker["evidence"],
                ),
            }

        payload = {
            "schema": OBSERVED_ATTESTATION_SCHEMA,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "campaign_id": self.lease["campaign_id"],
            "observed_at_unix_ns": observed_at,
            "bindings": dict(self.bindings),
            "release_contract": dict(self.release_contract),
            "process": {
                "bridge_pid": bridge_pid,
                "bridge_starttime_ticks": bridge_starttime,
                "heartbeat_at_unix_ns": rtde_at,
                "process_tree_fingerprint": process_fingerprint,
                "writer_pids": writer_values,
                "evidence": process_ref,
            },
            "controller": {
                "observed_at_unix_ns": controller_at,
                "fresh_get_observed_at_unix_ns": fresh_get[
                    "observed_at_unix_ns"
                ],
                "delivery_transaction_id": fresh_get["transaction_id"],
                "uploaded_triplet_sha256": uploaded,
                "readback_triplet_sha256": readback,
                "loaded_program": loaded_program,
                "tp_runtime_identity": _identity_from_row(latest_row),
                "rtde_output_fields": fields,
                "rtde_output_types": types,
                "rtde_observed_at_unix_ns": rtde_at,
                "kunwei_observed_at_unix_ns": kunwei_at,
                "delivery_observation": delivery_ref,
                "evidence": controller_ref,
            },
            "mailbox": {
                "observed_at_unix_ns": mailbox_at,
                "pending_arm_sequence": pending_arm,
                "duplicate_arm_detected": duplicate_arm,
                "evidence": mailbox_ref,
            },
            "lease": copy.deepcopy(self.lease),
            "events": copy.deepcopy(self.events),
            "external_blocker": external,
        }
        try:
            attestation = validate_observed_attestation(payload)
            pointer = publish_observed_attestation(self.campaign_root, attestation)
        except GovernanceError as exc:
            raise RuntimeObservationError(f"observed attestation is invalid: {exc}") from exc
        for process in processes:
            if process.get("observed_terminal_exit") is not True:
                self._process_cache[process["role"]] = copy.deepcopy(process)
        self.sequence += 1
        return {"attestation": attestation, "pointer": pointer}


__all__ = [
    "RuntimeObservationError",
    "RuntimeObservationPublisher",
]
