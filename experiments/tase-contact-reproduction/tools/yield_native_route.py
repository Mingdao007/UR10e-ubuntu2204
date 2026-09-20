"""Transport-free native yield binding and creation for the R006 provider seam.

This route binds the reviewed 2026-09-17 model receipt, frozen-transfer law
parameters (MSFC g50, original active metric), and the explicit NO-v3 observer
file.  It does not load the inherited RNN contract, open a device, or claim
fresh physical qualification.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from step5c_calibrated_kinematics_audit import (
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_XACRO_PATH,
    build_calibrated_model,
)
from yield_contact_provider import YieldContactProvider
from yield_contact_runtime import YieldContactRuntime


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = EXPERIMENT_ROOT / "config" / "yield_native_route_v1.json"
SCHEMA = "yield-native-route-v1"
METHODS = ("SFC", "DSFC", "MSFC")
PRODUCTION_RUNTIME_DEADLINE_S = 0.0015
PRODUCTION_QP_DEADLINE_S = 0.001
CLAIM_SCOPE = (
    "historical model reconciliation with the 2026-09-17 read-only kinematics "
    "receipt; not fresh physical qualification, workspace accuracy, or source closure"
)
_HOME_VECTOR_KEYS = ("home_pose", "home_q")
_ESTIMATOR_KEYS = {
    "initial_inward_normal_base",
    "contact_force_n",
    "excitation_m_s",
    "motion_gain",
    "force_correction_gain",
    "force_correction_max_rad",
    "assumed_friction_mu",
    "min_force_n",
    "motion_normalization_floor_m_s",
    "motion_rate_cap_rad_s",
    "coplanarity_gain_s_inv",
    "coplanarity_cross_floor_n_m_s",
}


class YieldNativeRouteError(RuntimeError):
    """Native yield binding or creation failed closed."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise YieldNativeRouteError(f"{name} must be an object")
    return value


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise YieldNativeRouteError(f"{name} digest is not a lowercase SHA-256")
    return value


def _require_finite_vector(value: Any, length: int, name: str) -> tuple[float, ...]:
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise YieldNativeRouteError(f"{name} must be a finite length-{length} vector") from exc
    if len(vector) != length or not all(
        item == item and abs(item) != float("inf") for item in vector
    ):
        raise YieldNativeRouteError(f"{name} must be a finite length-{length} vector")
    return vector


def validate_home_observations(observations: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = _require_mapping(observations, "home observations")
    for name in _HOME_VECTOR_KEYS:
        if name not in payload:
            raise YieldNativeRouteError(f"home observations missing {name}")
        _require_finite_vector(payload[name], 6, name)
    return payload


@dataclass(frozen=True)
class YieldNativeBinding:
    """Typed native contract consumed by gate_qdot via model_hashes."""

    schema: str
    method: str
    model_hashes: Mapping[str, str]
    expanded_urdf_sha256: str
    calibration_yaml_sha256: str
    xacro_sha256: str
    calibration_hash: str
    controller_identity: str
    runtime_identity: str
    law_build_fingerprint: str
    qp_profile_sha256: str
    observer_sha256: str
    law_parameters: Mapping[str, float]
    observer_parameters: Mapping[str, Any]
    claim_scope: str

    def require_runtime(self, runtime: YieldContactRuntime) -> None:
        actual_urdf = hashlib.sha256(runtime.model.urdf_text.encode()).hexdigest()
        if actual_urdf != runtime.model_urdf_sha256:
            raise YieldNativeRouteError("runtime expanded URDF hash is inconsistent")
        if actual_urdf != self.expanded_urdf_sha256:
            raise YieldNativeRouteError("expanded URDF differs from the native binding")
        if runtime.model.calibration_hash != self.calibration_hash:
            raise YieldNativeRouteError("calibration identity differs from the native binding")
        if runtime.controller.identity != self.controller_identity:
            raise YieldNativeRouteError("controller identity differs from the native binding")
        if runtime.identity != self.runtime_identity:
            raise YieldNativeRouteError("runtime identity differs from the native binding")
        payload = runtime.controller.identity_payload
        if payload.get("law_build_fingerprint") != self.law_build_fingerprint:
            raise YieldNativeRouteError("native fingerprint differs from the native binding")
        if runtime.solver_profile.sha256 != self.qp_profile_sha256:
            raise YieldNativeRouteError("QP profile differs from the native binding")
        if _sha256_json(payload.get("estimator_parameters")) != self.observer_sha256:
            raise YieldNativeRouteError("observer identity differs from the native binding")
        if payload.get("parameters") != dict(self.law_parameters):
            raise YieldNativeRouteError("law parameters differ from the native binding")


def load_route_config(path: Path | str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YieldNativeRouteError(f"native route config is unreadable: {exc}") from exc
    payload = _require_mapping(document, "native route config")
    if payload.get("schema") != SCHEMA:
        raise YieldNativeRouteError("native route schema differs")
    if payload.get("claim_scope") != CLAIM_SCOPE:
        raise YieldNativeRouteError("native route claim scope differs")
    if payload.get("program") != "step5d_contact_six_qp_v1":
        raise YieldNativeRouteError("native route TP program differs")
    if tuple(payload.get("methods") or ()) != METHODS:
        raise YieldNativeRouteError("native route methods differ")
    if payload.get("msfc_gain_variant") != "g50":
        raise YieldNativeRouteError("native route MSFC gain variant is not g50")
    if payload.get("metric") != "original_active":
        raise YieldNativeRouteError("native route metric is not the original active metric")
    deadlines = _require_mapping(payload.get("deadlines"), "deadlines")
    if (
        float(deadlines.get("runtime_s")) != PRODUCTION_RUNTIME_DEADLINE_S
        or float(deadlines.get("qp_s")) != PRODUCTION_QP_DEADLINE_S
    ):
        raise YieldNativeRouteError("native route production deadlines differ")
    model = _require_mapping(payload.get("model"), "model")
    for key in (
        "expanded_urdf_sha256",
        "calibration_yaml_sha256",
        "xacro_sha256",
        "receipt_sha256",
    ):
        _require_sha256(model.get(key), key)
    receipt_rel = model.get("receipt")
    if not isinstance(receipt_rel, str) or not receipt_rel:
        raise YieldNativeRouteError("historical model receipt path is missing")
    receipt_path = EXPERIMENT_ROOT / receipt_rel
    if not receipt_path.is_file() or _sha256_file(receipt_path) != model["receipt_sha256"]:
        raise YieldNativeRouteError("historical model receipt binding differs")
    if not isinstance(model.get("calibration_hash"), str) or not model["calibration_hash"]:
        raise YieldNativeRouteError("calibration hash is missing")
    observer_rel = payload.get("observer_config")
    if not isinstance(observer_rel, str) or not observer_rel:
        raise YieldNativeRouteError("observer config path is missing")
    observer_path = EXPERIMENT_ROOT / observer_rel
    expected_observer = _require_sha256(
        payload.get("observer_config_sha256"), "observer_config_sha256"
    )
    if not observer_path.is_file() or _sha256_file(observer_path) != expected_observer:
        raise YieldNativeRouteError("NO-v3 observer config binding differs")
    source_rel = payload.get("law_parameters_source")
    if not isinstance(source_rel, str) or not source_rel:
        raise YieldNativeRouteError("law parameter source path is missing")
    source_path = EXPERIMENT_ROOT / source_rel
    expected_source = _require_sha256(
        payload.get("law_parameters_source_sha256"), "law_parameters_source_sha256"
    )
    if not source_path.is_file() or _sha256_file(source_path) != expected_source:
        raise YieldNativeRouteError("frozen-transfer protocol binding differs")
    try:
        protocol = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YieldNativeRouteError(f"frozen-transfer protocol is unreadable: {exc}") from exc
    law_parameters = _require_mapping(payload.get("law_parameters"), "law_parameters")
    protocol_parameters = _require_mapping(protocol.get("parameters"), "protocol parameters")
    if dict(law_parameters) != dict(protocol_parameters):
        raise YieldNativeRouteError("configured law parameters differ from frozen-transfer protocol")
    if set(law_parameters) != set(METHODS):
        raise YieldNativeRouteError("configured law methods differ")
    msfc = _require_mapping(law_parameters.get("MSFC"), "MSFC parameters")
    if float(msfc.get("g")) == 0.17166683818219516:
        raise YieldNativeRouteError("MSFC g100 is not the native-route gain")
    return dict(payload)


def load_observer_parameters(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = config if config is not None else load_route_config()
    observer_path = EXPERIMENT_ROOT / str(payload["observer_config"])
    try:
        document = json.loads(observer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YieldNativeRouteError(f"NO-v3 observer config is unreadable: {exc}") from exc
    parameters = dict(_require_mapping(document, "observer parameters"))
    extra = set(parameters) - _ESTIMATOR_KEYS
    if extra:
        raise YieldNativeRouteError(f"unsupported observer keys: {sorted(extra)}")
    return parameters


def measure_actual_model() -> dict[str, str]:
    xacro_sha256 = _sha256_file(DEFAULT_XACRO_PATH)
    calibration_yaml_sha256 = _sha256_file(DEFAULT_CALIBRATION_YAML)
    model = build_calibrated_model()
    return {
        "expanded_urdf_sha256": hashlib.sha256(model.urdf_text.encode()).hexdigest(),
        "calibration_yaml_sha256": calibration_yaml_sha256,
        "xacro_sha256": xacro_sha256,
        "calibration_hash": model.calibration_hash,
    }


def _require_desired_model(config: Mapping[str, Any], actual: Mapping[str, str]) -> None:
    desired = _require_mapping(config.get("model"), "model")
    for key in (
        "expanded_urdf_sha256",
        "calibration_yaml_sha256",
        "xacro_sha256",
        "calibration_hash",
    ):
        if actual[key] != desired[key]:
            raise YieldNativeRouteError(
                "calibration or expanded model differs from the reviewed native binding"
            )


def binding_from_runtime(
    runtime: YieldContactRuntime,
    *,
    config: Mapping[str, Any],
    actual_model: Mapping[str, str],
) -> YieldNativeBinding:
    if runtime.model_urdf_sha256 != actual_model["expanded_urdf_sha256"]:
        raise YieldNativeRouteError("runtime expanded URDF differs from the measured model")
    if runtime.model.calibration_hash != actual_model["calibration_hash"]:
        raise YieldNativeRouteError("runtime calibration differs from the measured model")
    payload = runtime.controller.identity_payload
    observer_parameters = dict(payload["estimator_parameters"])
    law_parameters = dict(payload["parameters"])
    model_hashes = {
        "expanded_urdf": actual_model["expanded_urdf_sha256"],
        "calibration_yaml": actual_model["calibration_yaml_sha256"],
        "ur_xacro": actual_model["xacro_sha256"],
        "calibration_hash": actual_model["calibration_hash"],
        "controller_identity": runtime.controller.identity,
        "runtime_identity": runtime.identity,
        "native_fingerprint": payload["law_build_fingerprint"],
        "qp_profile": runtime.solver_profile.sha256,
        "observer": _sha256_json(observer_parameters),
    }
    binding = YieldNativeBinding(
        schema=SCHEMA,
        method=runtime.controller.method,
        model_hashes=model_hashes,
        expanded_urdf_sha256=actual_model["expanded_urdf_sha256"],
        calibration_yaml_sha256=actual_model["calibration_yaml_sha256"],
        xacro_sha256=actual_model["xacro_sha256"],
        calibration_hash=actual_model["calibration_hash"],
        controller_identity=runtime.controller.identity,
        runtime_identity=runtime.identity,
        law_build_fingerprint=payload["law_build_fingerprint"],
        qp_profile_sha256=runtime.solver_profile.sha256,
        observer_sha256=model_hashes["observer"],
        law_parameters=law_parameters,
        observer_parameters=observer_parameters,
        claim_scope=str(config["claim_scope"]),
    )
    binding.require_runtime(runtime)
    return binding


def create_native_yield_runtime(
    *,
    method: str,
    qp_library: Path | str,
    anchor_m: Any,
    task_basis: Any,
    approach_inward_base: Any,
    home_observations: Mapping[str, Any] | None = None,
    deadline_s: float | None = PRODUCTION_RUNTIME_DEADLINE_S,
    config_path: Path | str | None = None,
    build_root: Path | str | None = None,
) -> tuple[YieldContactRuntime, YieldNativeBinding]:
    if method not in METHODS:
        raise YieldNativeRouteError("native yield method is not SFC, DSFC, or MSFC")
    if home_observations is not None:
        validate_home_observations(home_observations)
    config = load_route_config(config_path)
    desired = _require_mapping(config.get("model"), "model")
    xacro_sha256 = _sha256_file(DEFAULT_XACRO_PATH)
    calibration_yaml_sha256 = _sha256_file(DEFAULT_CALIBRATION_YAML)
    if (
        xacro_sha256 != desired["xacro_sha256"]
        or calibration_yaml_sha256 != desired["calibration_yaml_sha256"]
    ):
        raise YieldNativeRouteError(
            "calibration or expanded model differs from the reviewed native binding"
        )
    law_parameters = dict(_require_mapping(config["law_parameters"][method], f"{method} parameters"))
    observer_parameters = load_observer_parameters(config)
    runtime = YieldContactRuntime(
        method=method,
        qp_library=qp_library,
        anchor_m=anchor_m,
        task_basis=task_basis,
        approach_inward_base=approach_inward_base,
        deadline_s=deadline_s,
        law_parameters=law_parameters,
        estimator_parameters=observer_parameters,
        build_root=build_root,
    )
    actual_model = {
        "expanded_urdf_sha256": runtime.model_urdf_sha256,
        "calibration_yaml_sha256": calibration_yaml_sha256,
        "xacro_sha256": xacro_sha256,
        "calibration_hash": runtime.model.calibration_hash,
    }
    try:
        _require_desired_model(config, actual_model)
        binding = binding_from_runtime(runtime, config=config, actual_model=actual_model)
    except Exception:
        runtime.close()
        raise
    return runtime, binding


def create_native_yield_provider(
    *,
    runtime: YieldContactRuntime,
    binding: YieldNativeBinding,
    scenario: str = "nominal",
    amplitude_n: float = 0.0,
) -> YieldContactProvider:
    if not isinstance(binding, YieldNativeBinding):
        raise YieldNativeRouteError("native yield provider requires a typed native binding")
    binding.require_runtime(runtime)
    return YieldContactProvider(
        runtime=runtime,
        native_binding=binding,
        scenario=scenario,
        amplitude_n=amplitude_n,
    )


def native_provider_factory(**route_kwargs: Any):
    """Injection-seam factory; construction stays local and transport-free."""

    def factory(**_ignored: Any) -> YieldContactProvider:
        runtime, binding = create_native_yield_runtime(**route_kwargs)
        return create_native_yield_provider(runtime=runtime, binding=binding)

    return factory


def native_contract_for_provider(provider: YieldContactProvider) -> YieldNativeBinding:
    if not isinstance(provider, YieldContactProvider):
        raise YieldNativeRouteError("native contract requires YieldContactProvider")
    binding = getattr(provider, "native_binding", None)
    if not isinstance(binding, YieldNativeBinding):
        raise YieldNativeRouteError("yield provider requires a validated native binding")
    runtime = provider.runtime
    if not isinstance(runtime, YieldContactRuntime):
        raise YieldNativeRouteError("yield provider runtime is not a native yield runtime")
    binding.require_runtime(runtime)
    if dict(provider.model_hashes) != dict(binding.model_hashes):
        raise YieldNativeRouteError("yield provider model hashes differ from native binding")
    return binding
