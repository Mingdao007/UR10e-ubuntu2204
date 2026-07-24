"""Resolve and classify every static input of the production bridge path.

The release fingerprint API remains the small ``(experiment, repository)``
tuple returned by :func:`production_source_closure`.  The richer report is the
machine-readable explanation for that tuple: repository files, Python import
providers, contract-governed executables, and the SHA-bound controller helper.
No observed host path is included, so the result is clone-location independent.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Iterable, Mapping


SOURCE_CLOSURE_SCHEMA = "step5d.autotune-v3/active-source-closure-v1"
SOURCE_CLOSURE_REASON_CODE = "ACTIVE_SOURCE_CLOSURE_UNRESOLVED"

PRODUCTION_EXPERIMENT_SEEDS = frozenset(
    {
        "scripts/step5d-autotune-v3.sh",
        "tools/build_step5d_autotune_tp_v3.py",
        "tools/preflight_step5d_manual_bridge.py",
        "tools/preflight_step5d_autotune_v3.py",
        "tools/resolve_step5d_autotune_v3_runtime.py",
        "tools/resolve_step5d_bridge_route.py",
        "tools/run_step5d_autotune_campaign.py",
        "tools/run_step5d_autotune_v3_bridge.py",
        "tools/run_step5d_autotune_v3_live.py",
        "tools/run_step5d_autotune_v3_qualification.py",
        "tools/run_step5d_autotune_v3_tp_transaction.py",
        "tools/run_step5d_manual_bridge.py",
        "tools/run_step5d_manual_bridge_live.py",
        "tools/run_step5d_manual_live_campaign.py",
        "tools/step5d_bridge_authority.py",
        "tools/step5d_bridge_status.py",
        "tools/step5d_manual_qualification.py",
        "tools/step5d_manual_status.py",
        "tools/step5d_autotune_v3/cli.py",
    }
)

# These are immutable repository inputs read by the active code.  Runtime state,
# release selectors, output directories, and archival CSVs deliberately do not
# belong here.
PRODUCTION_EXPERIMENT_DATA = frozenset(
    {
        "config/schemas/step5d_autotune_campaign_v1.schema.json",
        "config/step5/step5d_v3_runtime_contract.json",
        "config/step5/step5d_autotune_v3_control_contract.json",
        "config/step5_safe_frame.json",
        "config/step5d/manual/launch_profile.json",
        "config/step5d/manual/stage_table.json",
        "config/step5d/artifact_locators/step5d_v35_retained_inputs.json",
        "config/step5d/manifests/step5d_strict_rnn_ablation_v35/"
        "controller_readback_receipt.json",
        "config/step5d_liveprep_solver_gate.json",
        "config/step5d/manifests/step5d_strict_rnn_autotune_v3/"
        "runtime_calibration.json",
        "config/step5d_autotune_campaign_v1.json",
        "programs/step5/step5d/step5d_strict_rnn_ablation_v35.script",
        "programs/step5/step5d/step5d_strict_rnn_ablation_v35.txt",
        "programs/step5/step5d/step5d_strict_rnn_ablation_v35.urp",
        "pyproject.toml",
        "uv.lock",
    }
)
PRODUCTION_REPOSITORY_ASSETS = frozenset(
    {
        "src/ur10e_bringup/config/ur10e_calibration.yaml",
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/schemas/"
        "experiment_spec.schema.json",
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/schemas/"
        "parallel_run_manifest.schema.json",
        "src/ur10e_experiment_runtime/ur10e_experiment_runtime/schemas/"
        "run_manifest.schema.json",
    }
)

# Import name and distribution name are intentionally separate.  A package
# appearing transitively in uv.lock does not authorize a new production import;
# adding one requires an explicit reviewable row here.
UV_IMPORT_DISTRIBUTIONS: Mapping[str, str] = {
    "botorch": "botorch",
    "cupy": "cupy-cuda12x",
    "cupy_backends": "cupy-cuda12x",
    "gpytorch": "gpytorch",
    "jsonschema": "jsonschema",
    "matplotlib": "matplotlib",
    "mujoco": "mujoco",
    "numpy": "numpy",
    "pandas": "pandas",
    "pexpect": "pexpect",
    "torch": "torch",
    "yaml": "pyyaml",
}
HOST_IMPORT_PACKAGES: Mapping[str, str] = {
    "ament_index_python": "ros-humble-ament-index-python",
    "pinocchio": "ros-humble-pinocchio",
    "xacro": "ros-humble-xacro",
}

# Executable locations are observed later by runtime attestation.  This closure
# records only the immutable contract keys that govern their identities.
HOST_EXECUTABLE_INPUTS = (
    ("bash_and_ubuntu_base_tools", "host.os_id+host.os_version_id"),
    ("bootstrap_python", "python.bootstrap_executable+python.bootstrap_sha256"),
    ("uv", "uv.executable_name+uv.sha256"),
    ("dpkg-query", "ros.packages"),
    ("nvidia-smi", "gpu.uuid+gpu.driver_version"),
    ("systemd_user_tools", "host.os_id+host.os_version_id"),
)

_RUNTIME_CONTRACT = "config/step5/step5d_v3_runtime_contract.json"
_PROFILE_PROBE_SOURCE = "tools/step5d_autotune_v3/runtime_installation.py"
_PROFILE_PROBE_SYMBOL = "_PROFILE_PROBE"
_LOCK_PACKAGE_RE = re.compile(
    r'^\[\[package\]\]\s*\nname\s*=\s*"([a-zA-Z0-9_.-]+)"', re.MULTILINE
)


class SourceClosureError(RuntimeError):
    """The active production path contains an unclassified static input."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{SOURCE_CLOSURE_REASON_CODE}: {detail}")
        self.reason_code = SOURCE_CLOSURE_REASON_CODE
        self.detail = detail


def _strict_object(path: Path, role: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourceClosureError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"forbidden JSON constant {value!r}")
            ),
        )
    except SourceClosureError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SourceClosureError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SourceClosureError(f"{role} must be a JSON object")
    return payload


def _safe_file(root: Path, relative: str, role: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise SourceClosureError(f"{role} path is not canonical: {relative!r}")
    unresolved = root / Path(*pure.parts)
    if unresolved.is_symlink() or not unresolved.is_file():
        raise SourceClosureError(f"{role} is missing or unsafe: {relative}")
    try:
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SourceClosureError(f"{role} escapes its repository root: {relative}") from exc
    return resolved


def _repository_relative(path: Path, repository_root: Path) -> str:
    try:
        relative = path.relative_to(repository_root)
    except ValueError as exc:
        raise SourceClosureError(f"production source escapes repository: {path}") from exc
    return PurePosixPath(relative).as_posix()


def _module_name(path: Path, experiment_root: Path, repository_root: Path) -> str | None:
    roots = (
        experiment_root / "tools",
        repository_root / "experiments/sensor-integration/kunwei-kwr75b/tools",
        repository_root / "src/ur10e_experiment_runtime",
    )
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return None


def _module_files(module: str, search_roots: Iterable[Path]) -> set[Path]:
    if not module:
        return set()
    parts = module.split(".")
    found: set[Path] = set()
    for root in search_roots:
        leaf = root.joinpath(*parts)
        leaf_files = {
            candidate.resolve(strict=True)
            for candidate in (leaf.with_suffix(".py"), leaf / "__init__.py")
            if candidate.is_file() and not candidate.is_symlink()
        }
        if not leaf_files:
            continue
        found.update(leaf_files)
        for index in range(1, len(parts)):
            package_init = root.joinpath(*parts[:index], "__init__.py")
            if package_init.is_file() and not package_init.is_symlink():
                found.add(package_init.resolve(strict=True))
    return found


def _resolved_import_base(
    node: ast.ImportFrom, current_package: str, *, source: str
) -> str:
    if not node.level:
        return node.module or ""
    package_parts = current_package.split(".") if current_package else []
    keep = len(package_parts) - (node.level - 1)
    if keep < 0:
        raise SourceClosureError(
            f"relative import escapes package in {source}:{node.lineno}"
        )
    return ".".join(
        part
        for part in [*package_parts[:keep], *(node.module or "").split(".")]
        if part
    )


def _dynamic_import_call(node: ast.Call) -> bool:
    function = node.func
    return (
        isinstance(function, ast.Name)
        and function.id in {"__import__", "import_module"}
    ) or (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
    )


class _Resolver:
    def __init__(
        self,
        *,
        experiment_root: Path,
        repository_root: Path,
        contract: Mapping[str, Any],
        lock_packages: set[str],
    ) -> None:
        self.experiment_root = experiment_root
        self.repository_root = repository_root
        self.contract = contract
        self.lock_packages = lock_packages
        self.search_roots = (
            experiment_root / "tools",
            repository_root / "experiments/sensor-integration/kunwei-kwr75b/tools",
            repository_root / "src/ur10e_experiment_runtime",
        )
        self.pending: list[Path] = []
        self.visited: set[Path] = set()
        self.stdlib: dict[str, set[str]] = {}
        self.uv_lock: dict[str, set[str]] = {}
        self.host_contract: dict[str, set[str]] = {}
        self.profile_probe_seen = False

    def _record_external(self, module: str, source: str) -> None:
        root = module.split(".", 1)[0]
        if root in sys.stdlib_module_names:
            self.stdlib.setdefault(root, set()).add(source)
            return
        if root in UV_IMPORT_DISTRIBUTIONS:
            distribution = UV_IMPORT_DISTRIBUTIONS[root]
            if distribution not in self.lock_packages:
                raise SourceClosureError(
                    f"import {root!r} maps to absent uv.lock distribution {distribution!r}"
                )
            self.uv_lock.setdefault(root, set()).add(source)
            return
        if root in HOST_IMPORT_PACKAGES:
            package = HOST_IMPORT_PACKAGES[root]
            packages = self.contract.get("ros", {}).get("packages", {})
            if not isinstance(packages, Mapping) or package not in packages:
                raise SourceClosureError(
                    f"import {root!r} maps to absent host contract package {package!r}"
                )
            self.host_contract.setdefault(root, set()).add(source)
            return
        raise SourceClosureError(f"unclassified import {module!r} used by {source}")

    def _record_module(self, module: str, source: str) -> set[Path]:
        files = _module_files(module, self.search_roots)
        if files:
            self.pending.extend(files - self.visited)
            return files
        self._record_external(module, source)
        return set()

    def _validate_profile_probe(self, tree: ast.AST, source: str) -> None:
        if source != (
            "experiments/tase-contact-reproduction/" + _PROFILE_PROBE_SOURCE
        ):
            return
        values = [
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(node.value.value, str)
            and (
                (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == _PROFILE_PROBE_SYMBOL
                        for target in node.targets
                    )
                )
                or (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == _PROFILE_PROBE_SYMBOL
                )
            )
        ]
        if len(values) != 1:
            raise SourceClosureError("runtime _PROFILE_PROBE definition differs")
        try:
            probe_tree = ast.parse(values[0], filename=f"{source}:{_PROFILE_PROBE_SYMBOL}")
        except SyntaxError as exc:
            raise SourceClosureError(f"runtime _PROFILE_PROBE is invalid Python: {exc}") from exc
        dynamic = [
            node
            for node in ast.walk(probe_tree)
            if isinstance(node, ast.Call) and _dynamic_import_call(node)
        ]
        governed_loops = [
            node
            for node in ast.walk(probe_tree)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "name"
            and isinstance(node.iter, ast.Subscript)
            and isinstance(node.iter.value, ast.Name)
            and node.iter.value.id == "contract"
            and isinstance(node.iter.slice, ast.Constant)
            and node.iter.slice.value == "required_imports"
            and any(call is dynamic[0] for call in ast.walk(node))
        ] if dynamic else []
        if (
            len(dynamic) != 1
            or len(governed_loops) != 1
            or len(dynamic[0].args) != 1
            or not isinstance(dynamic[0].args[0], ast.Name)
            or dynamic[0].args[0].id != "name"
        ):
            raise SourceClosureError(
                "runtime _PROFILE_PROBE dynamic import is not contract-governed"
            )
        profiles = self.contract.get("profiles")
        if not isinstance(profiles, Mapping) or not profiles:
            raise SourceClosureError("runtime contract profiles are missing")
        for profile, row in profiles.items():
            if not isinstance(row, Mapping):
                raise SourceClosureError(f"runtime contract profile {profile!r} is invalid")
            imports = row.get("required_imports")
            if not isinstance(imports, list) or not imports:
                raise SourceClosureError(
                    f"runtime contract profile {profile!r} required_imports differ"
                )
            for module in imports:
                if not isinstance(module, str) or not module:
                    raise SourceClosureError(
                        f"runtime contract profile {profile!r} has invalid import"
                    )
                self._record_external(module, source)
        self.profile_probe_seen = True

    def _visit_python(self, path: Path) -> None:
        source = _repository_relative(path, self.repository_root)
        try:
            tree = ast.parse(path.read_bytes(), filename=source)
        except (OSError, SyntaxError) as exc:
            raise SourceClosureError(f"cannot parse production source {source}: {exc}") from exc
        current_module = _module_name(
            path, self.experiment_root, self.repository_root
        )
        current_package = ""
        if current_module:
            current_package = (
                current_module
                if path.name == "__init__.py"
                else current_module.rpartition(".")[0]
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._record_module(alias.name, source)
            elif isinstance(node, ast.ImportFrom):
                base = _resolved_import_base(node, current_package, source=source)
                base_files = self._record_module(base, source) if base else set()
                if node.level and not base_files:
                    raise SourceClosureError(
                        f"unresolved relative import in {source}:{node.lineno}"
                    )
                if base_files:
                    for alias in node.names:
                        if alias.name != "*":
                            module = ".".join(part for part in (base, alias.name) if part)
                            files = _module_files(module, self.search_roots)
                            self.pending.extend(files - self.visited)
            elif isinstance(node, ast.Call) and _dynamic_import_call(node):
                if (
                    len(node.args) != 1
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    raise SourceClosureError(
                        f"non-literal dynamic import in {source}:{node.lineno}"
                    )
                self._record_module(node.args[0].value, source)
        self._validate_profile_probe(tree, source)

    def resolve(self) -> None:
        while self.pending:
            path = self.pending.pop()
            if path in self.visited:
                continue
            self.visited.add(path)
            if path.suffix == ".py":
                self._visit_python(path)
        if not self.profile_probe_seen:
            raise SourceClosureError("runtime _PROFILE_PROBE was not registered")


def _runtime_contract(experiment_root: Path) -> dict[str, Any]:
    path = _safe_file(experiment_root, _RUNTIME_CONTRACT, "runtime contract")
    contract = _strict_object(path, "runtime contract")
    if contract.get("schema") != "step5d.autotune-v3/runtime-contract-v1":
        raise SourceClosureError("runtime contract schema differs")
    required = {
        "python",
        "uv",
        "profiles",
        "host",
        "ros",
        "gpu",
        "calibration",
        "owner_dependencies",
    }
    if not required < set(contract):
        raise SourceClosureError("runtime contract classification fields are incomplete")
    return contract


def _lock_packages(experiment_root: Path) -> set[str]:
    path = _safe_file(experiment_root, "uv.lock", "uv lock")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourceClosureError(f"uv lock is unreadable: {exc}") from exc
    packages = set(_LOCK_PACKAGE_RE.findall(text))
    if not packages:
        raise SourceClosureError("uv lock has no package records")
    return packages


def _validate_runtime_data(
    experiment_root: Path,
    repository_root: Path,
    contract: Mapping[str, Any],
) -> tuple[set[Path], set[Path]]:
    experiment_paths = {
        _safe_file(experiment_root, relative, "production data")
        for relative in PRODUCTION_EXPERIMENT_DATA
    }
    repository_paths = {
        _safe_file(repository_root, relative, "production repository data")
        for relative in PRODUCTION_REPOSITORY_ASSETS
    }
    pyproject = experiment_root / "pyproject.toml"
    expected_manifest = contract.get("uv", {}).get("dependency_manifest_sha256")
    if expected_manifest != hashlib.sha256(pyproject.read_bytes()).hexdigest():
        raise SourceClosureError("pyproject.toml differs from runtime contract")

    calibration = contract.get("calibration")
    if not isinstance(calibration, Mapping):
        raise SourceClosureError("runtime calibration contract is missing")
    calibration_relative = calibration.get("artifact_path")
    if calibration_relative not in PRODUCTION_EXPERIMENT_DATA:
        raise SourceClosureError("runtime calibration artifact is outside active closure")
    calibration_path = _safe_file(
        experiment_root, str(calibration_relative), "runtime calibration artifact"
    )
    if calibration.get("artifact_sha256") != hashlib.sha256(
        calibration_path.read_bytes()
    ).hexdigest():
        raise SourceClosureError("runtime calibration artifact digest differs")
    calibration_payload = _strict_object(calibration_path, "runtime calibration")
    calibrated_model = calibration_payload.get("calibrated_model")
    if not isinstance(calibrated_model, Mapping):
        raise SourceClosureError("runtime calibration model is missing")
    yaml_relative = calibrated_model.get("calibration_yaml")
    if not isinstance(yaml_relative, str) or not yaml_relative:
        raise SourceClosureError("runtime calibration YAML path is missing")
    unresolved_yaml = experiment_root / yaml_relative
    try:
        yaml_path = unresolved_yaml.resolve(strict=True)
        yaml_repo_relative = _repository_relative(yaml_path, repository_root)
    except (OSError, ValueError) as exc:
        raise SourceClosureError("runtime calibration YAML escapes repository") from exc
    if unresolved_yaml.is_symlink() or yaml_repo_relative not in PRODUCTION_REPOSITORY_ASSETS:
        raise SourceClosureError("runtime calibration YAML is outside active closure")
    yaml_digest = hashlib.sha256(yaml_path.read_bytes()).hexdigest()
    if (
        calibrated_model.get("calibration_yaml_sha256") != yaml_digest
        or calibration.get("yaml_sha256") != yaml_digest
    ):
        raise SourceClosureError("runtime calibration YAML digest differs")
    repository_paths.add(yaml_path)
    return experiment_paths, repository_paths


def _validate_owner_dependency(contract: Mapping[str, Any]) -> dict[str, str]:
    owners = contract.get("owner_dependencies")
    helper = owners.get("controller_helper") if isinstance(owners, Mapping) else None
    if not isinstance(helper, Mapping) or set(helper) != {"owner_id", "sha256"}:
        raise SourceClosureError("controller helper owner binding is missing")
    owner_id = helper.get("owner_id")
    digest = helper.get("sha256")
    if not isinstance(owner_id, str) or not owner_id:
        raise SourceClosureError("controller helper owner ID is invalid")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise SourceClosureError("controller helper SHA-256 is invalid")
    return {
        "kind": "executable",
        "name": "controller_helper",
        "owner_id": owner_id,
        "contract_key": "owner_dependencies.controller_helper.sha256",
        "sha256": digest,
    }


def production_source_closure_report(experiment_root: Path) -> dict[str, Any]:
    """Return a strict, clone-independent classification of active V3 inputs."""

    try:
        experiment = experiment_root.resolve(strict=True)
    except OSError as exc:
        raise SourceClosureError(f"experiment root is unavailable: {exc}") from exc
    repository = experiment.parents[1]
    contract = _runtime_contract(experiment)
    lock_packages = _lock_packages(experiment)
    required_distributions: set[str] = set()
    profiles = contract.get("profiles")
    if not isinstance(profiles, Mapping):
        raise SourceClosureError("runtime contract profiles are missing")
    for profile, row in profiles.items():
        distributions = row.get("required_distributions") if isinstance(row, Mapping) else None
        if not isinstance(distributions, Mapping) or not distributions:
            raise SourceClosureError(
                f"runtime contract profile {profile!r} distributions differ"
            )
        if any(not isinstance(name, str) or not name for name in distributions):
            raise SourceClosureError(
                f"runtime contract profile {profile!r} has invalid distribution"
            )
        required_distributions.update(distributions)
    absent = sorted(required_distributions - lock_packages)
    if absent:
        raise SourceClosureError(f"runtime contract distributions absent from uv.lock: {absent}")

    resolver = _Resolver(
        experiment_root=experiment,
        repository_root=repository,
        contract=contract,
        lock_packages=lock_packages,
    )
    for relative in PRODUCTION_EXPERIMENT_SEEDS:
        resolver.pending.append(_safe_file(experiment, relative, "production source seed"))
    experiment_data, repository_data = _validate_runtime_data(
        experiment, repository, contract
    )
    resolver.pending.extend(experiment_data)
    resolver.pending.extend(repository_data)
    resolver.resolve()

    experiment_paths: set[str] = set()
    repository_paths: set[str] = set()
    repository_rows: list[dict[str, str]] = []
    for path in sorted(resolver.visited):
        repository_relative = _repository_relative(path, repository)
        try:
            relative = path.relative_to(experiment)
        except ValueError:
            repository_paths.add(repository_relative)
            scope = "repository"
        else:
            experiment_paths.add(PurePosixPath(relative).as_posix())
            scope = "experiment"
        repository_rows.append(
            {"kind": "file", "path": repository_relative, "identity_scope": scope}
        )

    def import_rows(
        sources: Mapping[str, set[str]], *, provider: Mapping[str, str] | None = None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in sorted(sources):
            row: dict[str, Any] = {
                "kind": "python_import",
                "name": name,
                "used_by": sorted(sources[name]),
            }
            if provider is not None:
                row["provider"] = provider[name]
            rows.append(row)
        return rows

    host_rows = import_rows(resolver.host_contract, provider=HOST_IMPORT_PACKAGES)
    host_rows.extend(
        {
            "kind": "executable",
            "name": name,
            "contract_key": contract_key,
        }
        for name, contract_key in HOST_EXECUTABLE_INPUTS
    )
    host_rows.append(
        {
            "kind": "dynamic_import_seam",
            "name": "runtime_profile_probe",
            "source": (
                "experiments/tase-contact-reproduction/" + _PROFILE_PROBE_SOURCE
            ),
            "symbol": _PROFILE_PROBE_SYMBOL,
            "contract_key": "profiles.*.required_imports",
        }
    )
    host_rows.sort(key=lambda row: (str(row["kind"]), str(row["name"])))
    report = {
        "schema": SOURCE_CLOSURE_SCHEMA,
        "reason_code_on_failure": SOURCE_CLOSURE_REASON_CODE,
        "experiment_paths": sorted(experiment_paths),
        "repository_paths": sorted(repository_paths),
        "classifications": {
            "stdlib": import_rows(resolver.stdlib),
            "repository": repository_rows,
            "uv_lock": import_rows(
                resolver.uv_lock, provider=UV_IMPORT_DISTRIBUTIONS
            ),
            "host_contract": host_rows,
            "sha_bound_owner": [_validate_owner_dependency(contract)],
        },
        "unresolved": [],
    }
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if str(experiment) in encoded or str(repository) in encoded:
        raise SourceClosureError("source closure report contains checkout location")
    return report


def production_source_closure(
    experiment_root: Path,
) -> tuple[frozenset[str], frozenset[str]]:
    """Compatibility wrapper used by immutable release identity."""

    report = production_source_closure_report(experiment_root)
    return (
        frozenset(report["experiment_paths"]),
        frozenset(report["repository_paths"]),
    )


__all__ = [
    "HOST_IMPORT_PACKAGES",
    "PRODUCTION_EXPERIMENT_DATA",
    "PRODUCTION_EXPERIMENT_SEEDS",
    "PRODUCTION_REPOSITORY_ASSETS",
    "SOURCE_CLOSURE_REASON_CODE",
    "SOURCE_CLOSURE_SCHEMA",
    "SourceClosureError",
    "UV_IMPORT_DISTRIBUTIONS",
    "production_source_closure",
    "production_source_closure_report",
]
