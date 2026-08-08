#!/usr/bin/env python3
"""Offline repro: Phase5 cold_read import-binding bug (Claude audit problem 2).

Writes NDJSON to /home/andy/.cursor/debug-aec521.log
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

LOG = Path("/home/andy/.cursor/debug-aec521.log")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def _log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict,
    *,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    rec = {
        "sessionId": "aec521",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # #endregion


def main() -> int:
    from step5d_autotune_v4_r006.contracts import load_contract
    from step5d_autotune_v4_r006.lattice import ANCHOR_POINT
    from step5d_autotune_v4_r006 import objective as obj_mod
    from step5d_autotune_v4_r006.objective import (
        R006ObjectiveBuilder,
        cold_read_verify as stock_bound_name,
    )
    from step5d_autotune_v4_r008.binary_seal import (
        R008_OBJECTIVE_RECEIPT_VERSION,
        peek_pending_raw2,
        r008_binary_seal_scope,
    )
    # Intentionally do NOT import cold_read_verify from bounded_worker — it
    # must not re-export a frozen stock binding.
    from step5d_autotune_v4_r008.raw_force_binary import SOURCE_KEYS
    from step5d_force_objective import ForcePathSample

    def sample(t: float, f: float, seq: int) -> ForcePathSample:
        return ForcePathSample(
            path_time_s=t,
            path_phase=25,
            filtered_normal_n=f,
            source_sequences={k: seq for k in SOURCE_KEYS},
            source_ages_s={k: 0.001 for k in SOURCE_KEYS},
            commanded_qdot=(0.01, -0.02, 0.03, -0.04, 0.05, -0.06),
            actual_qd=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            timestamp_s=1000.0 + t,
        )

    samples = [sample(0.05 + i * 0.1, 5.1, i + 1) for i in range(550)]
    samples.extend(sample(55.05 + i * 0.1, 5.1, 551 + i) for i in range(50))
    contract = load_contract(verify_source_closure=False)

    with r008_binary_seal_scope():
        builder = R006ObjectiveBuilder(
            attempt_sequence=1,
            execution_id="debug-p5-import-bind",
            campaign_fingerprint=contract.campaign_fingerprint,
            candidate_uid=ANCHOR_POINT.uid,
        )
        for s in samples:
            builder.add(s)
        receipt = builder.finalize()
        raw = peek_pending_raw2(1, "debug-p5-import-bind")
        assert receipt.version == R008_OBJECTIVE_RECEIPT_VERSION
        assert list(receipt.raw_bundle.get("samples") or []) == []
        assert raw is not None

        patched = obj_mod.cold_read_verify
        import step5d_autotune_v4_r008.bounded_worker_artifact_binding as bind_mod

        _log(
            "A",
            "repro:identity",
            "function identity under scope",
            {
                "obj_mod_is_binary": patched is not stock_bound_name,
                "worker_module_exports_cold_read": hasattr(bind_mod, "cold_read_verify"),
                "receipt_version": receipt.version,
                "sample_count_field": receipt.sample_count,
                "bundle_samples_len": len(receipt.raw_bundle.get("samples") or []),
            },
        )

        # H-A: stock-bound name (import-time freeze) still fails on empty samples
        try:
            stock_bound_name(
                receipt, expected_campaign_fingerprint=contract.campaign_fingerprint
            )
            _log("A", "repro:stock_bound", "UNEXPECTED_OK", {})
        except Exception as exc:  # noqa: BLE001
            _log(
                "A",
                "repro:stock_bound",
                "FAILED_as_expected",
                {"exc_type": type(exc).__name__, "exc": str(exc)[:300]},
            )

        # H-B: module attribute patch works with artifact_raw_bytes
        try:
            ok = obj_mod.cold_read_verify(
                receipt,
                expected_campaign_fingerprint=contract.campaign_fingerprint,
                artifact_raw_bytes=raw,
            )
            _log(
                "B",
                "repro:obj_mod_patched",
                "OK",
                {
                    "trainable": ok.trainable,
                    "mae": ok.objective_mae_n,
                    "verification_state": ok.verification_state,
                },
            )
        except Exception as exc:  # noqa: BLE001
            _log(
                "B",
                "repro:obj_mod_patched",
                "FAILED",
                {"exc_type": type(exc).__name__, "exc": str(exc)[:300]},
            )

    # H-C: stock sidecar subprocess path (no r008 patch) fails on empty samples
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        art = Path(td) / "slim.json"
        art.write_text(json.dumps(receipt.as_dict(), sort_keys=True) + "\n", encoding="utf-8")
        # no .r008raw sibling — stock path only sees empty samples
        tools_root = str(ROOT / "tools")
        code = (
            "import json, pathlib, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from step5d_autotune_v4_r006.objective import R006ObjectiveReceipt, cold_read_verify\n"
            "path = pathlib.Path(sys.argv[2])\n"
            "receipt = R006ObjectiveReceipt.from_mapping(json.loads(path.read_text(encoding='utf-8')))\n"
            "verified = cold_read_verify(receipt, expected_campaign_fingerprint=sys.argv[3])\n"
            "print(json.dumps({'ok': True, 'mae': verified.objective_mae_n}))\n"
        )
        # from_mapping may fail first on version outside scope
        completed = subprocess.run(
            [sys.executable, "-c", code, tools_root, str(art), contract.campaign_fingerprint],
            capture_output=True,
            text=True,
            timeout=60,
        )
        _log(
            "C",
            "repro:stock_subprocess",
            "child_exit",
            {
                "returncode": completed.returncode,
                "stdout": (completed.stdout or "")[:200],
                "stderr_tail": (completed.stderr or "")[-400:],
            },
        )

    # H-D: without scope, from_mapping P5 receipt fails (worker would hit this too)
    try:
        obj_mod.R006ObjectiveReceipt.from_mapping(receipt.as_dict())
        _log("D", "repro:from_mapping_no_scope", "UNEXPECTED_OK", {})
    except Exception as exc:  # noqa: BLE001
        _log(
            "D",
            "repro:from_mapping_no_scope",
            "FAILED_as_expected",
            {"exc_type": type(exc).__name__, "exc": str(exc)[:300]},
        )

    # H-E post-fix: bounded_artifact_binding must accept P5 RAW2 tail row
    from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar
    from step5d_autotune_v4_r008.bounded_sidecar_verify import r008_bounded_sidecar_scope
    from step5d_autotune_v4_r008.bounded_worker_artifact_binding import (
        bounded_artifact_binding,
        clear_receipt_cache,
    )

    clear_receipt_cache()
    with tempfile.TemporaryDirectory() as td2:
        td2p = Path(td2)
        with r008_bounded_sidecar_scope(tail_rows=1):
            sidecar = R006ObjectiveSidecar(
                td2p / "r006-objectives.jsonl",
                campaign_fingerprint=contract.campaign_fingerprint,
            )
            row = sidecar.append(
                receipt,
                epoch=1,
                candidate_uid=ANCHOR_POINT.uid,
                kind="STAIRCASE",
                point_key=list(ANCHOR_POINT.key),
            )
        _sidecar_data = sidecar.path.read_bytes()
        binding = {
            "sidecar_path": str(sidecar.path),
            "sidecar_sha256": __import__("hashlib").sha256(_sidecar_data).hexdigest(),
            "sidecar_prefix_bytes": len(_sidecar_data),
            "campaign_fingerprint": contract.campaign_fingerprint,
            "rows": [
                {
                    "attempt_sequence": 1,
                    "execution_id": "debug-p5-import-bind",
                }
            ],
        }
        try:
            out = bounded_artifact_binding(binding, tail_rows=1)
            verified = out["rows"][0]["receipt"]
            _log(
                "E",
                "repro:bounded_artifact_binding",
                "OK",
                {
                    "trainable": verified.trainable,
                    "mae": verified.objective_mae_n,
                    "version": verified.version,
                    "artifact": str(row.get("artifact_path")),
                },
                run_id="post-fix",
            )
        except Exception as exc:  # noqa: BLE001
            _log(
                "E",
                "repro:bounded_artifact_binding",
                "FAILED",
                {"exc_type": type(exc).__name__, "exc": str(exc)[:400]},
                run_id="post-fix",
            )

    print("repro done; see", LOG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
