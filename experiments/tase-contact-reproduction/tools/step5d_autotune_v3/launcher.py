"""Check-only production launcher contract for frozen-v1 Step5d autotune.

The module intentionally has no spawn/exec function.  Its only job is to build
the governed argv, round-trip it through ``kunwei_rtde_bridge.parse_args``, and
return a machine-readable attestation before another owner may start anything.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest.mock import patch

from .profile import (
    CATEGORIES,
    DEFAULT_CONTRACT_PATH,
    EXPERIMENT_ROOT,
    ContractViolation,
    contract_sha256,
    control_fingerprint,
    expected_effective_config,
    load_contract,
    normalize_candidate,
    resolve_expected_value,
    runtime_values,
    validate_source_bindings,
)


CHECK_SCHEMA = "step5d.autotune.v3.launch-check/v1"
DEFAULT_CHECK_RUNTIME_ROOT = Path("/tmp/step5d-autotune-v3-check")


def _cli_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ContractViolation(f"CLI placeholder resolved to unsupported value {value!r}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ContractViolation("CLI numeric value must be finite")
    text = format(Decimal(str(numeric)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _contract_cli_tokens(
    contract: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    runtime: Mapping[str, str],
) -> list[str]:
    tokens: list[str] = []
    for row in contract["cli_arguments"]:
        tokens.append(row[0])
        if len(row) == 2:
            resolved = resolve_expected_value(
                row[1],
                candidate=candidate,
                runtime=runtime,
            )
            tokens.append(_cli_text(resolved))
    return tokens


def build_bridge_argv(
    runtime_root: Path,
    candidate: Mapping[str, Any] | None = None,
    *,
    contract: Mapping[str, Any] | None = None,
    experiment_root: Path = EXPERIMENT_ROOT,
    launch_profile: Any | None = None,
    trial_overlay: Mapping[str, Any] | None = None,
) -> list[str]:
    """Build, but never execute, the frozen production bridge command."""

    payload = dict(contract or load_contract())
    candidate_values = normalize_candidate(candidate)
    runtime = runtime_values(runtime_root)
    bridge = experiment_root / "tools/kunwei_rtde_bridge.py"
    if bridge.is_symlink() or not bridge.is_file():
        raise ContractViolation(f"production bridge entrypoint is unavailable: {bridge}")
    argv = [
        sys.executable,
        str(bridge.resolve()),
        *_contract_cli_tokens(
            payload,
            candidate=candidate_values,
            runtime=runtime,
        ),
    ]
    if launch_profile is not None:
        from .runtime_profile import apply_profile_to_argv

        argv = apply_profile_to_argv(
            argv,
            profile=launch_profile,
            overlay=trial_overlay,
        )
    elif trial_overlay is not None:
        raise ContractViolation("trial overlay requires an explicit launch profile")
    return argv


def _flag_shapes(contract: Mapping[str, Any]) -> dict[str, int]:
    return {row[0]: len(row) - 1 for row in contract["cli_arguments"]}


def _scan_cli(
    argv: Sequence[str],
    contract: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    if len(argv) < 2 or any(not isinstance(token, str) for token in argv):
        raise ContractViolation("launcher argv must contain string executable and entrypoint")
    shapes = _flag_shapes(contract)
    forbidden = set(contract["forbidden_cli_flags"])
    observed: dict[str, tuple[str, ...]] = {}
    index = 2
    while index < len(argv):
        flag = argv[index]
        if flag in forbidden:
            raise ContractViolation(f"launcher contains forbidden flag {flag}")
        if flag not in shapes:
            raise ContractViolation(f"launcher contains unknown or unclassified flag {flag}")
        if flag in observed:
            raise ContractViolation(f"launcher repeats flag {flag}")
        arity = shapes[flag]
        stop = index + 1 + arity
        if stop > len(argv):
            raise ContractViolation(f"launcher flag {flag} is missing its value")
        values = tuple(argv[index + 1 : stop])
        if any(value.startswith("--") for value in values):
            raise ContractViolation(f"launcher flag {flag} fell back after a missing value")
        observed[flag] = values
        index = stop
    missing = sorted(set(shapes) - set(observed))
    if missing:
        raise ContractViolation(f"launcher is missing required flags: {missing}")
    return observed


def validate_raw_argv(
    argv: Sequence[str],
    *,
    expected: Sequence[str],
    contract: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    observed = _scan_cli(argv, contract)
    expected_rows = _scan_cli(expected, contract)
    if argv[0] != expected[0]:
        raise ContractViolation(
            f"launcher executable differs: expected={expected[0]!r} observed={argv[0]!r}"
        )
    if Path(argv[1]).resolve(strict=False) != Path(expected[1]).resolve(strict=False):
        raise ContractViolation(
            f"launcher entrypoint differs: expected={expected[1]!r} observed={argv[1]!r}"
        )
    drift = {
        flag: {"expected": expected_rows[flag], "observed": observed[flag]}
        for flag in expected_rows
        if observed[flag] != expected_rows[flag]
    }
    if drift:
        raise ContractViolation(f"launcher raw argv differs from frozen v1: {drift}")
    return observed


def _checked_environment(
    environ: Mapping[str, str] | None,
    contract: Mapping[str, Any],
) -> dict[str, str]:
    source: Mapping[str, str] = os.environ if environ is None else environ
    if not isinstance(source, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in source.items()
    ):
        raise ContractViolation("launcher environment must map strings to strings")
    overrides = {
        name: source[name]
        for name in contract["forbidden_environment"]
        if source.get(name, "") != ""
    }
    if overrides:
        raise ContractViolation(
            f"launcher environment attempts governed overrides: {sorted(overrides)}"
        )
    return dict(source)


def _normalize_effective(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize_effective(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_effective(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractViolation("production parser returned a non-finite value")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContractViolation(
        f"production parser returned non-JSON field value {type(value).__name__}"
    )


def parse_effective_config(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Round-trip a full launcher command through the real bridge parser."""

    payload = dict(contract or load_contract())
    clean_environment = _checked_environment(environ, payload)
    try:
        with patch.dict(os.environ, clean_environment, clear=True):
            bridge = importlib.import_module("kunwei_rtde_bridge")
            expected_source = (EXPERIMENT_ROOT / "tools/kunwei_rtde_bridge.py").resolve()
            module_source = Path(str(getattr(bridge, "__file__", ""))).resolve(
                strict=False
            )
            parser_source = Path(bridge.parse_args.__code__.co_filename).resolve(
                strict=False
            )
            if module_source != expected_source or parser_source != expected_source:
                raise ContractViolation(
                    "production bridge parser provenance differs: "
                    f"module={module_source} function={parser_source}"
                )
            parsed = bridge.parse_args(list(argv[2:]))
    except SystemExit as exc:
        raise ContractViolation(f"production bridge parser rejected argv: {exc}") from exc
    except ImportError as exc:
        raise ContractViolation(f"production bridge parser cannot be imported: {exc}") from exc
    normalized = _normalize_effective(vars(parsed))
    if not isinstance(normalized, dict):
        raise ContractViolation("production bridge parser did not return a Namespace mapping")
    return normalized


def _effective_drift(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: {"expected": expected[name], "observed": observed[name]}
        for name in expected
        if observed[name] != expected[name]
    }


def validate_effective_config(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> None:
    missing = sorted(set(expected) - set(observed))
    unclassified = sorted(set(observed) - set(expected))
    if missing or unclassified:
        raise ContractViolation(
            "production parser field classification differs: "
            f"missing={missing} unclassified={unclassified}"
        )
    drift = _effective_drift(expected, observed)
    if drift:
        raise ContractViolation(f"effective production configuration differs: {drift}")


def check_effective_config(
    candidate: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    runtime_root: Path = DEFAULT_CHECK_RUNTIME_ROOT,
    argv: Sequence[str] | None = None,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
    verify_sources: bool = True,
    launch_profile_path: Path | None = None,
    trial_overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe attestation; fail before any process or device action."""

    contract = load_contract(contract_path)
    candidate_values = normalize_candidate(candidate)
    runtime = runtime_values(runtime_root)
    launch_profile = None
    normalized_overlay = None
    if launch_profile_path is not None:
        from .runtime_profile import load_launch_profile, normalize_trial_overlay

        launch_profile = load_launch_profile(launch_profile_path, contract=contract)
        normalized_overlay = normalize_trial_overlay(
            trial_overlay,
            profile=launch_profile,
        )
        candidate_values = {
            name: normalized_overlay[name]
            for name in ("force_p_gain", "force_i_gain", "force_damping")
        }
    expected_argv = build_bridge_argv(
        runtime_root,
        candidate_values,
        contract=contract,
        launch_profile=launch_profile,
        trial_overlay=normalized_overlay,
    )
    actual_argv = list(expected_argv if argv is None else argv)
    validate_raw_argv(actual_argv, expected=expected_argv, contract=contract)
    if verify_sources:
        validate_source_bindings(contract)
    effective = parse_effective_config(
        actual_argv,
        environ=environ,
        contract=contract,
    )
    baseline_expected = expected_effective_config(
        contract,
        candidate=candidate_values,
        runtime=runtime,
    )
    missing = sorted(set(baseline_expected) - set(effective))
    unclassified = sorted(set(effective) - set(baseline_expected))
    if missing or unclassified:
        raise ContractViolation(
            "production parser field classification differs: "
            f"missing={missing} unclassified={unclassified}"
        )
    expected = (
        parse_effective_config(expected_argv, environ=environ, contract=contract)
        if launch_profile is not None
        else baseline_expected
    )
    validate_effective_config(expected, effective)
    control_effective = effective
    if launch_profile is not None:
        launch_argv = build_bridge_argv(
            runtime_root,
            candidate_values,
            contract=contract,
            launch_profile=launch_profile,
            trial_overlay=None,
        )
        control_effective = parse_effective_config(
            launch_argv,
            environ=environ,
            contract=contract,
        )
    profile_report: dict[str, Any] = {}
    if launch_profile is not None and normalized_overlay is not None:
        from .runtime_profile import (
            comparison_profile_fingerprint,
            overlay_fingerprint,
        )

        profile_report = {
            "launch_profile_fingerprint": launch_profile.fingerprint,
            "trial_overlay": normalized_overlay,
            "trial_overlay_fingerprint": overlay_fingerprint(
                launch_profile, normalized_overlay
            ),
            "comparison_profile_fingerprint": comparison_profile_fingerprint(
                launch_profile, normalized_overlay
            ),
        }
    return {
        "schema": CHECK_SCHEMA,
        "ok": True,
        "no_motion": True,
        "frozen_baseline": contract["frozen_baseline"],
        "deployment_tp_identity": contract["deployment_tp_identity"],
        "execution_profile_id": contract["execution_profile_id"],
        "contract_sha256": contract_sha256(contract),
        "control_fingerprint": control_fingerprint(contract, control_effective),
        "candidate": candidate_values,
        "runtime_root": str(runtime_root.resolve(strict=False)),
        "argv": actual_argv,
        "field_categories": {
            category: sorted(contract["effective_fields"][category])
            for category in CATEGORIES
        },
        "effective_config": effective,
        **profile_report,
    }


validate_contract = check_effective_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_CHECK_RUNTIME_ROOT)
    parser.add_argument("--force-p-gain", type=float, default=0.001)
    parser.add_argument("--force-i-gain", type=float, default=0.00001)
    parser.add_argument("--force-damping", type=float, default=7.0)
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("this launcher is check-only; --check is required")
    try:
        report = check_effective_config(
            {
                "force_p_gain": args.force_p_gain,
                "force_i_gain": args.force_i_gain,
                "force_damping": args.force_damping,
            },
            runtime_root=args.runtime_root,
        )
    except ContractViolation as exc:
        parser.exit(24, f"refusing Step5d autotune v3 launch: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
