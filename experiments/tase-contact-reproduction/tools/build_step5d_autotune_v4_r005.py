#!/usr/bin/env python3
"""Build the offline-only Autotune V4 r005 TP triplet and sanity records."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import build_step4e_p0p1_programs as package_support
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from step5d_autotune_v4_r005.contracts import (
    CONTRACT_PATH,
    D_ANCHOR,
    PROGRAM,
    R005Contract,
    TARGET_FORCE_N,
    load_contract,
    refresh_contract_document,
)
from step5d_autotune_v4_r005.tp import CONTROLLER_DIR, RUNTIME_PROTOCOL, render_script


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "programs/step5/step5d"
LIVE_WRITER_CONFIG = ROOT / "config/step5d/autotune_v4_live_writer_r005.json"
BASELINE_GENESIS = ROOT / "config/step5d/autotune_v4_r005_baseline_ledger_genesis.json"
OFFLINE_CLOSURE = ROOT / "config/step5d/autotune_v4_r005_offline_closure.json"
R005_STAMP = "2026-08-02T1200HKT_STEP5D_AUTOTUNE_V4_R005"
STAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}HKT_STEP5D_AUTOTUNE_V4_R005$")


def identity_bindings(contract: R005Contract) -> dict[str, Any]:
    """Derive every r005 identity limb from the validated release contract."""

    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM, contract.sha256, contract.campaign_fingerprint
    )
    return {
        "contract_sha256": contract.sha256,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "runtime_identity": {"hi": runtime_hi, "lo": runtime_lo},
    }


def _load_contract(contract_path: Path = CONTRACT_PATH) -> R005Contract:
    return load_contract(contract_path)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_stamp(now: datetime | None = None) -> str:
    if now is None:
        return R005_STAMP
    return now.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H%MHKT_STEP5D_AUTOTUNE_V4_R005"
    )


def validate_stamp(stamp: str) -> str:
    if STAMP_PATTERN.fullmatch(stamp) is None:
        raise ValueError("r005 source stamp has the wrong format")
    return stamp


def build_txt(stamp: str, *, contract_path: Path = CONTRACT_PATH) -> str:
    contract = _load_contract(contract_path)
    return f"""Step5d independent new-EOAT 5 N Autotune V4 r005 resident host/TP loop

Controller target:
  {CONTROLLER_DIR}/{PROGRAM}.urp

Version:
  {stamp}

Campaign lifecycle:
  HOME -> dispatch -> ARM -> bounded 60 s attempt -> safe return -> durable
  sealed observation/tell -> sequential refill -> next ARM.  TP remains
  resident at READY_HOME_NEXT and accepts positive monotonically increasing
  attempt sequences without a 1..16 bound.

Campaign bootstrap:
  Start a fresh r005 epoch, pass three fresh qualifications, then run the
  existing ten-row P/D bootstrap.  The immutable target is 5 N and D=28 is
  only the incumbent anchor.  After bootstrap the full named domain is
  P,D,tau,I_on_log2,I_off,Ko,Kp on a bounded 0.25-octave lattice.

Optimizer and queue:
  V4 maps named coordinates through the V3 durable queue and CUDA qLogNEI
  adapter.  One physical dispatch is inflight; at most two rows are pending.
  X_pending includes both pending rows, observed/pending duplicate candidates
  are excluded, and no degraded/local optimizer fallback exists.  Every
  proposal changes at most one physical coordinate by at most 0.25 octave.

Evidence and timing:
  Safe returned but nontrainable rows are sealed and durably excluded while
  the loop continues.  Binding, sequence, alignment, safety, or return faults
  stop and revoke authority. qdot and actual_qd share packet sequence, RTDE
  sequence, and timestamp binding. Absolute-deadline pacing is 500 Hz and the
  writer/RTDE/Kunwei/TP gates are distinct and each >=460 Hz.

Completion:
  An eligible MAE <=0.20 N cancels queued pending rows durably and starts
  three incumbent retests. Completion requires >=2/3 retests <=0.20 N, all
  motion/timing/contact/return/identity gates, and a retest median at least 5%
  better than the bootstrap anchor median. Failed retests return to BO; there
  is no trial-count limit. Domain exhaustion, operator stop, or hard fault is
  incomplete.

Resume and claim boundary:
  Resume performs fresh-process cold-read hash-chain verification. r004
  qualifications are audit-only and never imported. This is an offline source
  and package build: no upload, read-back, Load, Play, ARM, bridge, motion,
  contact, zero, tare, sensor write, network, or live evidence is present.

Binding:
  contract_sha256={contract.sha256}
  campaign_fingerprint={contract.campaign_fingerprint}
  target_force_n={TARGET_FORCE_N:.1f}; anchor_D={D_ANCHOR:.1f}
"""


def _urp_content(urp: bytes) -> tuple[ET.Element, str, str]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    return root, cached, script_path


def validate_urscript_block_balance(script: str) -> None:
    starters = re.compile(r"^(def|thread|if|while|for|sec)\b.*:$")
    stack: list[int] = []
    for line_number, line in enumerate(script.splitlines(), 1):
        stripped = line.strip()
        if starters.match(stripped):
            stack.append(line_number)
        elif stripped == "end":
            if not stack:
                raise ValueError(f"unmatched URScript end at {line_number}")
            stack.pop()
    if stack:
        raise ValueError(f"unclosed URScript block at {stack[-1]}")


def numeric_sanity(
    script: str,
    *,
    contract_path: Path = CONTRACT_PATH,
) -> dict[str, Any]:
    contract = _load_contract(contract_path)
    checks = {
        "target_exact_5n": "TARGET_FORCE_N: 5.0" in script,
        "anchor_damping_exact_28": "D_ANCHOR: 28.0" in script,
        "r005_runtime_protocol": str(RUNTIME_PROTOCOL) in script,
        "resident_waiting_loop": "while True:" in script and "READY_HOME_NEXT" in script,
        "positive_unbounded_attempt": "input_ordinal < 1" in script and "ordinal > 16" not in script,
        "resident_epoch_immutable": "(active_epoch > 0 and input_epoch != active_epoch)" in script,
        "complete_identity_exact": (
            "input_epoch == active_epoch and input_ordinal == current_ordinal" in script
            and "input_token == current_token and input_kind == current_kind" in script
        ),
        "old_phase_plan_removed": "input_ordinal >= 14 and input_kind != 4" not in script,
        "five_hundred_hz_contract": "R005_RATE_CONTRACT: 500 Hz" in script,
        "path_runtime_60": "path_elapsed_s < 60.000000000" in script,
        "no_live_stop_ordinal_marker": "stop-after-ordinal" not in script,
        "no_ur_force": "force_mode(" not in script and "get_tcp_force(" not in script,
        "no_sensor_write": "zero_ftsensor(" not in script and "tare(" not in script.lower(),
    }
    if not all(checks.values()):
        raise ValueError(f"r005 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.autotune-v4/r005-numeric-sanity-v1",
        "program": PROGRAM,
        "contract_sha256": contract.sha256,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "target_force_n": TARGET_FORCE_N,
        "anchor_damping": D_ANCHOR,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "checks": checks,
        "passed": True,
        "live_evidence": False,
    }


def validate_triplet(
    script: str,
    txt: str,
    urp: bytes,
    stamp: str,
    *,
    contract_path: Path = CONTRACT_PATH,
) -> dict[str, bool]:
    contract = _load_contract(contract_path)
    root, cached, script_path = _urp_content(urp)
    validate_urscript_block_balance(script)
    checks = {
        "version_stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script_node_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM}.script",
        "cached_contents_exact": cached == script,
        "r005_contract_binding": contract.sha256 in script and contract.campaign_fingerprint in script,
        "resident_loop": "while True:" in script and "R005_ATTEMPT_SEQUENCE" in script,
        "ordinal_gate_removed": "ordinal > 16" not in script and "input_ordinal > 16" not in script,
        "phase_plan_removed": "input_ordinal >= 14 and input_kind != 4" not in script,
        "unbounded_arm": (
            "input_ordinal < 1" in script
            and "input_ordinal <= current_ordinal" in script
            and "input_ordinal > 16" not in script
        ),
        "resident_epoch_immutable": "(active_epoch > 0 and input_epoch != active_epoch)" in script,
        "complete_identity_exact": "input_token == current_token and input_kind == current_kind" in script,
        "offline_text": "no upload, read-back, Load, Play" in txt,
        "flow_text": "HOME -> dispatch -> ARM" in txt and "X_pending" in txt,
    }
    if not all(checks.values()):
        raise ValueError(f"r005 triplet validation failed: {checks}")
    return checks


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"r005 identity binding is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"r005 identity binding is not strict JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"r005 identity binding must be an object: {path}")
    return value


def _temporary_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return temporary


def _atomic_replace(path: Path, data: bytes) -> None:
    temporary = _temporary_bytes(path, data)
    try:
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _commit_artifacts(
    entries: Mapping[Path, bytes],
    *,
    rebuild: bool,
) -> None:
    """Validate, stage, and replace a rebuild's complete identity set."""

    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    try:
        for raw_path, data in entries.items():
            path = Path(raw_path)
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise FileExistsError(f"r005 artifact is not a regular file: {path}")
                previous = path.read_bytes()
                if previous == data:
                    continue
                if not rebuild:
                    raise FileExistsError(
                        f"r005 artifact already exists with different bytes: {path}; use --rebuild"
                    )
                originals[path] = previous
            else:
                originals[path] = None
            staged[path] = _temporary_bytes(path, data)
    except Exception:
        for temporary in staged.values():
            if temporary.exists():
                temporary.unlink()
        raise
    committed: list[Path] = []
    try:
        for path, temporary in staged.items():
            os.replace(temporary, path)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            previous = originals[path]
            if previous is None:
                if path.exists() or path.is_symlink():
                    path.unlink()
            else:
                _atomic_replace(path, previous)
        raise
    finally:
        for temporary in staged.values():
            if temporary.exists():
                temporary.unlink()


def _refresh_auxiliary_bindings(
    identity: Mapping[str, Any],
) -> dict[Path, bytes]:
    live_writer = _read_json(LIVE_WRITER_CONFIG)
    live_writer["release_binding"] = dict(identity)
    baseline = _read_json(BASELINE_GENESIS)
    baseline["release_contract_sha256"] = identity["contract_sha256"]
    baseline["campaign_fingerprint"] = identity["campaign_fingerprint"]
    baseline["runtime_identity"] = dict(identity["runtime_identity"])
    return {
        LIVE_WRITER_CONFIG: _json_bytes(live_writer),
        BASELINE_GENESIS: _json_bytes(baseline),
    }


def write_triplet(
    output_dir: Path = OUTPUT_DIR,
    *,
    stamp: str = R005_STAMP,
    rebuild: bool = False,
) -> dict[str, Any]:
    stamp = validate_stamp(stamp)
    output_dir = Path(output_dir)
    contract_bytes: bytes | None = None
    temporary_contract: Path | None = None
    if rebuild:
        _, contract_bytes = refresh_contract_document(CONTRACT_PATH)
        temporary_contract = _temporary_bytes(CONTRACT_PATH, contract_bytes)
        contract = _load_contract(temporary_contract)
    else:
        contract = _load_contract(CONTRACT_PATH)
    contract_path = contract.path
    identity = identity_bindings(contract)
    try:
        script = f"# VERSION: {stamp}\n{render_script(contract_path=contract_path)}"
        txt = build_txt(stamp, contract_path=contract_path)
        urp = package_support.build_urp(script, PROGRAM, CONTROLLER_DIR)
        checks = validate_triplet(script, txt, urp, stamp, contract_path=contract_path)
        sanity = numeric_sanity(script, contract_path=contract_path)
    except Exception:
        if temporary_contract is not None and temporary_contract.exists():
            temporary_contract.unlink()
        raise
    paths = {
        suffix: output_dir / f"{PROGRAM}{suffix}"
        for suffix in (".script", ".txt", ".urp")
    }
    numeric_path = output_dir / f"{PROGRAM}.numeric-sanity.json"
    manifest_path = output_dir / f"{PROGRAM}.deploy-manifest.json"
    package_bytes = {
        paths[".script"]: script.encode("utf-8"),
        paths[".txt"]: txt.encode("utf-8"),
        paths[".urp"]: urp,
    }
    numeric_bytes = _json_bytes(sanity)
    package_bytes[numeric_path] = numeric_bytes
    digests = {
        suffix: sha256_bytes(package_bytes[path]) for suffix, path in paths.items()
    }
    manifest = {
        "schema_version": 1,
        "basename": PROGRAM,
        "controller_directory": CONTROLLER_DIR,
        "release_binding": dict(identity),
        "artifacts": [
            {
                "filename": f"{PROGRAM}{suffix}",
                "source": f"{PROGRAM}{suffix}",
                "sha256": digests[suffix],
            }
            for suffix in (".script", ".txt", ".urp")
        ],
    }
    manifest_bytes = _json_bytes(manifest)
    package_bytes[manifest_path] = manifest_bytes
    entries: dict[Path, bytes] = dict(package_bytes)
    if rebuild:
        assert contract_bytes is not None
        entries[CONTRACT_PATH] = contract_bytes
        entries.update(_refresh_auxiliary_bindings(identity))
        if output_dir.resolve() == OUTPUT_DIR.resolve():
            from build_step5d_autotune_v4_r005_offline_closure import build_document

            overrides = {
                path.relative_to(ROOT).as_posix(): data for path, data in entries.items()
            }
            closure = build_document(contract=contract, overrides=overrides)
            entries[OFFLINE_CLOSURE] = _json_bytes(closure)
    try:
        _commit_artifacts(entries, rebuild=rebuild)
    finally:
        if temporary_contract is not None and temporary_contract.exists():
            temporary_contract.unlink()
    return {
        "program": PROGRAM,
        "stamp": stamp,
        "sha256": digests,
        "numeric_sanity_sha256": sha256_bytes(numeric_bytes),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "contract_sha256": identity["contract_sha256"],
        "campaign_fingerprint": identity["campaign_fingerprint"],
        "runtime_identity": identity["runtime_identity"],
        "checks": checks,
        "live_evidence": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--stamp", default=R005_STAMP)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="explicitly refresh r004 parent digests and atomically rebuild all r005 identity bindings",
    )
    args = parser.parse_args(argv)
    print(
        json.dumps(
            write_triplet(args.output_dir, stamp=args.stamp, rebuild=args.rebuild),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
