"""Attested CUDA r006 optimizer worker.

This child is launched only by ``step5d_optimizer_runtime`` through the
managed V3 optimizer interpreter.  It cold-reads the ObservationLedger-owned
r006 artifact sidecar, fits the conditional Matern-5/2 model with fixed
Gaussian noise, and evaluates BoTorch qLogNEI with X_pending.  There is no
CPU/kernel-smoother fallback in this protocol.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
import json
import math
import os
import stat
import statistics
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_optimizer_runtime import ATTESTATION_SCHEMA, REQUEST_SCHEMA, RESPONSE_SCHEMA


class OptimizerWorkerError(RuntimeError):
    """The managed CUDA child failed closed."""


def _digest(value: Any, role: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise OptimizerWorkerError(f"{role} is not a lowercase SHA-256")
    return value


def _expected_environment(expected: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "bundle_id", "runtime_attestation_sha256", "profile",
        "environment_id", "environment_hash", "required_versions",
        "cuda_available", "gpu_name", "gpu_uuid",
    }
    if not isinstance(expected, Mapping) or set(expected) != required or expected.get("schema") != ATTESTATION_SCHEMA:
        raise OptimizerWorkerError("optimizer expected-attestation fields differ")
    if expected.get("profile") != "optimizer" or expected.get("cuda_available") is not True:
        raise OptimizerWorkerError("r006 optimizer requires the managed CUDA profile")
    versions = expected.get("required_versions")
    if not isinstance(versions, Mapping) or set(versions) != {"torch", "botorch", "gpytorch"}:
        raise OptimizerWorkerError("managed Torch/BoTorch/GPyTorch versions are incomplete")
    return dict(expected)


def _self_attest(expected: Mapping[str, Any]) -> dict[str, Any]:
    expected_payload = _expected_environment(expected)
    import torch

    versions = {name: importlib.metadata.version(name) for name in ("torch", "botorch", "gpytorch")}
    if not torch.cuda.is_available():
        raise OptimizerWorkerError("CUDA is unavailable; r006 has no degraded fallback")
    torch.cuda.set_device(0)
    properties = torch.cuda.get_device_properties(0)
    raw_uuid = getattr(properties, "uuid", None)
    if isinstance(raw_uuid, bytes):
        raw_uuid = raw_uuid.decode("utf-8", errors="strict")
    gpu_uuid = str(raw_uuid) if raw_uuid is not None else ""
    if gpu_uuid and not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    observed = {
        "schema": ATTESTATION_SCHEMA,
        "bundle_id": os.environ.get("STEP5D_V3_RUNTIME_BUNDLE_ID"),
        "runtime_attestation_sha256": os.environ.get("STEP5D_V3_RUNTIME_ATTESTATION_SHA256"),
        "profile": os.environ.get("STEP5D_V3_RUNTIME_PROFILE"),
        "environment_id": os.environ.get("STEP5D_SHARED_OPTIMIZER_ENVIRONMENT_ID"),
        "environment_hash": os.environ.get("STEP5D_SHARED_OPTIMIZER_ENVIRONMENT_HASH"),
        "required_versions": versions,
        "cuda_available": True,
        "gpu_name": str(torch.cuda.get_device_name(0)),
        "gpu_uuid": gpu_uuid,
    }
    if observed != expected_payload:
        raise OptimizerWorkerError("r006 child environment/GPU attestation differs")
    return observed


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _row_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({key: value for key, value in row.items() if key != "row_sha256"})).hexdigest()


def _regular_file(value: Any, role: str) -> Path:
    if not isinstance(value, str) or not value:
        raise OptimizerWorkerError(f"{role} path is invalid")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise OptimizerWorkerError(f"{role} is not a regular absolute file")
    if not stat.S_ISREG(path.stat().st_mode) or path.resolve(strict=True) != path:
        raise OptimizerWorkerError(f"{role} is not canonical")
    return path


def _artifact_binding(value: Any) -> dict[str, Any]:
    required = {"sidecar_path", "sidecar_sha256", "campaign_fingerprint", "rows"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise OptimizerWorkerError("r006 artifact binding fields differ")
    sidecar = _regular_file(value["sidecar_path"], "r006 artifact sidecar")
    sidecar_sha = _digest(value["sidecar_sha256"], "r006 artifact sidecar digest")
    campaign = _digest(value["campaign_fingerprint"], "r006 artifact campaign")
    if hashlib.sha256(sidecar.read_bytes()).hexdigest() != sidecar_sha:
        raise OptimizerWorkerError("r006 artifact sidecar bytes differ")
    try:
        lines = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OptimizerWorkerError("r006 artifact sidecar is not strict JSONL") from exc
    if not lines or lines[0].get("schema") != "step5d.autotune-v4/r006-objective-sidecar-v1" or lines[0].get("record_type") != "header":
        raise OptimizerWorkerError("r006 artifact sidecar header differs")
    if lines[0].get("campaign_fingerprint") != campaign:
        raise OptimizerWorkerError("r006 artifact sidecar campaign differs")
    requested_rows = value["rows"]
    if not isinstance(requested_rows, list):
        raise OptimizerWorkerError("r006 artifact refs are not ordered")
    requested_ids = tuple((int(item["attempt_sequence"]), str(item["execution_id"])) for item in requested_rows)
    if len(set(requested_ids)) != len(requested_ids):
        raise OptimizerWorkerError("r006 artifact refs contain duplicates")
    previous = "0" * 64
    normalized: list[dict[str, Any]] = []
    for row in lines[1:]:
        if row.get("record_type") != "objective_artifact" or row.get("previous_sha256") != previous or row.get("row_sha256") != _row_hash(row):
            raise OptimizerWorkerError("r006 artifact sidecar hash chain differs")
        artifact = _regular_file(
            str(sidecar.parent / "r006_raw_objectives" / str(row["artifact_name"])),
            "r006 raw artifact",
        )
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != row.get("artifact_sha256"):
            raise OptimizerWorkerError("r006 raw artifact bytes differ")
        try:
            receipt_payload = json.loads(artifact.read_text(encoding="utf-8"))
            from step5d_autotune_v4_r006.objective import R006ObjectiveReceipt, cold_read_verify

            receipt = cold_read_verify(R006ObjectiveReceipt.from_mapping(receipt_payload), expected_campaign_fingerprint=campaign)
        except Exception as exc:
            raise OptimizerWorkerError("fresh r006 raw artifact verification failed") from exc
        if receipt.attempt_sequence != row.get("attempt_sequence") or receipt.execution_id != row.get("execution_id"):
            raise OptimizerWorkerError("r006 raw artifact attempt/execution binding differs")
        identity = (int(row["attempt_sequence"]), str(row["execution_id"]))
        if identity in requested_ids:
            normalized.append({**dict(row), "receipt": receipt, "point_key": row.get("point_key")})
        previous = str(row["row_sha256"])
    actual_ids = tuple((int(item["attempt_sequence"]), str(item["execution_id"])) for item in normalized)
    if requested_ids != actual_ids:
        raise OptimizerWorkerError("r006 artifact refs differ from fresh sidecar")
    return {"sidecar_path": str(sidecar), "sidecar_sha256": sidecar_sha, "campaign_fingerprint": campaign, "rows": normalized}


def _point(key: Any):
    from step5d_autotune_v4_r006.lattice import IMode, ParameterPoint

    if not isinstance(key, list) or len(key) != 7:
        raise OptimizerWorkerError("r006 point key is invalid")
    try:
        return ParameterPoint(int(key[0]), int(key[1]), int(key[2]), IMode(str(key[3])), None if key[4] is None else int(key[4]), int(key[5]), int(key[6]))
    except Exception as exc:
        raise OptimizerWorkerError("r006 point key is not typed") from exc


def _features(point) -> tuple[float, ...]:
    import math

    p = point.p_gain
    return (
        -math.log2(p),
        math.log2(point.d_gain / p),
        0.0 if point.i_mode.value == "OFF" else math.log2(point.i_gain / p),
        math.log2(point.tau_s),
        math.log2(point.ko),
        math.log2(point.kp),
        0.0 if point.i_mode.value == "OFF" else 1.0,
    )


def _model_state_payload(model, *, torch: Any) -> dict[str, Any]:
    """Serialize fitted GP parameters for the later frozen child process."""

    state: dict[str, Any] = {}
    for name, value in model.state_dict().items():
        tensor = value.detach().cpu()
        # GPyTorch constraint bounds may intentionally be +/-inf; they are
        # constructor constants, not fitted hyperparameters.  Persist only
        # finite parameters/buffers and reconstruct those constants from the
        # same typed model definition in the next child.
        if bool(torch.isfinite(tensor).all().item()):
            state[name] = tensor.tolist()
    return state


def _load_model_state(model, value: Any, *, torch: Any) -> None:
    if not isinstance(value, Mapping) or not value:
        raise OptimizerWorkerError("r006 frozen GP state is missing")
    current = model.state_dict()
    finite_names = {
        name
        for name, template in current.items()
        if bool(torch.isfinite(template).all().item())
    }
    if set(value) != finite_names:
        raise OptimizerWorkerError("r006 frozen GP state fields differ")
    converted = {}
    for name in finite_names:
        template = current[name]
        try:
            tensor = torch.as_tensor(
                value[name], dtype=template.dtype, device=template.device
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            raise OptimizerWorkerError(f"r006 frozen GP state is invalid: {name}") from exc
        if tuple(tensor.shape) != tuple(template.shape) or not bool(torch.isfinite(tensor).all().item()):
            raise OptimizerWorkerError(f"r006 frozen GP state shape/value differs: {name}")
        converted[name] = tensor
    try:
        model.load_state_dict(converted, strict=False)
    except (RuntimeError, TypeError) as exc:
        raise OptimizerWorkerError("r006 frozen GP state could not be loaded") from exc


def _deterministic_initialization(
    model: Any,
    *,
    points: Sequence[Any],
    values: Sequence[float],
    features: Any,
    torch: Any,
) -> dict[str, Any]:
    """Seed the production GP from repeats and the prescribed +/- probes.

    BoTorch still performs the real MLL fit after this step.  The initializer
    is deliberately data-derived and repeatable so the two warm-start fits
    have an auditable starting state instead of relying on library defaults.
    """

    if not points or len(points) != len(values):
        raise OptimizerWorkerError("r006 deterministic GP initializer has no observations")
    by_point: dict[tuple[Any, ...], list[float]] = {}
    for point, value in zip(points, values, strict=True):
        by_point.setdefault(tuple(point.key), []).append(float(value))
    repeat_variances = [
        statistics.pvariance(row)
        for row in by_point.values()
        if len(row) > 1
    ]
    repeat_noise_n2 = max(1.0e-4, statistics.fmean(repeat_variances) if repeat_variances else 1.0e-4)
    signal_scale_n = max(
        1.0e-4,
        statistics.pstdev(values) if len(values) > 1 else abs(float(values[0])) * 0.1,
    )
    matrix = torch.as_tensor(features, dtype=torch.double, device=model.train_inputs[0].device)
    spans = (matrix.max(dim=0).values - matrix.min(dim=0).values).clamp_min(1.0)
    # All three Matern components share the deterministic probe-derived
    # lengthscale initialization; their conditional masks remain distinct in
    # the kernel forward pass.
    base = model.covar_module.base_kernel
    for component in (base.shared, base.same_mode, base.i_on_only):
        component.initialize(lengthscale=spans.reshape(1, 1, -1))
    model.covar_module.initialize(
        outputscale=torch.tensor(signal_scale_n**2, dtype=torch.double, device=matrix.device)
    )
    return {
        "method": "repeats_plus_minus_probes",
        "signal_scale_n": float(signal_scale_n),
        "lengthscales": [float(value) for value in spans.detach().cpu().tolist()],
        "repeat_noise_n2": float(repeat_noise_n2),
        "repeat_point_count": sum(1 for row in by_point.values() if len(row) > 1),
    }


def _fit_and_ask(payload: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    import torch
    import gpytorch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.acquisition.objective import GenericMCObjective
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from gpytorch.mlls import ExactMarginalLogLikelihood
    from gpytorch.likelihoods import FixedNoiseGaussianLikelihood

    attestation = _self_attest(expected)
    artifact_binding = _artifact_binding(payload["artifact_binding"])
    operation = str(payload.get("operation", "ask"))
    choices = tuple(_point(key) for key in payload["choices"])
    pending = tuple(_point(key) for key in payload["pending"])
    if operation not in {"ask", "fit", "certificate"}:
        raise OptimizerWorkerError("r006 optimizer operation is invalid")
    if operation == "ask" and not choices:
        raise OptimizerWorkerError("qLogNEI choice set is empty")
    seed = payload["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise OptimizerWorkerError("qLogNEI seed is invalid")
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    rows = [row for row in artifact_binding["rows"] if row["receipt"].trainable]
    if len(rows) < 2:
        raise OptimizerWorkerError("fresh r006 sidecar has fewer than two trainable observations")
    if any(row.get("point_key") is None for row in rows):
        raise OptimizerWorkerError("r006 artifact row lacks a typed point key")
    train_points = tuple(_point(row["point_key"]) for row in rows)
    device = torch.device("cuda:0")
    train_x = torch.tensor([_features(point) for point in train_points], dtype=torch.double, device=device)
    train_y = torch.tensor([[float(row["receipt"].objective)] for row in rows], dtype=torch.double, device=device)
    train_yvar = torch.full_like(train_y, 1.0e-4)
    initializer_features = train_x.detach()

    class ConditionalMatern52Kernel(gpytorch.kernels.Kernel):
        has_lengthscale = True

        def __init__(self) -> None:
            super().__init__(ard_num_dims=7)
            self.shared = gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=7)
            self.same_mode = gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=7)
            self.i_on_only = gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=7)

        def forward(self, x1, x2, diag=False, **params):
            shared = self.shared(x1, x2, diag=diag, **params)
            same = self.same_mode(x1, x2, diag=diag, **params)
            ion = self.i_on_only(x1, x2, diag=diag, **params)
            if diag:
                return shared + same + ion
            x1_mode = x1[..., -1].unsqueeze(-1)
            x2_mode = x2[..., -1].unsqueeze(-2)
            mode_equal = (x1_mode == x2_mode).to(dtype=x1.dtype)
            ion_mask = (x1_mode > 0.5) & (x2_mode > 0.5)
            return shared + mode_equal * same + ion_mask.to(dtype=x1.dtype) * ion

    model = SingleTaskGP(
        train_X=train_x,
        train_Y=train_y,
        train_Yvar=train_yvar,
        likelihood=FixedNoiseGaussianLikelihood(noise=train_yvar.squeeze(-1)),
        covar_module=gpytorch.kernels.ScaleKernel(ConditionalMatern52Kernel()),
    ).to(device=device, dtype=torch.double)
    initialization = _deterministic_initialization(
        model,
        points=train_points,
        values=[float(row["receipt"].objective) for row in rows],
        features=initializer_features,
        torch=torch,
    )
    if bool(payload["hyperparameters_frozen"]):
        _load_model_state(model, payload.get("frozen_model_state"), torch=torch)
    else:
        mll = ExactMarginalLogLikelihood(model.likelihood, model)
        fit_gpytorch_mll(mll)
    model.eval()
    requested_q = int(payload.get("q", 1))
    if requested_q not in (1, 4):
        raise OptimizerWorkerError("r006 qLogNEI q must be 1 or 4")
    if operation != "ask" and requested_q != 1:
        raise OptimizerWorkerError("r006 fit/certificate operations require q=1")
    metadata = {
        "worker": "r006_managed_cuda_botorch",
        "kernel": "conditional_matern52_shared_same_mode_i_on_only",
        "train_yvar_n2": 1.0e-4,
        "qlognei": "qLogNEI",
        "x_pending": True,
        "x_pending_count": len(pending),
        "fit_group": payload["fit_group"],
        "hyperparameters_frozen": bool(payload["hyperparameters_frozen"]),
        "observation_count": len(rows),
        "q": requested_q,
        "initialization": initialization,
        "child_attestation": attestation,
    }
    if operation == "fit":
        metadata["fit_only"] = True
        return {
            "metadata": metadata,
            "model_state": _model_state_payload(model, torch=torch),
            "attestation": attestation,
        }
    if operation == "certificate":
        if not choices:
            raise OptimizerWorkerError("certificate region is empty")
        import statistics

        region_x = torch.tensor(
            [_features(point) for point in choices], dtype=torch.double, device=device
        )
        posterior = model.posterior(region_x)
        means = posterior.mean.reshape(-1)
        variances = posterior.variance.reshape(-1).clamp_min(0.0)
        z = statistics.NormalDist().inv_cdf(1.0 - 0.05 / (2.0 * len(choices)))
        incumbent_point = _point(payload["incumbent"])
        incumbent_index = next(
            (index for index, point in enumerate(choices) if point == incumbent_point),
            None,
        )
        if incumbent_index is None:
            raise OptimizerWorkerError("certificate incumbent is outside the finite region")
        incumbent_ucb = float(means[incumbent_index].item()) + z * float(torch.sqrt(variances[incumbent_index]).item())
        minimum_lcb = float(torch.min(means - z * torch.sqrt(variances)).item())
        metadata["certificate"] = {
            "incumbent_ucb_n": incumbent_ucb,
            "minimum_lcb_n": minimum_lcb,
            "epsilon_n": float(payload["epsilon_n"]),
            "region_size": len(choices),
            "simultaneous_z95": z,
            "global_convergence_claim": False,
        }
        return {"metadata": metadata, "attestation": attestation}
    baseline = train_x
    pending_x = torch.tensor([_features(point) for point in pending], dtype=torch.double, device=device) if pending else None
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=baseline,
        X_pending=pending_x,
        objective=objective,
        prune_baseline=False,
    )
    scored: list[tuple[float, tuple[Any, ...], tuple[Any, ...]]] = []
    with torch.no_grad():
        for batch in itertools.combinations(choices, requested_q):
            batch_x = torch.tensor(
                [[_features(point) for point in batch]],
                dtype=torch.double,
                device=device,
            )
            value = acquisition(batch_x)
            scored.append(
                (
                    float(value.detach().cpu().item()),
                    tuple(item.key for item in batch),
                    batch,
                )
            )
    if not scored:
        raise OptimizerWorkerError("r006 qLogNEI has no finite candidate batch")
    selected_batch = max(
        scored,
        key=lambda row: (row[0], tuple(str(value) for value in row[1])),
    )[2]
    selected = selected_batch[0]
    return {
        "selected_point_key": list(selected.key),
        "selected_point_keys": [list(point.key) for point in selected_batch],
        "metadata": {
            **metadata,
        },
        "attestation": attestation,
    }


def _request(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    required = {"schema", "profile", "expected_attestation", "payload"}
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema") != REQUEST_SCHEMA or value.get("profile") != "optimizer":
        raise OptimizerWorkerError("r006 optimizer request envelope differs")
    expected = _expected_environment(value["expected_attestation"])
    payload = value["payload"]
    fields = {"artifact_binding", "choices", "pending", "incumbent", "seed", "fit_group", "hyperparameters_frozen"}
    optional = {"operation", "epsilon_n", "q", "frozen_model_state"}
    if not isinstance(payload, Mapping) or not fields.issubset(set(payload)) or set(payload) - fields - optional:
        raise OptimizerWorkerError("r006 optimizer request fields differ")
    if not isinstance(payload["choices"], list) or not isinstance(payload["pending"], list) or not isinstance(payload["incumbent"], list):
        raise OptimizerWorkerError("r006 optimizer point catalog is not typed")
    if payload["fit_group"] not in (1, 2, "frozen") or not isinstance(payload["hyperparameters_frozen"], bool):
        raise OptimizerWorkerError("r006 fit/freeze state is invalid")
    if "q" in payload and payload["q"] not in (1, 4):
        raise OptimizerWorkerError("r006 qLogNEI q is invalid")
    operation = str(payload.get("operation", "ask"))
    if operation not in {"ask", "fit", "certificate"}:
        raise OptimizerWorkerError("r006 optimizer operation is invalid")
    if operation == "certificate":
        if (
            "epsilon_n" not in payload
            or isinstance(payload["epsilon_n"], bool)
            or not isinstance(payload["epsilon_n"], (int, float))
            or not math.isfinite(float(payload["epsilon_n"]))
            or float(payload["epsilon_n"]) <= 0.0
        ):
            raise OptimizerWorkerError("r006 certificate epsilon is missing")
    elif "epsilon_n" in payload:
        raise OptimizerWorkerError("r006 epsilon is only valid for certificate requests")
    # Validate only the shape here; the child reads every objective from the
    # immutable sidecar immediately before model construction.
    _artifact_binding(payload["artifact_binding"])
    return expected, dict(payload)


def run(request: Mapping[str, Any]) -> dict[str, Any]:
    expected, payload = _request(request)
    return _fit_and_ask(payload, expected)


def main() -> int:
    encoded = sys.stdin.buffer.read()
    try:
        request = json.loads(encoded.decode("utf-8"))
        result = run(request)
        attestation = result.pop("attestation")
        body = {
            "schema": RESPONSE_SCHEMA,
            "ok": True,
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "attestation": attestation,
            "result": result,
        }
        sys.stdout.write(json.dumps(body, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"schema": RESPONSE_SCHEMA, "ok": False, "reason_code": "OPTIMIZER_WORKER_FAILED", "detail": f"{type(exc).__name__}:{exc}"}, sort_keys=True), file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
