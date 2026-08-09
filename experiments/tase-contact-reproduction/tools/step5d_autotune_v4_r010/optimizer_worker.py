"""R010 worker envelope: R008 qLogNEI plus immutable calibration binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import step5d_autotune_v4_r006.optimizer_worker as _r006_worker
from step5d_autotune_v4_r006.optimizer_worker import OptimizerWorkerError
from step5d_autotune_v4_r008 import optimizer_worker_batched as _batched

from .gp_calibration import load_calibration_artifact, sha256_file


REQUEST_SCHEMA = "step5d.autotune-v4/r010-optimizer-request-v1"
RESPONSE_SCHEMA = "step5d.autotune-v4/r010-optimizer-response-v1"


def _calibration_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "file_sha256", "calibration_sha256"}:
        raise OptimizerWorkerError("R010 calibration binding fields differ")
    path = Path(value["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise OptimizerWorkerError("R010 calibration path must be a canonical regular absolute file")
    if sha256_file(path) != value["file_sha256"]:
        raise OptimizerWorkerError("R010 calibration file bytes differ")
    artifact = load_calibration_artifact(path)
    if artifact["calibration_sha256"] != value["calibration_sha256"]:
        raise OptimizerWorkerError("R010 calibration semantic digest differs")
    return artifact


def run(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping) or set(request) != {"schema", "profile", "expected_attestation", "payload"}:
        raise OptimizerWorkerError("R010 optimizer request fields differ")
    if request.get("schema") != REQUEST_SCHEMA or request.get("profile") != "optimizer":
        raise OptimizerWorkerError("R010 optimizer request schema differs")
    payload = request.get("payload")
    if not isinstance(payload, Mapping) or set(payload) != {"calibration", "r008_payload"}:
        raise OptimizerWorkerError("R010 optimizer payload fields differ")
    calibration = _calibration_binding(payload["calibration"])
    base_request = {
        "schema": _r006_worker.REQUEST_SCHEMA,
        "profile": "optimizer",
        "expected_attestation": request["expected_attestation"],
        "payload": payload["r008_payload"],
    }
    expected, r008_payload = _r006_worker._request(base_request)
    response = _batched._fit_and_ask(
        r008_payload,
        expected,
        r010_calibration=calibration,
    )
    calibration_attestation = {
        "calibration_sha256": calibration["calibration_sha256"],
        "noise_floor_n2": calibration["noise"]["selected_floor_n2"],
        "variance_estimator": calibration["noise"]["variance_estimator"],
        "lengthscale_policy": calibration["lengthscales"]["policy"],
        "kernel_implementation_sha256": calibration["kernel"]["implementation_sha256"],
    }
    child = response.get("attestation")
    if not isinstance(child, Mapping):
        raise OptimizerWorkerError("R010 child response lacks environment attestation")
    response["attestation"] = {
        "environment": dict(child),
        "calibration": calibration_attestation,
    }
    return {"schema": RESPONSE_SCHEMA, "payload": response}


__all__ = ["REQUEST_SCHEMA", "RESPONSE_SCHEMA", "run"]
