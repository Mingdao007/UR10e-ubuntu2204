"""Load and hash-check the Stage B domain design artifact."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import math

from step5d_autotune_v4_r008.lattice import BoxRegion, R008Point


class DesignBindingError(ValueError):
    """Domain design artifact failed verification."""


SCHEMA = "step5d.autotune-v4/r008-domain-design-v1"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_domain_design(path: Path) -> Mapping[str, Any]:
    path = Path(path)
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DesignBindingError(f"domain design is not JSON: {path}") from exc
    if document.get("schema") != SCHEMA:
        raise DesignBindingError(f"unexpected domain schema: {document.get('schema')}")
    body = {key: value for key, value in document.items() if key != "artifact_sha256"}
    digest = _sha256_bytes(_canonical_bytes(body))
    if document.get("artifact_sha256") != digest:
        raise DesignBindingError("domain design artifact_sha256 mismatch")
    return document


def box_from_document(document: Mapping[str, Any]) -> BoxRegion:
    box = document["box"]
    return BoxRegion(
        log2_pd_min=float(box["log2_pd"][0]),
        log2_pd_max=float(box["log2_pd"][1]),
        log2_d_min=float(box["log2_d"][0]),
        log2_d_max=float(box["log2_d"][1]),
        log2_tau_min=float(box["log2_tau"][0]),
        log2_tau_max=float(box["log2_tau"][1]),
        kf_off_allowed=bool(box["kf_off_allowed"]),
        log2_kf_min=float(box["log2_kf"][0]),
        log2_kf_max=float(box["log2_kf"][1]),
        log2_ko_min=float(box["log2_ko"][0]),
        log2_ko_max=float(box["log2_ko"][1]),
        log2_kp_min=float(box["log2_kp"][0]),
        log2_kp_max=float(box["log2_kp"][1]),
    )


def anchor_from_document(document: Mapping[str, Any]) -> R008Point:
    anchor = document["anchor"]
    pd = float(anchor["pd_ratio"])
    damping = float(anchor["force_damping"])
    tau = float(anchor["normal_filter_tau_s"])
    i_gain = float(anchor["force_i_gain"])
    ko = float(anchor["orientation_ko"])
    kp = float(anchor["motion_kp"])
    kf_off = i_gain <= 0.0
    p_gain = float(anchor["force_p_gain"])
    kf = 0.0 if kf_off else i_gain / p_gain
    return R008Point(
        log2_pd=math.log2(pd),
        log2_d=math.log2(damping),
        log2_tau=math.log2(tau),
        kf_off=kf_off,
        log2_kf=None if kf_off else math.log2(kf),
        log2_ko=math.log2(ko),
        log2_kp=math.log2(kp),
    )


__all__ = [
    "SCHEMA",
    "DesignBindingError",
    "anchor_from_document",
    "box_from_document",
    "load_domain_design",
]
