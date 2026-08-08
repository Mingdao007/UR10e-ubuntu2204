"""Offline algebra fixtures plus the managed CUDA r006 optimizer protocol."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from step5d_managed_runtime import ManagedRuntimeError, ManagedRuntimeManifest, resolve_managed_optimizer_runtime
from step5d_optimizer_runtime import OptimizerProfileDeclaration, OptimizerRuntimeError, OptimizerSubprocessClient

from .lattice import ParameterPoint, PointObservation, TrustRegion


class OptimizerError(RuntimeError):
    """A managed optimizer, fit/freeze, or certificate invariant failed."""


def feature_map(point: ParameterPoint) -> tuple[float, ...]:
    """The exact seven production coordinates from the r006 contract."""

    p = point.p_gain
    return (
        -math.log2(p),
        math.log2(point.d_gain / p),
        0.0 if point.i_gain == 0.0 else math.log2(point.i_gain / p),
        math.log2(point.tau_s),
        math.log2(point.ko),
        math.log2(point.kp),
        0.0 if point.i_gain == 0.0 else 1.0,
    )


def _m52(distance: float) -> float:
    root5 = math.sqrt(5.0) * distance
    return (1.0 + root5 + 5.0 * distance * distance / 3.0) * math.exp(-root5)


@dataclass(frozen=True)
class GPObservation:
    point: ParameterPoint
    y_n: float
    std_n: float = 0.0


@dataclass(frozen=True)
class GPPosterior:
    mean_n: float
    variance_n2: float

    @property
    def std_n(self) -> float:
        return math.sqrt(max(0.0, self.variance_n2))


@dataclass(frozen=True)
class GPHyperparameters:
    signal_scale_n: float
    lengthscales: tuple[float, ...]
    repeat_noise_n2: float
    fit_group: int


class ConditionalMatern52GP:
    """Named offline deterministic fixture; never used by the live route."""

    kernel_name = "conditional_matern52"
    train_yvar_n2 = 1e-4
    feature_names = (
        "-log2(P)",
        "log2(D/P)",
        "conditional_log2(I/P)",
        "log2(tau)",
        "log2(Ko)",
        "log2(Kp)",
        "I_mode",
    )

    def __init__(self) -> None:
        self.observations: list[GPObservation] = []
        self.hyperparameters: GPHyperparameters | None = None
        self.fit_groups: list[int] = []
        self.frozen = False

    def add(self, observation: GPObservation) -> None:
        if self.frozen:
            raise OptimizerError("production GP is frozen")
        if not math.isfinite(observation.y_n) or observation.y_n < 0.0:
            raise OptimizerError("GP observation is invalid")
        self.observations.append(observation)

    def fit(self, group: int) -> GPHyperparameters:
        if self.frozen:
            raise OptimizerError("hyperparameters cannot fit after GP is frozen")
        if group not in (1, 2) or group in self.fit_groups:
            raise OptimizerError("r006 GP fits only during the two warm-start groups")
        if not self.observations:
            raise OptimizerError("GP cannot fit without observations")
        values = [item.y_n for item in self.observations]
        repeats: dict[str, list[float]] = {}
        for item in self.observations:
            repeats.setdefault(item.point.uid, []).append(item.y_n)
        repeat_residuals = [statistics.pvariance(row) for row in repeats.values() if len(row) > 1]
        signal = max(1e-4, statistics.pstdev(values) if len(values) > 1 else abs(values[0]) * 0.1)
        # Plus/minus probes determine deterministic per-feature scales; absent
        # probes retain a stable unit scale rather than fitting post-freeze.
        spans: list[float] = []
        for index in range(7):
            coordinates = [feature_map(item.point)[index] for item in self.observations]
            spans.append(max(1.0, (max(coordinates) - min(coordinates)) if coordinates else 1.0))
        self.hyperparameters = GPHyperparameters(
            signal_scale_n=signal,
            lengthscales=tuple(round(span, 12) for span in spans),
            repeat_noise_n2=max(self.train_yvar_n2, statistics.fmean(repeat_residuals) if repeat_residuals else self.train_yvar_n2),
            fit_group=group,
        )
        self.fit_groups.append(group)
        return self.hyperparameters

    def freeze(self) -> None:
        if self.hyperparameters is None or self.fit_groups != [1, 2]:
            raise OptimizerError("production GP freezes only after both warm-start fits")
        self.frozen = True

    def _kernel(self, left: ParameterPoint, right: ParameterPoint) -> float:
        left_features = feature_map(left)
        right_features = feature_map(right)
        assert self.hyperparameters is not None
        distance = math.sqrt(
            sum(
                ((a - b) / max(scale, 1e-9)) ** 2
                for a, b, scale in zip(left_features, right_features, self.hyperparameters.lengthscales, strict=True)
            )
        )
        shared = _m52(distance)
        # Shared M52 spans the categorical boundary; same-mode and I-on-only
        # components add conditional structure without replacing it.
        same_mode = _m52(distance) if left.i_mode is right.i_mode else 0.0
        i_on_only = _m52(distance) if left.i_gain > 0.0 and right.i_gain > 0.0 else 0.0
        return shared + same_mode + i_on_only

    def predict(self, point: ParameterPoint) -> GPPosterior:
        if self.hyperparameters is None:
            raise OptimizerError("GP has not been fitted")
        if not self.observations:
            return GPPosterior(0.0, self.hyperparameters.signal_scale_n ** 2 + self.train_yvar_n2)
        weights = [self._kernel(point, item.point) for item in self.observations]
        total = math.fsum(weights)
        if total <= 1e-12:
            mean = statistics.fmean(item.y_n for item in self.observations)
        else:
            mean = math.fsum(weight * item.y_n for weight, item in zip(weights, self.observations, strict=True)) / total
        variance = max(self.train_yvar_n2, (self.hyperparameters.signal_scale_n ** 2) / (1.0 + total))
        return GPPosterior(mean, variance)


@dataclass(frozen=True)
class ShadowModel:
    name: str
    command_invariant: bool = True

    def command_signature(self, point: ParameterPoint, pending: Sequence[ParameterPoint]) -> tuple[str, tuple[str, ...]]:
        return (point.uid, tuple(item.uid for item in pending))


STUDENT_T_VARIATIONAL_SHADOW = ShadowModel("student_t_variational", True)
RBF_SHADOW = ShadowModel("rbf", True)


@dataclass(frozen=True)
class ManagedOptimizerBinding:
    profile: str
    environment_id: str
    environment_hash: str
    torch_version: str
    botorch_version: str
    gpytorch_version: str
    cuda_available: bool
    gpu_uuid: str


class ManagedOptimizer:
    """Resolve the existing managed optimizer environment; never install."""

    def __init__(self, resolver: Callable[[], Any] | None = None) -> None:
        self._resolver = resolver
        self.binding: ManagedOptimizerBinding | None = None

    def resolve(self) -> ManagedOptimizerBinding:
        if self._resolver is None:
            from step5d_optimizer_runtime import resolve_optimizer_runtime

            runtime = resolve_optimizer_runtime()
        else:
            runtime = self._resolver()
        required = ("environment_id", "environment_hash", "torch_version", "botorch_version", "gpytorch_version", "cuda_available", "gpu_uuid")
        if isinstance(runtime, Mapping):
            values = runtime
        else:
            values = {name: getattr(runtime, name, None) for name in required}
        if values.get("cuda_available") is not True:
            raise OptimizerError("CUDA-managed optimizer is unavailable; degraded fallback is forbidden")
        self.binding = ManagedOptimizerBinding(
            profile="optimizer",
            environment_id=str(values["environment_id"]),
            environment_hash=str(values["environment_hash"]),
            torch_version=str(values["torch_version"]),
            botorch_version=str(values["botorch_version"]),
            gpytorch_version=str(values["gpytorch_version"]),
            cuda_available=True,
            gpu_uuid=str(values["gpu_uuid"]),
        )
        return self.binding


@dataclass(frozen=True)
class PACCertificate:
    incumbent_ucb_n: float
    minimum_lcb_n: float
    epsilon_n: float
    region_size: int
    simultaneous_z95: float
    global_convergence_claim: bool = False

    @property
    def gap_n(self) -> float:
        return self.incumbent_ucb_n - self.minimum_lcb_n

    @property
    def passed(self) -> bool:
        return self.region_size > 0 and self.gap_n <= self.epsilon_n and not self.global_convergence_claim


@dataclass(frozen=True)
class CertifiedRegionState:
    """A local certified region that may recenter/expand without global claims."""

    center: ParameterPoint
    radius_octave: float
    global_convergence_claim: bool = False

    @classmethod
    def from_trust_region(cls, region: TrustRegion) -> "CertifiedRegionState":
        return cls(region.center, region.radius_steps * 0.25, False)

    def recenter_expand(self, center: ParameterPoint, *, expansion_octave: float = 0.25) -> "CertifiedRegionState":
        if expansion_octave < 0.0 or not math.isfinite(float(expansion_octave)):
            raise OptimizerError("certified-region expansion is invalid")
        return CertifiedRegionState(center, self.radius_octave + float(expansion_octave), False)


class LocalPAC:
    """Finite-region simultaneous 95 percent envelope only."""

    def __init__(self, *, epsilon_n: float) -> None:
        self.epsilon_n = float(epsilon_n)

    def certify(self, predictions: Mapping[ParameterPoint, GPPosterior], incumbent: ParameterPoint) -> PACCertificate:
        if not predictions or incumbent not in predictions:
            raise OptimizerError("PAC certificate requires a finite region and incumbent")
        z = statistics.NormalDist().inv_cdf(1.0 - 0.05 / (2.0 * len(predictions)))
        incumbent_posterior = predictions[incumbent]
        incumbent_ucb = incumbent_posterior.mean_n + z * incumbent_posterior.std_n
        minimum_lcb = min(posterior.mean_n - z * posterior.std_n for posterior in predictions.values())
        return PACCertificate(incumbent_ucb, minimum_lcb, self.epsilon_n, len(predictions), z, False)


@dataclass(frozen=True)
class R006OptimizerAsk:
    point: ParameterPoint
    metadata: Mapping[str, Any]


class R006CudaQLogNEI:
    """Production client for the attested CUDA BoTorch worker.

    The client sends only typed point keys and an ObservationLedger sidecar
    binding.  The child re-reads every raw artifact and computes objectives in
    its own process; caller metrics cannot enter the training set.
    """

    worker_module = "step5d_autotune_v4_r006.optimizer_worker"

    def __init__(
        self,
        *,
        runtime_manifest: ManagedRuntimeManifest,
        artifact_binding: Mapping[str, Any],
        seed: int,
    ) -> None:
        if not isinstance(runtime_manifest, ManagedRuntimeManifest):
            raise OptimizerError("r006 managed optimizer requires a typed runtime manifest")
        if not isinstance(artifact_binding, Mapping):
            raise OptimizerError("r006 optimizer requires an ObservationLedger artifact binding")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise OptimizerError("r006 optimizer seed is invalid")
        self.runtime_manifest = runtime_manifest
        self.artifact_binding = dict(artifact_binding)
        self.seed = seed
        self.fit_group: int | str = 1
        self.hyperparameters_frozen = False
        self.frozen_model_state: Mapping[str, Any] | None = None
        self.last_metadata: Mapping[str, Any] = {}

    def update_artifact_binding(self, binding: Mapping[str, Any]) -> None:
        if not isinstance(binding, Mapping):
            raise OptimizerError("r006 artifact binding is not typed")
        self.artifact_binding = dict(binding)

    def fit_group_once(self, group: int) -> None:
        if group not in (1, 2) or self.hyperparameters_frozen:
            raise OptimizerError("r006 CUDA hyperparameters fit only during warm-start groups 1 and 2")
        try:
            binding = resolve_managed_optimizer_runtime(self.runtime_manifest)
            client = OptimizerSubprocessClient(
                declaration=OptimizerProfileDeclaration(profile=binding.profile),
                worker_module=binding.worker_module,
                runtime_resolver=lambda: binding.runtime,
            )
            response = client.request(
                {
                    "operation": "fit",
                    "artifact_binding": dict(self.artifact_binding),
                    "choices": [],
                    "pending": [],
                    "incumbent": [0, 0, 0, "OFF", None, 0, 0],
                    "seed": self.seed,
                    "fit_group": group,
                    "hyperparameters_frozen": False,
                    "q": 1,
                }
            )
        except (ManagedRuntimeError, OptimizerRuntimeError, OSError) as exc:
            raise OptimizerError(f"managed r006 CUDA fit group {group} failed closed: {exc}") from exc
        if set(response) != {"metadata", "model_state"} or not isinstance(response["metadata"], Mapping) or not isinstance(response["model_state"], Mapping):
            raise OptimizerError("r006 CUDA fit response fields differ")
        if response["metadata"].get("fit_group") != group:
            raise OptimizerError("r006 CUDA fit group attestation differs")
        self.last_metadata = {
            **dict(response["metadata"]),
            "child_attestation": dict(client.last_child_attestation or {}),
        }
        self.frozen_model_state = dict(response["model_state"])
        self.fit_group = group

    def freeze(self) -> None:
        if self.fit_group != 2:
            raise OptimizerError("r006 optimizer cannot freeze before warm-start group 2")
        if self.frozen_model_state is None:
            raise OptimizerError("r006 optimizer cannot freeze without a fitted model state")
        self.hyperparameters_frozen = True
        self.fit_group = "frozen"

    def ask(
        self,
        *,
        choices: Sequence[ParameterPoint],
        pending: Sequence[ParameterPoint],
        incumbent: ParameterPoint,
        q: int = 1,
    ) -> R006OptimizerAsk:
        if not choices:
            raise OptimizerError("r006 CUDA qLogNEI choice set is empty")
        if q not in (1, 4) or len(choices) < q:
            raise OptimizerError("r006 CUDA qLogNEI supports q=4 or q=1 within the frontier")
        if not isinstance(incumbent, ParameterPoint):
            raise OptimizerError("r006 incumbent is not typed")
        if self.hyperparameters_frozen and self.frozen_model_state is None:
            raise OptimizerError("r006 frozen CUDA GP state is missing")
        try:
            binding = resolve_managed_optimizer_runtime(self.runtime_manifest)
            client = OptimizerSubprocessClient(
                declaration=OptimizerProfileDeclaration(profile=binding.profile),
                worker_module=binding.worker_module,
                runtime_resolver=lambda: binding.runtime,
            )
            response = client.request(
                {
                    "operation": "ask",
                    "artifact_binding": dict(self.artifact_binding),
                    "choices": [list(point.key) for point in choices],
                    "pending": [list(point.key) for point in pending],
                    "incumbent": list(incumbent.key),
                    "seed": self.seed,
                    "fit_group": self.fit_group,
                    "hyperparameters_frozen": self.hyperparameters_frozen,
                    "q": q,
                    **(
                        {"frozen_model_state": dict(self.frozen_model_state)}
                        if self.hyperparameters_frozen
                        else {}
                    ),
                }
            )
        except (ManagedRuntimeError, OptimizerRuntimeError, OSError) as exc:
            raise OptimizerError(f"managed r006 CUDA qLogNEI failed closed: {exc}") from exc
        if set(response) not in ({"selected_point_key", "selected_point_keys", "metadata"}, {"selected_point_key", "metadata"}):
            raise OptimizerError("r006 CUDA response fields differ")
        selected_key = response.get("selected_point_key")
        if not isinstance(selected_key, list):
            raise OptimizerError("r006 CUDA selected point is not typed")
        selected = next((point for point in choices if list(point.key) == selected_key), None)
        if selected is None:
            raise OptimizerError("r006 CUDA selected a point outside the requested graph frontier")
        selected_keys = response.get("selected_point_keys")
        if not isinstance(selected_keys, list) or len(selected_keys) != q:
            raise OptimizerError("r006 CUDA selected batch cardinality differs from q")
        if len({tuple(key) for key in selected_keys if isinstance(key, list)}) != q:
            raise OptimizerError("r006 CUDA selected batch contains duplicate or untyped points")
        choice_keys = {tuple(point.key) for point in choices}
        if any(not isinstance(key, list) or tuple(key) not in choice_keys for key in selected_keys):
            raise OptimizerError("r006 CUDA selected batch escaped the graph frontier")
        metadata = response.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("qlognei") != "qLogNEI" or metadata.get("x_pending") is not True or metadata.get("q") != q:
            raise OptimizerError("r006 CUDA worker did not attest qLogNEI/X_pending")
        self.last_metadata = {**dict(metadata), "child_attestation": dict(client.last_child_attestation or {})}
        return R006OptimizerAsk(selected, self.last_metadata)

    def certificate(
        self,
        *,
        region: Sequence[ParameterPoint],
        incumbent: ParameterPoint,
        epsilon_n: float,
    ) -> Mapping[str, Any]:
        """Ask the managed child for a finite-region simultaneous envelope."""

        if not region or not isinstance(incumbent, ParameterPoint):
            raise OptimizerError("r006 PAC certificate region is not typed")
        if not math.isfinite(float(epsilon_n)) or float(epsilon_n) <= 0.0:
            raise OptimizerError("r006 PAC epsilon is invalid")
        self.update_artifact_binding(self.artifact_binding)
        try:
            binding = resolve_managed_optimizer_runtime(self.runtime_manifest)
            client = OptimizerSubprocessClient(
                declaration=OptimizerProfileDeclaration(profile=binding.profile),
                worker_module=binding.worker_module,
                runtime_resolver=lambda: binding.runtime,
            )
            response = client.request(
                {
                    "operation": "certificate",
                    "artifact_binding": dict(self.artifact_binding),
                    "choices": [list(point.key) for point in region],
                    "pending": [],
                    "incumbent": list(incumbent.key),
                    "seed": self.seed,
                    "fit_group": self.fit_group,
                    "hyperparameters_frozen": self.hyperparameters_frozen,
                    "epsilon_n": float(epsilon_n),
                    "q": 1,
                    **(
                        {"frozen_model_state": dict(self.frozen_model_state)}
                        if self.hyperparameters_frozen
                        else {}
                    ),
                }
            )
        except (ManagedRuntimeError, OptimizerRuntimeError, OSError) as exc:
            raise OptimizerError(f"managed r006 PAC certificate failed closed: {exc}") from exc
        metadata = response.get("metadata") if isinstance(response, Mapping) else None
        certificate = metadata.get("certificate") if isinstance(metadata, Mapping) else None
        if not isinstance(certificate, Mapping):
            raise OptimizerError("r006 CUDA worker did not return a finite-region certificate")
        self.last_metadata = {
            **dict(metadata),
            "child_attestation": dict(client.last_child_attestation or {}),
        }
        return dict(certificate)


class WarmStartOptimizer:
    """Production fit/freeze seam used by the offline fixture."""

    def __init__(self) -> None:
        self.gp = ConditionalMatern52GP()
        self.route_observations: list[ParameterPoint] = []

    def fit_group(self, group: int, observations: Iterable[PointObservation]) -> None:
        for observation in observations:
            self.gp.add(GPObservation(observation.point, observation.mae_n, observation.std_n))
        self.gp.fit(group)

    def include_route_observation(self, point: ParameterPoint) -> None:
        self.route_observations.append(point)

    def freeze(self) -> None:
        self.gp.freeze()

    @property
    def frozen(self) -> bool:
        return self.gp.frozen


class QLogNEIScheduler:
    """q=4 until the finite certificate gap is <=2 epsilon, then q=1."""

    def __init__(self, *, epsilon_n: float) -> None:
        self.epsilon_n = float(epsilon_n)

    def q_for(self, certificate: PACCertificate | None) -> int:
        return 4 if certificate is None or certificate.gap_n > 2.0 * self.epsilon_n else 1


class QLogNEI:
    """Deterministic offline score surface with explicit X_pending binding."""

    name = "qLogNEI"

    def __init__(self, gp: ConditionalMatern52GP) -> None:
        self.gp = gp
        self.last_pending: tuple[ParameterPoint, ...] = ()

    def ask(
        self,
        candidates: Sequence[ParameterPoint],
        *,
        pending: Sequence[ParameterPoint],
        q: int,
    ) -> tuple[ParameterPoint, ...]:
        if q not in (1, 4):
            raise OptimizerError("r006 qLogNEI supports q=4 or q=1")
        self.last_pending = tuple(pending)
        excluded = {item.uid for item in pending}
        scored: list[tuple[float, tuple[object, ...], ParameterPoint]] = []
        for point in candidates:
            if point.uid in excluded:
                continue
            posterior = self.gp.predict(point)
            # Lower MAE is better.  The positive log transform is the qLogNEI
            # command surface; pending is explicitly present in the score.
            score = math.log1p(math.exp(-(posterior.mean_n - posterior.std_n)))
            scored.append((-score, point.key, point))
        scored.sort()
        if len(scored) < q:
            raise OptimizerError("qLogNEI candidate catalog is smaller than q")
        return tuple(item[2] for item in scored[:q])


__all__ = [
    "ConditionalMatern52GP",
    "CertifiedRegionState",
    "GPHyperparameters",
    "GPObservation",
    "GPPosterior",
    "LocalPAC",
    "ManagedOptimizer",
    "ManagedOptimizerBinding",
    "OptimizerError",
    "R006CudaQLogNEI",
    "R006OptimizerAsk",
    "PACCertificate",
    "QLogNEI",
    "QLogNEIScheduler",
    "RBF_SHADOW",
    "STUDENT_T_VARIATIONAL_SHADOW",
    "ShadowModel",
    "WarmStartOptimizer",
    "feature_map",
]
