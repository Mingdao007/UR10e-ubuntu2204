#!/usr/bin/python3.10
"""Run one networkless Step5d OCI slice and issue hermetic-only evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


BASE_IMAGE = (
    "ros:humble-ros-base-jammy@sha256:"
    "5c793b92e0b12d6babb438cb20eed7766495fde6419a21e3d2e918464f09dc17"
)
AUTHORITY = "HERMETIC_CI_PROVEN"
SCHEMA = "step5d.autotune-v3/oci-cleanroom-attestation-v1"
EXPERIMENT_ROOT = Path("/workspace/experiments/tase-contact-reproduction")
LOCK_PATH = EXPERIMENT_ROOT / "uv.lock"


class OciGateError(RuntimeError):
    """The clean-room invocation or functional slice is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_container_contract() -> None:
    if os.environ.get("STEP5D_OCI_NETWORK_MODE") != "none":
        raise OciGateError("OCI runtime must declare network none")
    if os.environ.get("STEP5D_OCI_REPOSITORY_MODE") != "read_only":
        raise OciGateError("OCI repository mount must be read-only")
    if not LOCK_PATH.is_file() or LOCK_PATH.is_symlink():
        raise OciGateError("mounted uv.lock is missing or unsafe")
    Path("/tmp/step5d-home").mkdir(parents=True, exist_ok=True, mode=0o700)


def _run(
    command: Sequence[str],
    *,
    log: Path,
    extra_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("NVIDIA_")
        or key
        in {
            "CUDA_VISIBLE_DEVICES",
            "HOME",
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "PATH",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONNOUSERSITE",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        }
    }
    environment.update(
        {
            "HOME": "/tmp/step5d-home",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    if extra_environment is not None:
        environment.update(extra_environment)
    log.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        list(command),
        cwd=EXPERIMENT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=600,
    )
    log.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise OciGateError(
            f"clean-room child failed ({completed.returncode}): {' '.join(command)}"
        )
    return {
        "command": list(command),
        "log": str(log),
        "log_sha256": _sha256(log),
        "returncode": completed.returncode,
    }


def _cpu_slice(output_root: Path) -> dict[str, Any]:
    return _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_step5d_v3_source_closure.py",
            "tests/test_step5d_autotune_v3_qualification_endpoints.py::"
            "test_rtde_repository_client_rejects_unknown_recipe_field",
        ],
        log=output_root / "cpu-slice.log",
    )


def _cuda_slice(output_root: Path) -> dict[str, Any]:
    probes = {
        "control": (
            Path("/opt/step5d-control/.venv/bin/python"),
            "import cupy as c,json; x=c.arange(1024,dtype=c.float32); "
            "c.cuda.Stream.null.synchronize(); print(json.dumps({"
            "'backend':'cupy','device_count':c.cuda.runtime.getDeviceCount(),"
            "'sum':float(c.asnumpy(x.sum()))}))",
        ),
        "optimizer": (
            Path("/opt/step5d-optimizer/.venv/bin/python"),
            "import botorch,json,torch; assert torch.cuda.is_available(); "
            "x=torch.arange(1024,device='cuda',dtype=torch.float32); "
            "torch.cuda.synchronize(); print(json.dumps({"
            "'backend':'torch+botorch','device_count':torch.cuda.device_count(),"
            "'sum':float(x.sum().cpu())}))",
        ),
    }
    results: dict[str, Any] = {}
    for profile, (python, source) in probes.items():
        if not python.is_file():
            raise OciGateError(f"{profile} clean-room interpreter is missing")
        nvidia_root = (
            python.parent.parent
            / "lib"
            / "python3.10"
            / "site-packages"
            / "nvidia"
        )
        library_dirs = sorted(
            str(candidate)
            for candidate in nvidia_root.glob("*/lib")
            if candidate.is_dir()
        )
        if not library_dirs:
            raise OciGateError(f"{profile} NVIDIA wheel libraries are missing")
        inherited_library_path = os.environ.get("LD_LIBRARY_PATH", "")
        if inherited_library_path:
            library_dirs.append(inherited_library_path)
        results[profile] = _run(
            [str(python), "-I", "-c", source],
            log=output_root / f"cuda-{profile}.log",
            extra_environment={"LD_LIBRARY_PATH": os.pathsep.join(library_dirs)},
        )
    return results


def run(slice_name: str, output: Path) -> dict[str, Any]:
    _require_container_contract()
    output_root = output.parent
    output_root.mkdir(parents=True, exist_ok=True)
    started = time.time_ns()
    result = (
        _cpu_slice(output_root)
        if slice_name == "cpu"
        else _cuda_slice(output_root)
    )
    payload = {
        "schema": SCHEMA,
        "authority": AUTHORITY,
        "offline_proven": False,
        "slice": slice_name,
        "base_image": BASE_IMAGE,
        "uv_version": "0.9.30",
        "lock_sha256": _sha256(LOCK_PATH),
        "network_mode": "none",
        "repository_mode": "read_only",
        "started_at_unix_ns": started,
        "completed_at_unix_ns": time.time_ns(),
        "result": result,
        "ok": True,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, output)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slice", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = run(args.slice, args.output)
    except (OciGateError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "blocker": str(exc)}))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
