#!/usr/bin/env python3
"""Build the canonical TacDiffusion formal V4 offline source closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT_ROOT = REPOSITORY_ROOT / "experiments" / "tase-contact-reproduction"
CLOSURE_PATH = EXPERIMENT_ROOT / "config" / "tacdiffusion_formal_v4_offline_closure.json"
CURRENT_PATH = EXPERIMENT_ROOT / "config" / "tacdiffusion_formal_v4_current_stage.json"
SCHEMA = "ur10e_tacdiffusion_formal_source_closure/v1"

PINNED_PATHS = (
    "experiments/tase-contact-reproduction/STEP5D_TACDIFFUSION_FORMAL_V4_FLOW.md",
    "experiments/tase-contact-reproduction/config/step5d_tacdiffusion_sensor_frame_v4.json",
    "experiments/tase-contact-reproduction/evidence/step5d_tacdiffusion/sensor_frame_calibration_v4_20260808.json",
    "experiments/tase-contact-reproduction/tests/test_run_tacdiffusion_formal_v4.py",
    "experiments/tase-contact-reproduction/tests/test_run_tacdiffusion_remote_direct_torque_v4.py",
    "experiments/tase-contact-reproduction/tools/run_tacdiffusion_formal_v4.py",
    "experiments/tase-contact-reproduction/tools/step5d_v34_transport_primitives.py",
    "experiments/tase-contact-reproduction/tools/build_tacdiffusion_formal_v4_numeric_sanity.py",
    "experiments/tase-contact-reproduction/tools/run_tacdiffusion_remote_direct_torque_v4.py",
    "experiments/tase-contact-reproduction/config/tacdiffusion_formal_v4_numeric_sanity.json",
    "experiments/tase-contact-reproduction/tests/test_formal_contact_acquisition.py",
    "experiments/ur10e-variable-impedance/config/tacdiffusion_formal_v4_campaign_contract.json",
    "experiments/ur10e-variable-impedance/config/tacdiffusion_formal_v4_contract.json",
    "experiments/ur10e-variable-impedance/config/tacdiffusion_formal_v4_equipment_contract.json",
    "experiments/ur10e-variable-impedance/config/tacdiffusion_formal_v4_review_governance.json",
    "experiments/ur10e-variable-impedance/tests/test_direct_torque_live_v4.py",
    "experiments/ur10e-variable-impedance/tests/test_tacdiffusion_core.py",
    "experiments/ur10e-variable-impedance/tests/test_tacdiffusion_episode_composition.py",
    "experiments/ur10e-variable-impedance/tests/test_tacdiffusion_formal_orchestration.py",
    "experiments/ur10e-variable-impedance/tests/test_tacdiffusion_formal_v4.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/__init__.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/action.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/contracts.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/direct_torque_live_v4.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/eligibility.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/episode_recorder.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/episode_composition.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/expert.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_artifacts.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_benchmark.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_campaign.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_dataset.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_dynamics.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_episode.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_equipment.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_identity.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_model.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_orchestration.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_contact_acquisition.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_source.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_timing.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/formal_trajectory.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/governance.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/signals.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _atomic_replace(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(dict(payload), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def build() -> dict[str, object]:
    paths = tuple(sorted(PINNED_PATHS))
    if len(paths) != len(set(paths)):
        raise ValueError("formal closure path list contains duplicates")
    files: list[dict[str, str]] = []
    for relative in paths:
        path = REPOSITORY_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"formal closure source is missing: {relative}")
        files.append({"path": relative, "sha256": _sha256(path)})
    content_sha = _canonical_sha256({"files": files})
    closure: dict[str, object] = {
        "schema_version": SCHEMA,
        "lineage": "tacdiffusion_formal_v4",
        "files": files,
        "content_address": {"algorithm": "sha256", "sha256": content_sha},
        "route": "remote_secondary_client_direct_torque",
        "controller_readback_applicable": False,
        "kunwei_only": True,
        "ur_internal_ft_used": False,
        "model_active": False,
        "shadow_only": True,
    }
    _atomic_replace(CLOSURE_PATH, closure)
    current = json.loads(CURRENT_PATH.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        raise ValueError("formal current-stage source is not an object")
    current["source_closure_file_sha256"] = _sha256(CLOSURE_PATH)
    current["source_content_sha256"] = content_sha
    _atomic_replace(CURRENT_PATH, current)
    return {
        "ok": True,
        "closure": str(CLOSURE_PATH),
        "closure_file_sha256": _sha256(CLOSURE_PATH),
        "source_content_sha256": content_sha,
        "file_count": len(files),
        "current_stage": str(CURRENT_PATH),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(build(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
