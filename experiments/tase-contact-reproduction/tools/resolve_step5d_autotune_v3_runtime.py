#!/usr/bin/python3.10
"""Read-only bootstrap resolver for exact governed Step5d interpreters."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5d_autotune_v3.runtime_installation import (
    PROFILES,
    RuntimeInstallationError,
    load_runtime_pointer,
    load_runtime_pointer_identity,
    runtime_status,
)
from step5d_autotune_v3.runtime_environment import production_runtime_environment


_NEXT_ACTION = {
    "RUNTIME_NOT_PROVISIONED": "provision_runtime",
    "RUNTIME_LOCK_MISMATCH": "provision_runtime_for_current_lock",
    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH": "reprovision_runtime",
    "CONTROL_RUNTIME_INVALID": "reprovision_control_runtime",
    "OPTIMIZER_RUNTIME_INVALID": "reprovision_optimizer_runtime",
    "HOST_CONTRACT_MISMATCH": "restore_host_contract",
    "GPU_IDENTITY_MISMATCH": "restore_governed_gpu_identity",
    "OWNER_DEPENDENCY_MISMATCH": "restore_owner_dependency",
}


def _blocked_status(environment: dict[str, object]) -> dict[str, object]:
    reason = str(environment.get("reason_code") or "RUNTIME_NOT_PROVISIONED")
    environment.update(
        {
            "gpu_identity_ready": False,
            "gpu_functional_proven": False,
            "environment_attestation_sha256": None,
            "blocker": {
                "reason_code": reason,
                "detail": environment.get("detail"),
            },
        }
    )
    return {
        "schema": "step5d.autotune-v3/governed-status-v1",
        "generated_at_unix_ns": time.time_ns(),
        "transition_actor": "launcher_supervisor",
        "release": {
            "sha256": None,
            "manifest_path": None,
            "program_id": None,
            "release_stage_id": None,
            "valid": False,
            "error": "runtime gate precedes release resolution",
        },
        "state": None,
        "predicates": {
            "release_contract_proven": False,
            "bench_ready": False,
            "play_prompt_ready": False,
        },
        "environment": environment,
        "bridge": {"pid": None, "heartbeat_at_unix_ns": None},
        "controller": {},
        "campaign_lease": {},
        "attestation": {},
        "terminal": {"completed": False},
        "blocker": {
            "class": "INTERNAL",
            "reason_codes": [reason],
            "evidence": [
                {
                    "role": "runtime_environment",
                    "path": None,
                    "sha256": None,
                    "detail": environment.get("detail"),
                }
            ],
        },
        "outcome": {"live_proven": False, "trial_id": None, "evidence": None},
        "next_action": _NEXT_ACTION.get(reason, "restore_runtime_environment"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--shell-binding", action="store_true")
    parser.add_argument("--status-json", action="store_true")
    args = parser.parse_args(argv)
    if sum((args.profile is not None, args.json, args.shell_binding, args.status_json)) > 1:
        parser.error("resolver output modes are mutually exclusive")
    if args.status_json:
        status = runtime_status()
        if status.get("reason_code") is not None:
            print(json.dumps(_blocked_status(status), sort_keys=True))
        else:
            print(json.dumps({"ok": True, **status}, sort_keys=True))
        return 0
    pointer_loader = (
        load_runtime_pointer_identity if args.shell_binding else load_runtime_pointer
    )
    try:
        pointer = pointer_loader()
    except RuntimeInstallationError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": "step5d.autotune-v3/runtime-resolution-v1",
                        "ok": False,
                        "reason_code": exc.reason_code,
                        "detail": exc.detail,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"{exc.reason_code}: {exc.detail}")
        return 2
    if args.shell_binding:
        from step5d_autotune_v3.runtime_installation import load_runtime_contract

        contract = load_runtime_contract()
        control_environment = production_runtime_environment(
            os.environ,
            profile="control",
            runtime_pointer=pointer,
        )
        fields = (
            pointer["profiles"]["control"]["python_executable"],
            pointer["profiles"]["optimizer"]["python_executable"],
            pointer["bundle_id"],
            pointer["attestation_sha256"],
            pointer["contract_sha256"],
            pointer["lock_sha256"],
            pointer["profiles"]["control"]["environment_id"],
            pointer["profiles"]["optimizer"]["environment_id"],
            contract["gpu"]["uuid"],
            control_environment["LD_LIBRARY_PATH"],
            control_environment["CUPY_CACHE_DIR"],
        )
        if any(not isinstance(value, str) or not value or any(c in value for c in "\t\r\n") for value in fields):
            print("runtime binding contains unsafe control characters", file=sys.stderr)
            return 2
        print("\t".join(fields))
        return 0
    if args.profile is not None and not args.json:
        print(pointer["profiles"][args.profile]["python_executable"])
        return 0
    print(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/runtime-resolution-v1",
                "ok": True,
                **pointer,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
