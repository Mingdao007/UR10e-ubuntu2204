"""Lineage-neutral managed-runtime manifest, resolvers, and launcher.

The manifest is the only lineage-owned input.  Control always resolves through
the canonical V3 ``control`` profile with the explicit full-audit gate;
optimizer always resolves through the existing lineage-neutral V3 CUDA
profile.  The two lanes deliberately have different typed bindings and cannot
be selected together or substituted with the host interpreter.

This module contains no numerical, GPU, ROS, Pinocchio, Torch, BoTorch, or
GPyTorch imports.  Heavy imports remain inside the attested managed children.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence

from step5d_autotune_v3 import runtime_environment
from step5d_autotune_v3.runtime_installation import (
    RuntimeVerificationMode,
    load_runtime_contract,
    load_runtime_pointer_for_mode,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = "step5d.managed-runtime/manifest-v1"
CONTROL_PROFILE = "control"
CONTROL_VERIFICATION_MODE = RuntimeVerificationMode.FULL_AUDIT.value
OPTIMIZER_PROFILE = "optimizer"
OPTIMIZER_RESOLVER = "step5d_optimizer_runtime.resolve_optimizer_runtime"
ROUTES = ("prepare", "host")
REQUIRED_PRIMITIVES = frozenset(
    {
        "tools/step5d_managed_runtime.py",
        "tools/step5d_optimizer_runtime.py",
        "tools/step5d_autotune_v3/runtime_installation.py",
        "tools/step5d_autotune_v3/runtime_environment.py",
    }
)
_HEX = frozenset("0123456789abcdef")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_IDENTITY = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_RELEASE_IDENTITY = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


class ManagedRuntimeError(RuntimeError):
    """A typed manifest, profile, interpreter, or child-launch failure."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not a SHA-256")
    return value


def _canonical_json(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is missing or unsafe")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"forbidden JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} must be an object")
    return value


def _safe_relative(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not a safe relative path")
    path = Path(value)
    if (
        path.is_absolute()
        or str(path) != value
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} escapes the experiment root")
    return value


def _regular_under(root: Path, relative: str, role: str) -> Path:
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} contains a symlink")
    if path.is_symlink() or not path.is_file():
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not a regular file")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} escapes the experiment root") from exc
    return path


def _module(value: Any, role: str) -> str:
    if not isinstance(value, str) or len(value) > 160 or _MODULE.fullmatch(value) is None:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not an allowlisted module name")
    return value


def _unique_strings(value: Any, role: str, *, module: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} must be a bounded list")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_module(item, f"{role}[{index}]") if module else _identity_string(item, f"{role}[{index}]"))
    if len(set(result)) != len(result):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} contains duplicates")
    return tuple(result)


def _identity_string(value: Any, role: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not a bounded identity")
    return value


def _release_identity(value: Any, role: str) -> str:
    if not isinstance(value, str) or _RELEASE_IDENTITY.fullmatch(value) is None:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not a bounded release identity")
    return value


def _absolute_path(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is invalid")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"{role} is not canonical absolute")
    return value


@dataclass(frozen=True)
class ManagedRuntimeManifest:
    """Validated, immutable manifest data supplied by one lineage."""

    path: Path
    sha256: str
    schema: str
    lineage_id: str
    launcher_path: str
    release_contract_path: str
    release_contract_schema: str
    release_program: str
    release_revision: int
    route_modules: tuple[tuple[str, str], ...]
    control_profile: str
    control_verification_mode: str
    control_required_imports: tuple[str, ...]
    control_required_ros_packages: tuple[str, ...]
    control_required_ros_pythonpath: tuple[str, ...]
    optimizer_profile: str
    optimizer_resolver: str
    optimizer_worker_module: str
    no_system_install: bool
    source_closure: tuple[str, ...]

    @property
    def routes(self) -> Mapping[str, str]:
        return dict(self.route_modules)

    def module_for_route(self, route: str) -> str:
        if not isinstance(route, str) or route not in dict(self.route_modules):
            raise ManagedRuntimeError(
                "MANAGED_RUNTIME_ROUTE_INVALID",
                "unknown route; expected exactly: " + ", ".join(ROUTES),
            )
        return dict(self.route_modules)[route]


def load_runtime_manifest(
    path: Path,
    *,
    root: Path = ROOT,
    expected_sha256: str | None = None,
) -> ManagedRuntimeManifest:
    """Load one canonical manifest and bind its release/source closure."""

    root = Path(root).resolve(strict=True)
    selected = Path(path)
    if not selected.is_absolute():
        selected = root / selected
    if selected.is_symlink() or not selected.is_file():
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime manifest is missing or unsafe")
    try:
        selected = selected.resolve(strict=True)
        selected.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime manifest path escapes root") from exc
    raw_bytes = selected.read_bytes()
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if expected_sha256 is not None and actual_sha256 != _digest(expected_sha256, "runtime manifest digest"):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime manifest digest differs")
    document = _canonical_json(selected, "runtime manifest")
    required = {"schema", "lineage_id", "launcher_path", "release_contract", "routes", "control", "optimizer", "no_system_install", "source_closure"}
    if set(document) != required or document.get("schema") != MANIFEST_SCHEMA:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime manifest fields/schema differ")
    lineage_id = _identity_string(document.get("lineage_id"), "lineage_id")
    launcher_path = _safe_relative(document.get("launcher_path"), "runtime launcher path")
    _regular_under(root, launcher_path, "runtime launcher")

    release = document.get("release_contract")
    release_fields = {"path", "schema", "program", "revision"}
    if not isinstance(release, Mapping) or set(release) != release_fields:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "release contract identity fields differ")
    release_path = _safe_relative(release.get("path"), "release contract path")
    release_file = _regular_under(root, release_path, "release contract")
    release_document = _canonical_json(release_file, "release contract")
    release_schema = _release_identity(release.get("schema"), "release contract schema")
    release_program = _release_identity(release.get("program"), "release contract program")
    revision = release.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 100000:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "release contract revision is invalid")
    if (
        release_document.get("schema") != release_schema
        or release_document.get("program") != release_program
        or release_document.get("revision") != revision
    ):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "release contract identity differs")

    routes = document.get("routes")
    if not isinstance(routes, Mapping) or set(routes) != set(ROUTES):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime routes differ")
    route_modules: list[tuple[str, str]] = []
    for route in ROUTES:
        row = routes.get(route)
        if not isinstance(row, Mapping) or set(row) != {"module"}:
            raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", f"runtime route {route} differs")
        route_modules.append((route, _module(row.get("module"), f"route {route} module")))
    if len({module for _, module in route_modules}) != len(route_modules):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime route modules must be unique")

    control = document.get("control")
    control_fields = {"profile", "verification_mode", "required_imports", "required_ros_packages", "required_ros_pythonpath"}
    if not isinstance(control, Mapping) or set(control) != control_fields:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "control runtime fields differ")
    if control.get("profile") != CONTROL_PROFILE or control.get("verification_mode") != CONTROL_VERIFICATION_MODE:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "control must use canonical V3 full_audit profile")
    control_imports = _unique_strings(control.get("required_imports"), "control required imports", module=True)
    control_ros_packages = _unique_strings(control.get("required_ros_packages"), "control required ROS packages")
    ros_paths_raw = control.get("required_ros_pythonpath")
    if not isinstance(ros_paths_raw, list) or not ros_paths_raw or len(ros_paths_raw) > 16:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "control ROS PYTHONPATH is not bounded")
    control_ros_paths = tuple(_absolute_path(item, "control ROS PYTHONPATH") for item in ros_paths_raw)
    if len(set(control_ros_paths)) != len(control_ros_paths):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "control ROS PYTHONPATH contains duplicates")

    optimizer = document.get("optimizer")
    optimizer_fields = {"profile", "resolver", "worker_module"}
    if not isinstance(optimizer, Mapping) or set(optimizer) != optimizer_fields:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "optimizer runtime fields differ")
    if optimizer.get("profile") != OPTIMIZER_PROFILE or optimizer.get("resolver") != OPTIMIZER_RESOLVER:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "optimizer must use canonical V3 CUDA resolver/profile")
    optimizer_worker = _module(optimizer.get("worker_module"), "optimizer worker module")
    if not isinstance(document.get("no_system_install"), bool) or document["no_system_install"] is not True:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "system installation fallback is forbidden")

    source_closure_raw = document.get("source_closure")
    if not isinstance(source_closure_raw, list) or not source_closure_raw or len(source_closure_raw) > 256:
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime source closure is not bounded")
    source_closure = tuple(_safe_relative(item, "runtime source closure") for item in source_closure_raw)
    if len(set(source_closure)) != len(source_closure):
        raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "runtime source closure contains duplicates")
    missing_primitives = REQUIRED_PRIMITIVES.difference(source_closure)
    if missing_primitives:
        raise ManagedRuntimeError(
            "MANAGED_RUNTIME_INVALID",
            "runtime source closure omits: " + ",".join(sorted(missing_primitives)),
        )
    for source in source_closure:
        _regular_under(root, source, "runtime source closure file")

    return ManagedRuntimeManifest(
        path=selected,
        sha256=actual_sha256,
        schema=MANIFEST_SCHEMA,
        lineage_id=lineage_id,
        launcher_path=launcher_path,
        release_contract_path=release_path,
        release_contract_schema=release_schema,
        release_program=release_program,
        release_revision=revision,
        route_modules=tuple(route_modules),
        control_profile=CONTROL_PROFILE,
        control_verification_mode=CONTROL_VERIFICATION_MODE,
        control_required_imports=control_imports,
        control_required_ros_packages=control_ros_packages,
        control_required_ros_pythonpath=control_ros_paths,
        optimizer_profile=OPTIMIZER_PROFILE,
        optimizer_resolver=OPTIMIZER_RESOLVER,
        optimizer_worker_module=optimizer_worker,
        no_system_install=True,
        source_closure=source_closure,
    )


def _regular_executable(value: Any, role: str, *, expected_path: Path) -> Path:
    path = Path(str(value))
    if not path.is_absolute() or path != expected_path:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"{role} is unavailable")
    if path.is_symlink():
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"{role} symlink target is unavailable") from exc
        if resolved.is_symlink() or not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"{role} symlink target is not executable")
        return path
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"{role} is unavailable")
    return path


def _pointer_row(pointer: Mapping[str, Any], manifest: ManagedRuntimeManifest) -> tuple[dict[str, Any], Path, Path]:
    if pointer.get("schema") != "step5d.autotune-v3/runtime-pointer-v2":
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "managed V3 pointer schema differs")
    for key in ("bundle_id", "contract_sha256", "lock_sha256", "attestation_sha256"):
        _digest(pointer.get(key), f"V3 pointer {key}")
    profiles = pointer.get("profiles")
    if not isinstance(profiles, Mapping) or manifest.control_profile not in profiles:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "managed V3 control profile is absent")
    raw = profiles[manifest.control_profile]
    if not isinstance(raw, Mapping):
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "managed V3 control row is invalid")
    row = dict(raw)
    root = Path(str(row.get("root", "")))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "managed V3 control root is unsafe")
    python = _regular_executable(row.get("python_executable"), "managed V3 control interpreter", expected_path=root / "bin/python")
    _digest(row.get("environment_id"), "control environment ID")
    _digest(row.get("profile_tree_sha256"), "control profile tree")
    return row, root, python


def _probe_control_interpreter(
    *,
    python_executable: Path,
    python_prefix: Path,
    environment: Mapping[str, str],
    required_imports: Sequence[str],
    required_distributions: Mapping[str, str],
) -> Mapping[str, Any]:
    """Import and attest control dependencies only in the selected child."""

    probe = r'''
import importlib
import importlib.metadata
import json
import os
import pathlib
import sys
import sysconfig

expected = json.loads(sys.argv[1])
observed_executable = os.path.abspath(sys.executable)
if observed_executable not in {expected["python_executable"], expected["python_resolved_executable"]}:
    raise SystemExit("managed control interpreter differs")
if os.path.realpath(observed_executable) != expected["python_resolved_executable"]:
    raise SystemExit("managed control interpreter target differs")
if str(pathlib.Path(sys.prefix).resolve()) != expected["python_prefix"]:
    raise SystemExit("managed control interpreter prefix differs")
observed_paths = {str(pathlib.Path(item).resolve()) for item in sys.path if item}
missing_paths = [item for item in expected["pythonpath"] if item not in observed_paths]
if missing_paths:
    raise SystemExit("ROS/runtime PYTHONPATH is incomplete: " + ",".join(missing_paths))
imports = {}
for name in expected["required_imports"]:
    module = importlib.import_module(name)
    imports[name] = str(pathlib.Path(module.__file__).resolve()) if getattr(module, "__file__", None) else None
versions = {name: importlib.metadata.version(name) for name in expected["required_distributions"]}
print(json.dumps({
    "python_executable": os.path.abspath(sys.executable),
    "python_resolved_executable": str(pathlib.Path(sys.executable).resolve()),
    "python_prefix": str(pathlib.Path(sys.prefix).resolve()),
    "python_version": ".".join(map(str, sys.version_info[:3])),
    "purelib": str(pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()),
    "required_imports": imports,
    "required_distributions": versions,
}, sort_keys=True))
'''
    payload = {
        "python_executable": str(python_executable),
        "python_resolved_executable": str(python_executable.resolve()),
        "python_prefix": str(python_prefix.resolve()),
        "pythonpath": [str(Path(item).resolve()) for item in environment.get("PYTHONPATH", "").split(os.pathsep) if item],
        "required_imports": list(required_imports),
        "required_distributions": dict(required_distributions),
    }
    try:
        completed = subprocess.run(
            [str(python_executable), "-B", "-c", probe, json.dumps(payload, sort_keys=True)],
            cwd=ROOT,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"control dependency probe failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"control interpreter/dependency attestation failed: {detail}")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control dependency attestation is not JSON") from exc
    if not isinstance(value, Mapping):
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control dependency attestation is not an object")
    return dict(value)


@dataclass(frozen=True)
class ManagedControlBinding:
    manifest_path: Path
    manifest_sha256: str
    profile: str
    bundle_id: str
    contract_sha256: str
    lock_sha256: str
    attestation_sha256: str
    environment_id: str
    python_executable: Path
    python_prefix: Path
    pythonpath: tuple[str, ...]
    required_imports: tuple[str, ...]
    required_distributions: Mapping[str, str]
    dependency_attestation: Mapping[str, Any]
    environment: Mapping[str, str]
    route_modules: tuple[tuple[str, str], ...]

    @property
    def schema(self) -> str:
        """Read-only compatibility view of the bound manifest schema."""

        return MANIFEST_SCHEMA

    def command(self, module: str, args: Sequence[str] = ()) -> tuple[str, ...]:
        allowed = {value for _, value in self.route_modules}
        if module not in allowed or any(not isinstance(value, str) for value in args):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control launcher module/arguments are invalid")
        return (str(self.python_executable), "-B", "-m", module, *tuple(args))


def resolve_managed_control_runtime(
    manifest: ManagedRuntimeManifest,
    *,
    source_environment: Mapping[str, str] | None = None,
    pointer_loader: Callable[..., Mapping[str, Any]] | None = None,
    environment_builder: Callable[..., Mapping[str, str]] | None = None,
    dependency_probe: Callable[..., Mapping[str, Any]] | None = None,
    runtime_contract_loader: Callable[[], Mapping[str, Any]] | None = None,
) -> ManagedControlBinding:
    if not isinstance(manifest, ManagedRuntimeManifest):
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control manifest binding is not typed")
    source = dict(os.environ if source_environment is None else source_environment)
    try:
        mode = RuntimeVerificationMode(manifest.control_verification_mode)
        loader = pointer_loader or load_runtime_pointer_for_mode
        pointer = dict(loader(mode, environ=source))
        row, root, python = _pointer_row(pointer, manifest)
        contract = dict(runtime_contract_loader() if runtime_contract_loader is not None else load_runtime_contract())
        profile = contract["profiles"][manifest.control_profile]
        required_imports = tuple(dict.fromkeys(tuple(str(item) for item in profile["required_imports"]) + manifest.control_required_imports))
        required_distributions = {str(name): str(version) for name, version in profile["required_distributions"].items()}
        packages = contract["ros"]["packages"]
        if any(package not in packages for package in manifest.control_required_ros_packages):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "required ROS package is absent from canonical V3 contract")
        builder = environment_builder or runtime_environment.production_runtime_environment
        environment = dict(builder(source, profile=manifest.control_profile, runtime_pointer=pointer))
        if environment.get("STEP5D_V3_RUNTIME_PROFILE") != CONTROL_PROFILE:
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "resolved runtime profile is not control")
        if environment.get("STEP5D_V3_CONTROL_ENVIRONMENT_ID") != row["environment_id"]:
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control environment ID is not pointer-bound")
        if environment.get("STEP5D_V3_CONTROL_PYTHON") != str(python):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control interpreter is not environment-bound")
        if any(key in environment for key in ("STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID", "STEP5D_V3_OPTIMIZER_PYTHON")):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "optimizer runtime leaked into control environment")
        observed_pythonpath = tuple(item for item in environment.get("PYTHONPATH", "").split(os.pathsep) if item)
        observed_resolved = {str(Path(item).resolve()) for item in observed_pythonpath}
        if any(str(Path(path).resolve()) not in observed_resolved for path in manifest.control_required_ros_pythonpath):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "ROS PYTHONPATH is incomplete")
        probe = dependency_probe or _probe_control_interpreter
        attestation = dict(probe(
            python_executable=python,
            python_prefix=root,
            environment=environment,
            required_imports=required_imports,
            required_distributions=required_distributions,
        ))
        expected_python = str(python)
        expected_resolved = str(python.resolve())
        observed_python = attestation.get("python_executable")
        if observed_python not in {expected_python, expected_resolved} or str(Path(str(observed_python)).resolve()) != expected_resolved or attestation.get("python_resolved_executable") != expected_resolved:
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "attested interpreter differs")
        if attestation.get("python_prefix") != str(root.resolve()):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "attested interpreter prefix differs")
        if set(attestation.get("required_imports", {})) != set(required_imports):
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "attested control imports differ")
        if dict(attestation.get("required_distributions", {})) != required_distributions:
            raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "attested control dependencies differ")
        return ManagedControlBinding(
            manifest_path=manifest.path,
            manifest_sha256=manifest.sha256,
            profile=CONTROL_PROFILE,
            bundle_id=_digest(pointer["bundle_id"], "runtime bundle ID"),
            contract_sha256=_digest(pointer["contract_sha256"], "runtime contract digest"),
            lock_sha256=_digest(pointer["lock_sha256"], "runtime lock digest"),
            attestation_sha256=_digest(pointer["attestation_sha256"], "runtime attestation digest"),
            environment_id=str(row["environment_id"]),
            python_executable=python,
            python_prefix=root,
            pythonpath=observed_pythonpath,
            required_imports=required_imports,
            required_distributions=required_distributions,
            dependency_attestation=attestation,
            environment=environment,
            route_modules=manifest.route_modules,
        )
    except ManagedRuntimeError:
        raise
    except Exception as exc:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"control runtime resolution failed closed: {exc}") from exc


@dataclass(frozen=True)
class ManagedOptimizerBinding:
    manifest_path: Path
    manifest_sha256: str
    profile: str
    resolver: str
    worker_module: str
    runtime: Any

    @property
    def python_executable(self) -> Path:
        return Path(self.runtime.python_executable)

    @property
    def environment_id(self) -> str:
        return str(self.runtime.environment_id)


def resolve_managed_optimizer_runtime(
    manifest: ManagedRuntimeManifest,
    *,
    resolver: Callable[..., Any] | None = None,
) -> ManagedOptimizerBinding:
    if not isinstance(manifest, ManagedRuntimeManifest):
        raise ManagedRuntimeError("OPTIMIZER_RUNTIME_INVALID", "optimizer manifest binding is not typed")
    try:
        from step5d_optimizer_runtime import (
            OptimizerProfileDeclaration,
            resolve_optimizer_runtime as canonical_resolver,
        )

        declaration = OptimizerProfileDeclaration(profile=manifest.optimizer_profile)
        selected = resolver or canonical_resolver
        runtime = selected(declaration)
    except Exception as exc:
        reason = getattr(exc, "reason_code", "OPTIMIZER_RUNTIME_INVALID")
        raise ManagedRuntimeError(str(reason), f"canonical V3 optimizer runtime resolution failed: {exc}") from exc
    declaration_observed = getattr(runtime, "declaration", None)
    if declaration_observed is None or getattr(declaration_observed, "profile", None) != OPTIMIZER_PROFILE:
        raise ManagedRuntimeError("OPTIMIZER_RUNTIME_INVALID", "optimizer child profile is not canonical V3 optimizer")
    return ManagedOptimizerBinding(
        manifest_path=manifest.path,
        manifest_sha256=manifest.sha256,
        profile=manifest.optimizer_profile,
        resolver=manifest.optimizer_resolver,
        worker_module=manifest.optimizer_worker_module,
        runtime=runtime,
    )


def resolve_managed_runtime(manifest: ManagedRuntimeManifest, lane: str, **kwargs: Any) -> Any:
    if lane == CONTROL_PROFILE:
        return resolve_managed_control_runtime(manifest, **kwargs)
    if lane == OPTIMIZER_PROFILE:
        return resolve_managed_optimizer_runtime(manifest, **kwargs)
    raise ManagedRuntimeError("MANAGED_RUNTIME_INVALID", "control and optimizer profiles cannot be mixed")


def launch_managed_module(
    module: str,
    args: Sequence[str] = (),
    *,
    binding: ManagedControlBinding,
    process_runner: Callable[..., Any] | None = None,
    cwd: Path = ROOT,
) -> int:
    """Run one manifest-allowlisted control module after admission."""

    if not isinstance(binding, ManagedControlBinding):
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control binding is not typed")
    if not isinstance(module, str) or not module:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "control module is missing")
    arguments = tuple(args)
    if any(not isinstance(value, str) for value in arguments):
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "launcher arguments must be strings")
    runner = process_runner or subprocess.run
    try:
        completed = runner(
            list(binding.command(module, arguments)),
            cwd=Path(cwd),
            env=dict(binding.environment),
            stdin=None,
            check=False,
        )
    except OSError as exc:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", f"managed child could not start: {exc}") from exc
    return int(completed.returncode)


def launch_manifest_route(
    manifest_path: Path,
    argv: Sequence[str] = (),
    *,
    root: Path = ROOT,
    binding: ManagedControlBinding | None = None,
    process_runner: Callable[..., Any] | None = None,
    source_environment: Mapping[str, str] | None = None,
    pointer_loader: Callable[..., Mapping[str, Any]] | None = None,
    environment_builder: Callable[..., Mapping[str, str]] | None = None,
    dependency_probe: Callable[..., Mapping[str, Any]] | None = None,
    runtime_contract_loader: Callable[[], Mapping[str, Any]] | None = None,
) -> int:
    arguments = tuple(argv)
    if any(not isinstance(value, str) for value in arguments):
        raise ManagedRuntimeError("MANAGED_RUNTIME_ROUTE_INVALID", "launcher arguments must be strings")
    if not arguments:
        raise ManagedRuntimeError("MANAGED_RUNTIME_ROUTE_INVALID", "missing route")
    manifest = load_runtime_manifest(manifest_path, root=root)
    module = manifest.module_for_route(arguments[0])
    selected = binding or resolve_managed_control_runtime(
        manifest,
        source_environment=source_environment,
        pointer_loader=pointer_loader,
        environment_builder=environment_builder,
        dependency_probe=dependency_probe,
        runtime_contract_loader=runtime_contract_loader,
    )
    if selected.manifest_sha256 != manifest.sha256 or selected.profile != CONTROL_PROFILE:
        raise ManagedRuntimeError("CONTROL_RUNTIME_INVALID", "launcher binding does not match manifest")
    return launch_managed_module(
        module,
        arguments[1:],
        binding=selected,
        process_runner=process_runner,
        cwd=Path(root),
    )


launch_managed_route = launch_manifest_route


__all__ = [
    "CONTROL_PROFILE",
    "CONTROL_VERIFICATION_MODE",
    "MANIFEST_SCHEMA",
    "ManagedControlBinding",
    "ManagedOptimizerBinding",
    "ManagedRuntimeError",
    "ManagedRuntimeManifest",
    "OPTIMIZER_PROFILE",
    "OPTIMIZER_RESOLVER",
    "REQUIRED_PRIMITIVES",
    "ROUTES",
    "launch_managed_module",
    "launch_managed_route",
    "launch_manifest_route",
    "load_runtime_manifest",
    "resolve_managed_control_runtime",
    "resolve_managed_optimizer_runtime",
    "resolve_managed_runtime",
]
