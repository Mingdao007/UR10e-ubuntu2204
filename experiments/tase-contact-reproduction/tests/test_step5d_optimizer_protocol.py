from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_autotune_r008_policy import PlannedOccurrence  # noqa: E402
from step5d_autotune_v3 import optimizer_protocol as protocol  # noqa: E402
from step5d_autotune_v3 import optimizer_payloads as payloads  # noqa: E402


def _pointer() -> dict[str, object]:
    return {
        "bundle_id": "a" * 64,
        "attestation_sha256": "b" * 64,
        "profiles": {
            profile: {
                "root": f"/governed/{profile}",
                "python_executable": f"/governed/{profile}/bin/python",
                "environment_id": ("c" if profile == "control" else "d") * 64,
                "record_tree_sha256": ("e" if profile == "control" else "f")
                * 64,
                "profile_tree_sha256": ("1" if profile == "control" else "2")
                * 64,
            }
            for profile in ("control", "optimizer")
        },
    }


def _deployment():
    return protocol.deployment_certificate(
        runtime_pointer=_pointer(),
        gpu_attestation_digest="3" * 64,
    )


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
) -> dict[str, object]:
    sequence = request["sequence"]
    assert isinstance(sequence, int)
    catalog = tuple(
        ForceCandidate.from_payload(value) for value in request["catalog"]
    )
    evidence = {
        "seed": protocol.MODE_SEEDS[request["mode"]],
        "optimizer": {"device": "cuda:0"},
    }
    return {
        "schema": protocol.RESPONSE_SCHEMA,
        "ok": True,
        "request_sha256": hashlib.sha256(
            protocol.canonical_bytes(request)
        ).hexdigest(),
        "suggestions": payloads.encode_suggestions(
            _occurrences(sequence),
            catalog=catalog,
            identity=request["optimizer_identity"],
            seed=request["seed"],
            history_digest=request["accepted_history_digest"],
            evidence=evidence,
        ),
        "evidence": evidence,
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
        response = _response(request)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=protocol.canonical_bytes(response),
            stderr=b"",
        )

    monkeypatch.setenv("PYTHONPATH", "/caller/private-sidecar")
    monkeypatch.setenv("CONDA_PREFIX", "/caller/conda")
    monkeypatch.setattr(protocol.subprocess, "run", fake_run)
    client = protocol.ExactOptimizerClient(
        deployment=_deployment(),
        runtime_pointer=_pointer(),
        pointer_loader=_pointer,
        persistent=False,
    )

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
    assert evidence["optimizer_deployment_certificate_digest"] == _deployment().digest
    request = observed["request"]
    assert isinstance(request, dict)
    assert "runtime" not in request
    assert "gpu" not in request


@pytest.mark.parametrize(
    "tamper",
    [
        "request_sha",
        "forbidden_attestation",
        "suggestion_id",
        "row_index",
        "parameter_bounds",
        "seed",
    ],
)
def test_optimizer_response_tamper_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    def fake_run(command, **kwargs):
        request = json.loads(kwargs["input"])
        response = _response(request)
        if tamper == "request_sha":
            response["request_sha256"] = "0" * 64
        elif tamper == "forbidden_attestation":
            response["runtime"] = {"attestation_sha256": "0" * 64}
        elif tamper == "suggestion_id":
            response["suggestions"][0]["suggestion_id"] = "0" * 64
        elif tamper == "row_index":
            response["suggestions"][1]["constraints"]["row_index"] = 1
        elif tamper == "parameter_bounds":
            response["suggestions"][0]["parameters"]["force_p_gain"]["bounds"][
                "upper"
            ] = 1.0
        else:
            response["evidence"]["seed"] = 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=protocol.canonical_bytes(response),
            stderr=b"",
        )

    monkeypatch.setattr(protocol.subprocess, "run", fake_run)
    client = protocol.ExactOptimizerClient(
        deployment=_deployment(),
        runtime_pointer=_pointer(),
        pointer_loader=_pointer,
        persistent=False,
    )
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
    client = protocol.ExactOptimizerClient(
        deployment=_deployment(),
        runtime_pointer=_pointer(),
        pointer_loader=_pointer,
        persistent=False,
    )
    with pytest.raises(protocol.OptimizerProtocolError, match="worker failed"):
        client.propose(
            mode="rolling_batch_a",
            observations=(),
            catalog=tuple(value.candidate for value in _occurrences(3)),
            sequence=3,
        )


def test_optimizer_pointer_rotation_after_admission_fails_before_child_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False

    def fake_run(command, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("optimizer child must not start after pointer rotation")

    rotated = _pointer()
    rotated["bundle_id"] = "9" * 64
    monkeypatch.setattr(protocol.subprocess, "run", fake_run)
    client = protocol.ExactOptimizerClient(
        deployment=_deployment(),
        runtime_pointer=_pointer(),
        pointer_loader=lambda: rotated,
        persistent=False,
    )

    with pytest.raises(
        protocol.OptimizerProtocolError,
        match="changed after deployment admission",
    ):
        client.propose(
            mode="rolling_batch_a",
            observations=(),
            catalog=tuple(value.candidate for value in _occurrences(3)),
            sequence=3,
        )
    assert started is False


def test_persistent_optimizer_reuses_one_framed_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[list[str]] = []

    class Sink:
        def __init__(self, worker: "FakeWorker") -> None:
            self.worker = worker
            self.buffer = bytearray()

        def write(self, encoded: bytes) -> int:
            self.buffer.extend(encoded)
            return len(encoded)

        def flush(self) -> None:
            size = struct.unpack(">I", self.buffer[:4])[0]
            request = json.loads(bytes(self.buffer[4 : 4 + size]))
            del self.buffer[: 4 + size]
            response = protocol.canonical_bytes(_response(request))
            os.write(
                self.worker.write_fd,
                struct.pack(">I", len(response)) + response,
            )

        def close(self) -> None:
            if self.worker.write_fd >= 0:
                os.close(self.worker.write_fd)
                self.worker.write_fd = -1

    class FakeWorker:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            starts.append(command)
            read_fd, self.write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.stderr = io.BytesIO()
            self.stdin = Sink(self)
            self.pid = 4321
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(protocol.subprocess, "Popen", FakeWorker)
    client = protocol.ExactOptimizerClient(
        deployment=_deployment(),
        runtime_pointer=_pointer(),
        pointer_loader=_pointer,
    )
    try:
        for sequence in (3, 4):
            rows, _evidence = client.propose(
                mode="rolling_batch_a",
                observations=(),
                catalog=tuple(value.candidate for value in _occurrences(sequence)),
                sequence=sequence,
            )
            assert len(rows) == 5
        assert len(starts) == 1
        assert starts[0][-1] == "--serve"
        assert client._worker is not None and client._worker.pid == 4321
    finally:
        client.close()


@pytest.mark.parametrize(
    "encoded",
    [b'{"a":1,"a":2}\n', b'{"value":NaN}\n'],
)
def test_optimizer_protocol_rejects_non_strict_json(encoded: bytes) -> None:
    with pytest.raises(protocol.OptimizerProtocolError):
        protocol.strict_json(encoded, "test payload")
