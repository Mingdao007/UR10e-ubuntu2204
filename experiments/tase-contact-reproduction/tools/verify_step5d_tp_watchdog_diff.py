#!/usr/bin/env python3
"""Fail closed until TP v2 is proven to preserve the fetched controller triplet."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path


EXTENSIONS = (".script", ".txt", ".urp")
BEGIN = "AUTOTUNE_WATCHDOG_V2_BEGIN"
END = "AUTOTUNE_WATCHDOG_V2_END"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_content(
    path: Path, *, current_program: str, candidate_program: str
) -> tuple[bytes, int]:
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".urp" else path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"triplet member is not UTF-8 after decoding: {path}") from exc
    output: list[str] = []
    inside = False
    blocks = 0
    for line in text.splitlines(keepends=True):
        if BEGIN in line:
            if inside:
                raise ValueError(f"nested watchdog marker in {path}")
            inside = True
            blocks += 1
            continue
        if END in line:
            if not inside:
                raise ValueError(f"orphan watchdog end marker in {path}")
            inside = False
            continue
        if not inside:
            output.append(line)
    if inside:
        raise ValueError(f"unterminated watchdog marker in {path}")
    normalized = "".join(output).replace(candidate_program, current_program)
    if path.suffix == ".urp":
        normalized = re.sub(r'crcValue="[^"]*"', 'crcValue="NORMALIZED"', normalized)
    return normalized.encode("utf-8"), blocks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-readback", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--program", default="step5d_strict_rnn_autotune_v2")
    parser.add_argument("--attestation", type=Path, required=True)
    args = parser.parse_args()
    current = args.current_readback.resolve()
    candidate = args.candidate.resolve()
    attestation = json.loads(args.attestation.read_text(encoding="utf-8"))
    if attestation.get("schema") != "step5d.autotune.tp-watchdog-diff/v2":
        raise SystemExit("unknown TP watchdog diff attestation schema")
    if attestation.get("control_math_changed") is not False:
        raise SystemExit("TP watchdog attestation must prove unchanged control math")
    if attestation.get("trajectory_changed") is not False:
        raise SystemExit("TP watchdog attestation must prove unchanged trajectory")
    if attestation.get("waypoint_changed") is not False:
        raise SystemExit("TP watchdog attestation must prove unchanged waypoint")
    fetched = attestation.get("fetched_controller_sha256") or {}
    candidate_sha = attestation.get("candidate_sha256") or {}
    normalized_sha = attestation.get("normalized_content_sha256") or {}
    watchdog_counts = attestation.get("watchdog_block_count") or {}
    for extension in EXTENSIONS:
        current_matches = list(current.glob(f"*{extension}"))
        next_path = candidate / f"{args.program}{extension}"
        if len(current_matches) != 1 or not next_path.is_file():
            raise SystemExit(f"triplet member missing or ambiguous: {extension}")
        if fetched.get(extension) != sha(current_matches[0]):
            raise SystemExit(f"fetched controller SHA drift: {extension}")
        if candidate_sha.get(extension) != sha(next_path):
            raise SystemExit(f"candidate SHA drift: {extension}")
        try:
            current_normalized, current_blocks = normalized_content(
                current_matches[0],
                current_program=current_matches[0].stem,
                candidate_program=args.program,
            )
            candidate_normalized, candidate_blocks = normalized_content(
                next_path,
                current_program=current_matches[0].stem,
                candidate_program=args.program,
            )
        except (OSError, ValueError, gzip.BadGzipFile) as exc:
            raise SystemExit(f"cannot normalize TP diff {extension}: {exc}") from exc
        if current_blocks != 0:
            raise SystemExit(f"fetched controller unexpectedly contains v2 markers: {extension}")
        if extension in {".script", ".urp"} and candidate_blocks < 1:
            raise SystemExit(f"candidate lacks bounded watchdog blocks: {extension}")
        actual_normalized = hashlib.sha256(candidate_normalized).hexdigest()
        if candidate_normalized != current_normalized:
            raise SystemExit(
                f"candidate changes content outside identity/watchdog blocks: {extension}"
            )
        if normalized_sha.get(extension) != actual_normalized:
            raise SystemExit(f"normalized content attestation drift: {extension}")
        if watchdog_counts.get(extension) != candidate_blocks:
            raise SystemExit(f"watchdog block-count attestation drift: {extension}")
    script = (candidate / f"{args.program}.script").read_text(encoding="utf-8")
    required = (
        "AUTOTUNE_WATCHDOG_V2",
        "host_heartbeat_timeout_at_home",
        "host_heartbeat_timeout_unknown_home",
        "AUTO_HOME_AFTER_HEARTBEAT_LOSS: false",
    )
    missing = [token for token in required if token not in script]
    if missing:
        raise SystemExit(f"candidate lacks watchdog semantic markers: {missing}")
    print("tp_watchdog_diff_gate=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
