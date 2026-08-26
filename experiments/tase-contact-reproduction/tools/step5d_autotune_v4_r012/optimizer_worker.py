"""Attested CUDA subprocess for the R012 five-dimensional production GP."""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Mapping

from step5d_optimizer_runtime import REQUEST_SCHEMA, RESPONSE_SCHEMA


def run(request: Mapping[str, Any]) -> dict[str, Any]:
    if request.get("schema") != REQUEST_SCHEMA or request.get("profile") != "optimizer":
        raise ValueError("R012 optimizer request identity differs")
    expected = request.get("expected_attestation")
    payload = request.get("payload")
    if not isinstance(expected, Mapping) or not isinstance(payload, Mapping):
        raise ValueError("R012 optimizer request is incomplete")

    # Reuse the already deployed runtime self-attestation, not its optimizer.
    from step5d_autotune_v4_r005.optimizer_worker import _self_attest
    from .censor import ExactObservation
    from .qlognei import ProductionGPConfig, ask_qlognei, fit_production_gp, physical_candidate_key

    rows = tuple(ExactObservation(**{k: v for k, v in row.items() if k != "censored"}) for row in payload["observations"])
    candidates = tuple(payload["candidates"])
    pending_keys = tuple(tuple(key) for key in payload["pending_keys"])
    config_raw = dict(payload["gp_config"])
    for name in ("initial_lengthscales_unit", "lengthscale_bounds_unit"):
        if name in config_raw:
            config_raw[name] = tuple(tuple(v) if isinstance(v, list) else v for v in config_raw[name])
    config = ProductionGPConfig(
        noise_floor_n2=config_raw["noise_floor_n2"],
        initial_lengthscales_unit=config_raw["initial_lengthscales_unit"],
        lengthscale_bounds_unit=config_raw["lengthscale_bounds_unit"],
    )
    fit = fit_production_gp(rows, config=config, allow_historical_out_of_box=True)
    proposal = ask_qlognei(
        fit,
        candidates,
        evaluated_keys=tuple(physical_candidate_key(row.candidate) for row in rows),
        pending_keys=pending_keys,
    )
    return {
        "proposal": proposal.as_dict(),
        "fit_receipt": dict(fit.fit_receipt),
        "attestation": _self_attest(expected),
    }


def main() -> int:
    encoded = sys.stdin.buffer.read()
    try:
        request = json.loads(encoded.decode("utf-8"))
        result = run(request)
        attestation = result.pop("attestation")
        sys.stdout.write(json.dumps({
            "schema": RESPONSE_SCHEMA,
            "ok": True,
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "attestation": attestation,
            "result": result,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}:{exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
