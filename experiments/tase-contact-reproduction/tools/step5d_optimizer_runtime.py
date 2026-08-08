"""Lineage-neutral resolver and subprocess protocol for CUDA optimizers.

The control host is deliberately a different process from the optimizer.  A
caller declares the canonical V3 runtime profile (normally ``optimizer``),
then this module revalidates that profile before every request.  The module
contains no numerical/GPU imports; Torch is imported only by the child worker
started through the resolved interpreter.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping

from step5d_autotune_v3 import runtime_environment as v3_environment
from step5d_autotune_v3 import runtime_installation as v3_runtime


REQUEST_SCHEMA = "step5d.optimizer-runtime/request-v1"
RESPONSE_SCHEMA = "step5d.optimizer-runtime/response-v1"
ATTESTATION_SCHEMA = "step5d.optimizer-runtime/child-attestation-v1"
DEFAULT_STALE_AFTER_S = 30.0 * 24.0 * 60.0 * 60.0
_REQUIRED_GPU_DISTRIBUTIONS = ("torch", "botorch", "gpytorch")
_HEX64 = frozenset("0123456789abcdef")


class OptimizerRuntimeError(RuntimeError):
    """A runtime pointer, child identity, or CUDA optimizer protocol failed."""


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX64 for character in value)
    ):
        raise OptimizerRuntimeError(f"{role} is not a lowercase SHA-256")
    return value


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
    except (TypeError, ValueError) as exc:
        raise OptimizerRuntimeError(f"optimizer protocol payload is not canonical: {exc}") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_json(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise OptimizerRuntimeError(f"{role} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OptimizerRuntimeError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise OptimizerRuntimeError(f"{role} must be a JSON object")
    return value


def _attestation(pointer: Mapping[str, Any]) -> dict[str, Any]:
    try:
        path = Path(str(pointer["attestation_path"]))
        expected = _digest(pointer["attestation_sha256"], "runtime attestation digest")
    except (KeyError, TypeError) as exc:
        raise OptimizerRuntimeError("V3 runtime pointer lacks attestation identity") from exc
    if path.is_symlink() or not path.is_file():
        raise OptimizerRuntimeError("V3 runtime attestation is missing or unsafe")
    actual = _sha256(path.read_bytes())
    if actual != expected:
        raise OptimizerRuntimeError("V3 runtime attestation digest differs")
    payload = _strict_json(path, "V3 runtime attestation")
    if payload.get("schema") != v3_runtime.ATTESTATION_SCHEMA:
        raise OptimizerRuntimeError("V3 runtime attestation schema differs")
    return payload


@dataclass(frozen=True)
class OptimizerProfileDeclaration:
    """The small profile declaration consumed by V4, V5, and later lineages."""

    profile: str = "optimizer"
    stale_after_s: float = DEFAULT_STALE_AFTER_S

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or not self.profile:
            raise OptimizerRuntimeError("optimizer profile declaration is empty")
        if self.profile not in v3_runtime.PROFILES:
            raise OptimizerRuntimeError(f"unknown canonical V3 optimizer profile: {self.profile}")
        if self.profile != "optimizer":
            raise OptimizerRuntimeError(
                "CUDA qLogNEI requires the canonical V3 optimizer profile"
            )
        if isinstance(self.stale_after_s, bool) or not isinstance(self.stale_after_s, (int, float)):
            raise OptimizerRuntimeError("optimizer pointer stale_after_s is not numeric")
        if float(self.stale_after_s) <= 0.0:
            raise OptimizerRuntimeError("optimizer pointer stale_after_s must be positive")


@dataclass(frozen=True)
class ResolvedOptimizerRuntime:
    """Immutable child-launch identity derived from one V3 pointer read."""

    declaration: OptimizerProfileDeclaration
    pointer: Mapping[str, Any]
    attestation: Mapping[str, Any]
    python_executable: Path
    environment_id: str
    environment_hash: str
    required_versions: Mapping[str, str]
    gpu_name: str
    gpu_uuid: str
    bundle_id: str
    runtime_attestation_sha256: str

    def child_environment(self, source: Mapping[str, str] | None = None) -> dict[str, str]:
        """Create the canonical V3 sanitized optimizer environment."""

        values = dict(os.environ if source is None else source)
        try:
            environment = v3_environment.production_runtime_environment(
                values,
                profile=self.declaration.profile,
                runtime_pointer=self.pointer,
                additions={
                    "STEP5D_SHARED_OPTIMIZER_ENVIRONMENT_ID": self.environment_id,
                    "STEP5D_SHARED_OPTIMIZER_ENVIRONMENT_HASH": self.environment_hash,
                    "STEP5D_SHARED_OPTIMIZER_REQUIRED_VERSIONS": json.dumps(
                        dict(self.required_versions), sort_keys=True, separators=(",", ":")
                    ),
                    "STEP5D_SHARED_EXPECTED_GPU_NAME": self.gpu_name,
                    "STEP5D_SHARED_EXPECTED_GPU_UUID": self.gpu_uuid,
                },
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            raise OptimizerRuntimeError(
                f"canonical V3 optimizer environment could not be built: {exc}"
            ) from exc
        # These keys must never let a caller change the child's identity.
        if environment.get("STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID") != self.environment_id:
            raise OptimizerRuntimeError("optimizer environment ID was not bound by V3")
        if environment.get("STEP5D_V3_GPU_UUID") != self.gpu_uuid:
            raise OptimizerRuntimeError("optimizer GPU UUID was not bound by V3")
        if environment.get("UR10E_RNN_GPU_DEVICE") != "cuda:0":
            raise OptimizerRuntimeError("optimizer device binding is not CUDA")
        return environment

    def expected_attestation(self) -> dict[str, Any]:
        return {
            "schema": ATTESTATION_SCHEMA,
            "bundle_id": self.bundle_id,
            "runtime_attestation_sha256": self.runtime_attestation_sha256,
            "profile": self.declaration.profile,
            "environment_id": self.environment_id,
            "environment_hash": self.environment_hash,
            "required_versions": dict(self.required_versions),
            "cuda_available": True,
            "gpu_name": self.gpu_name,
            "gpu_uuid": self.gpu_uuid,
        }


def resolve_optimizer_runtime(
    declaration: OptimizerProfileDeclaration | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    pointer_loader: Callable[..., Mapping[str, Any]] | None = None,
    now_unix_s: float | None = None,
) -> ResolvedOptimizerRuntime:
    """Resolve and verify the canonical V3 optimizer profile, fail closed."""

    selected = declaration or OptimizerProfileDeclaration()
    values = os.environ if environ is None else environ
    loader = pointer_loader or v3_runtime.load_runtime_pointer_identity
    try:
        pointer = dict(loader(environ=values))
    except TypeError:
        # Test/future resolvers may expose a zero-argument loader; production
        # V3's resolver always accepts ``environ``.
        try:
            pointer = dict(loader())
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise OptimizerRuntimeError(f"canonical V3 runtime pointer unavailable: {exc}") from exc
    except Exception as exc:
        raise OptimizerRuntimeError(f"canonical V3 runtime pointer unavailable: {exc}") from exc

    if pointer.get("schema") != v3_runtime.POINTER_SCHEMA:
        raise OptimizerRuntimeError("canonical V3 runtime pointer schema differs")
    bundle_id = _digest(pointer.get("bundle_id"), "runtime bundle ID")
    runtime_attestation_sha256 = _digest(
        pointer.get("attestation_sha256"), "runtime attestation digest"
    )
    profiles = pointer.get("profiles")
    if not isinstance(profiles, Mapping) or selected.profile not in profiles:
        raise OptimizerRuntimeError("canonical V3 optimizer profile is absent")

    try:
        contract = v3_runtime.load_runtime_contract()
        contract_profile = dict(contract["profiles"][selected.profile])
        row = dict(profiles[selected.profile])
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise OptimizerRuntimeError("canonical V3 optimizer profile contract is unavailable") from exc
    required_versions = {
        name: str(contract_profile["required_distributions"][name])
        for name in _REQUIRED_GPU_DISTRIBUTIONS
    }
    if any(not value for value in required_versions.values()):
        raise OptimizerRuntimeError("canonical V3 GPU optimizer version contract is incomplete")
    try:
        environment_id = _digest(row["environment_id"], "optimizer environment ID")
        environment_hash = _digest(row["profile_tree_sha256"], "optimizer profile hash")
        root = Path(str(row["root"]))
        python_executable = Path(str(row["python_executable"]))
    except (KeyError, TypeError) as exc:
        raise OptimizerRuntimeError("canonical V3 optimizer profile identity is incomplete") from exc
    if not root.is_absolute() or not python_executable.is_absolute():
        raise OptimizerRuntimeError("optimizer runtime paths must be absolute")
    if row.get("python_executable") != str(root / "bin/python"):
        raise OptimizerRuntimeError("optimizer pointer interpreter is not root/bin/python")
    if python_executable.name not in {"python", "python3", "python3.10"}:
        raise OptimizerRuntimeError("optimizer pointer does not name a managed interpreter")
    if str(python_executable) in {"python", "python3", "python3.10"}:
        raise OptimizerRuntimeError("plain python interpreter fallback is forbidden")
    if python_executable == Path(os.path.abspath(sys.executable)):
        raise OptimizerRuntimeError("optimizer must not inherit the control host interpreter")
    if root == Path(os.path.abspath(sys.prefix)):
        raise OptimizerRuntimeError("optimizer must not reuse the control host prefix")
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise OptimizerRuntimeError("optimizer interpreter is missing or not executable")

    attestation = _attestation(pointer)
    try:
        observed_ns = int(attestation["observed_at_unix_ns"])
        observed_profile = dict(attestation["profiles"][selected.profile])
        expected_gpu = dict(contract["gpu"])
        observed_gpu = dict(attestation["host"]["gpu"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OptimizerRuntimeError("V3 attestation lacks optimizer/GPU identity") from exc
    now = float(__import__("time").time() if now_unix_s is None else now_unix_s)
    age_s = now - observed_ns / 1_000_000_000.0
    if age_s < 0.0 or age_s > float(selected.stale_after_s):
        raise OptimizerRuntimeError("canonical V3 runtime pointer is stale")
    if (
        observed_profile.get("environment_id") != environment_id
        or observed_profile.get("profile_tree_sha256") != environment_hash
        or observed_profile.get("required_distributions") != contract_profile["required_distributions"]
    ):
        raise OptimizerRuntimeError("V3 optimizer attestation differs from its contract/pointer")
    if observed_gpu != expected_gpu:
        raise OptimizerRuntimeError("V3 expected GPU identity differs from attestation")
    gpu_name = str(expected_gpu.get("name", ""))
    gpu_uuid = str(expected_gpu.get("uuid", ""))
    if not gpu_name or not gpu_uuid.startswith("GPU-"):
        raise OptimizerRuntimeError("V3 GPU identity is incomplete")

    return ResolvedOptimizerRuntime(
        declaration=selected,
        pointer=pointer,
        attestation=attestation,
        python_executable=python_executable,
        environment_id=environment_id,
        environment_hash=environment_hash,
        required_versions=required_versions,
        gpu_name=gpu_name,
        gpu_uuid=gpu_uuid,
        bundle_id=bundle_id,
        runtime_attestation_sha256=runtime_attestation_sha256,
    )


class OptimizerSubprocessClient:
    """One-shot, attested request client for a resolved optimizer child."""

    def __init__(
        self,
        *,
        declaration: OptimizerProfileDeclaration | None = None,
        worker_module: str,
        timeout_s: float = 180.0,
        runtime_resolver: Callable[[], ResolvedOptimizerRuntime] | None = None,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(worker_module, str) or not worker_module:
            raise OptimizerRuntimeError("optimizer worker module is empty")
        if timeout_s <= 0.0:
            raise OptimizerRuntimeError("optimizer worker timeout must be positive")
        self.declaration = declaration or OptimizerProfileDeclaration()
        self.worker_module = worker_module
        self.timeout_s = float(timeout_s)
        self._runtime_resolver = runtime_resolver
        self._source_environment = dict(os.environ if source_environment is None else source_environment)
        self.last_child_attestation: Mapping[str, Any] | None = None

    def _resolve(self) -> ResolvedOptimizerRuntime:
        if self._runtime_resolver is not None:
            runtime = self._runtime_resolver()
            if not isinstance(runtime, ResolvedOptimizerRuntime):
                raise OptimizerRuntimeError("injected optimizer runtime resolver returned the wrong type")
            return runtime
        return resolve_optimizer_runtime(self.declaration, environ=self._source_environment)

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        runtime = self._resolve()
        body = {
            "schema": REQUEST_SCHEMA,
            "profile": runtime.declaration.profile,
            "expected_attestation": runtime.expected_attestation(),
            "payload": dict(payload),
        }
        encoded = _canonical_bytes(body)
        request_sha256 = _sha256(encoded)
        environment = runtime.child_environment(self._source_environment)
        try:
            completed = subprocess.run(
                [str(runtime.python_executable), "-B", "-m", self.worker_module],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                input=encoded,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OptimizerRuntimeError(f"optimizer child could not start: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise OptimizerRuntimeError(
                f"optimizer child failed closed ({completed.returncode}): {detail}"
            )
        try:
            response = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OptimizerRuntimeError(f"optimizer child response is not JSON: {exc}") from exc
        if not isinstance(response, Mapping):
            raise OptimizerRuntimeError("optimizer child response is not an object")
        if (
            response.get("schema") != RESPONSE_SCHEMA
            or response.get("request_sha256") != request_sha256
            or response.get("ok") is not True
        ):
            raise OptimizerRuntimeError("optimizer child response binding differs")
        child_attestation = response.get("attestation")
        if not isinstance(child_attestation, Mapping):
            raise OptimizerRuntimeError("optimizer child did not self-attest its environment")
        expected = runtime.expected_attestation()
        if dict(child_attestation) != expected:
            raise OptimizerRuntimeError("optimizer child environment/GPU/version attestation differs")
        self.last_child_attestation = dict(child_attestation)
        if "result" not in response or not isinstance(response["result"], Mapping):
            raise OptimizerRuntimeError("optimizer child result is missing")
        return response["result"]


__all__ = [
    "ATTESTATION_SCHEMA",
    "DEFAULT_STALE_AFTER_S",
    "OptimizerProfileDeclaration",
    "OptimizerRuntimeError",
    "OptimizerSubprocessClient",
    "ResolvedOptimizerRuntime",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "resolve_optimizer_runtime",
]
