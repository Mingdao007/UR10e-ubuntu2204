from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_autotune_r008_policy import PlannedOccurrence  # noqa: E402
from step5d_autotune_v3 import optimizer_protocol as protocol  # noqa: E402


def _pointer() -> dict[str, object]:
    return {
        "bundle_id": "a" * 64,
        "attestation_sha256": "b" * 64,
        "profiles": {
            profile: {
                "root": f"/governed/{profile}",
                "python_executable": f"/governed/{profile}/bin/python",
                "environment_id": ("c" if profile == "control" else "d") * 64,
            }
            for profile in ("control", "optimizer")
        },
    }


def _occurrences(sequence: int) -> tuple[PlannedOccurrence, ...]:
    candidates = (
        ForceCandidate.from_log2(p=-0.50, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.0, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.50, damping=0.0, i=0.0),
    )
    return tuple(
        PlannedOccurrence(
            logical_batch_sequence=sequence,
            row_index=index,
            candidate=candidate,
            plan_revision=sequence,
            selection_role="qlognei_a",
            replicate_ordinal=1,
        )
        for index, candidate in enumerate(candidates, start=1)
    )


def _response(
    request: dict[str, object],
    *,
    environment: dict[str, str],
) -> dict[str, object]:
    sequence = request["sequence"]
    assert isinstance(sequence, int)
    return {
        "schema": protocol.RESPONSE_SCHEMA,
        "ok": True,
        "request_sha256": hashlib.sha256(
            protocol.canonical_bytes(request)
        ).hexdigest(),
        "runtime": request["runtime"],
        "gpu": {
            "visible_devices": environment["CUDA_VISIBLE_DEVICES"],
            "mapped_device": "cuda:0",
            "name": "governed-test-gpu",
            "torch_version": "2.11.0+cu128",
            "botorch_version": "0.16.1",
            "gpytorch_version": "1.15.2",
            "torch_cuda_version": "12.8",
        },
        "occurrences": [
            protocol.occurrence_payload(value) for value in _occurrences(sequence)
        ],
        "evidence": {
            "seed": protocol.MODE_SEEDS[request["mode"]],
            "optimizer": {"device": "cuda:0"},
        },
        "module_closure": {
            "sha256": "e" * 64,
            "module_file_count": 42,
            "violations": [],
        },
    }


def test_exact_optimizer_client_uses_bound_interpreter_and_sanitized_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        request = json.loads(kwargs["input"])
        observed.update(
            command=command,
            environment=kwargs["env"],
            request=request,
        )
        response = _response(request, environment=kwargs["env"])
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=protocol.canonical_bytes(response),
            stderr=b"",
        )

    monkeypatch.setenv("PYTHONPATH", "/caller/private-sidecar")
    monkeypatch.setenv("CONDA_PREFIX", "/caller/conda")
    monkeypatch.setattr(protocol.subprocess, "run", fake_run)
    client = protocol.ExactOptimizerClient(runtime_pointer=_pointer())

    rows, evidence = client.propose(
        mode="rolling_batch_a",
        observations=(),
        catalog=tuple(value.candidate for value in _occurrences(3)),
        sequence=3,
    )

    assert observed["command"] == [
        "/governed/optimizer/bin/python",
        "-B",
        "-m",
        protocol.WORKER_MODULE,
    ]
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["STEP5D_V3_RUNTIME_PROFILE"] == "optimizer"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert "/caller/private-sidecar" not in environment["PYTHONPATH"]
    assert "CONDA_PREFIX" not in environment
    assert tuple(row.row_index for row in rows) == (1, 2, 3, 4, 5)
    assert evidence["worker_gpu"]["mapped_device"] == "cuda:0"


@pytest.mark.parametrize(
    "tamper",
    ["request_sha", "gpu", "row_index", "module_closure", "seed"],
)
def test_optimizer_response_tamper_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    def fake_run(command, **kwargs):
        request = json.loads(kwargs["input"])
        response = _response(request, environment=kwargs["env"])
        if tamper == "request_sha":
            response["request_sha256"] = "0" * 64
        elif tamper == "gpu":
            response["gpu"]["mapped_device"] = "cpu"
        elif tamper == "row_index":
            response["occurrences"][1]["row_index"] = 1
        elif tamper == "module_closure":
            response["module_closure"]["violations"] = ["forbidden_module:cupy"]
        else:
            response["evidence"]["seed"] = 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=protocol.canonical_bytes(response),
            stderr=b"",
        )

    monkeypatch.setattr(protocol.subprocess, "run", fake_run)
    client = protocol.ExactOptimizerClient(runtime_pointer=_pointer())
    with pytest.raises(protocol.OptimizerProtocolError):
        client.propose(
            mode="rolling_batch_a",
            observations=(),
            catalog=tuple(value.candidate for value in _occurrences(3)),
            sequence=3,
        )


def test_optimizer_worker_failure_does_not_return_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        protocol.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            78,
            stdout=b"",
            stderr=b'{"reason_code":"OPTIMIZER_WORKER_FAILED"}\n',
        ),
    )
    client = protocol.ExactOptimizerClient(runtime_pointer=_pointer())
    with pytest.raises(protocol.OptimizerProtocolError, match="worker failed"):
        client.propose(
            mode="rolling_batch_a",
            observations=(),
            catalog=tuple(value.candidate for value in _occurrences(3)),
            sequence=3,
        )


@pytest.mark.parametrize(
    "encoded",
    [b'{"a":1,"a":2}\n', b'{"value":NaN}\n'],
)
def test_optimizer_protocol_rejects_non_strict_json(encoded: bytes) -> None:
    with pytest.raises(protocol.OptimizerProtocolError):
        protocol.strict_json(encoded, "test payload")
