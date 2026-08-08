"""r006-only adapters for the frozen r005 parent release.

The r005 release contract and triplet remain immutable.  A dirty workspace
may contain repaired r004 source files whose historical r005 parent digest is
older than the checked-out source.  r006 keeps the published r005 identity for
receipts and campaign binding, while this adapter records the current source
map only for constructing the already-reviewed mature writer.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from step5d_autotune_v4_r005 import contracts as r005_contracts
from step5d_autotune_v4_r005.contracts import R005Contract, R005ContractError


def _current_parent_map(document) -> dict[str, str]:
    rows = r005_contracts._parent_entries(document)
    current: dict[str, str] = {}
    root = r005_contracts.ROOT
    for relative in rows:
        path = (root / relative).resolve()
        if path.is_symlink() or not path.is_file():
            raise R005ContractError(f"r006 parent source is unavailable: {relative}")
        current[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return current


def load_frozen_r005_contract(
    path: Path | None = None,
) -> tuple[R005Contract, R005Contract]:
    """Return ``(published_identity, current_source_admission)``.

    All normal r005 contract checks run first.  Only the parent-source map is
    relaxed for the r006 composition seam; release JSON, r005 runtime
    manifest, campaign fingerprint, EOAT, objective, and completion values
    remain the published bytes.  The second value is never exposed as an
    r005 release identity and is used only by the mature writer's source
    resolver.
    """

    selected = Path(path or r005_contracts.CONTRACT_PATH)
    try:
        published = r005_contracts.load_contract(selected)
        return published, published
    except R005ContractError as exc:
        if "parent source drift" not in str(exc):
            raise

    document = r005_contracts._read_contract_document(selected)
    if document.get("schema") != "step5d.autotune-v4/r005-release-contract-v1":
        raise R005ContractError("r006 frozen r005 schema differs")
    manifest = r005_contracts._runtime_manifest_declaration(document)
    control_runtime = r005_contracts._control_runtime_declaration(manifest)
    expected = r005_contracts._parent_entries(document)
    current = _current_parent_map(document)
    if set(expected) != set(current):
        raise R005ContractError("r006 frozen r005 parent map differs")
    published = R005Contract(
        path=selected,
        sha256=hashlib.sha256(selected.read_bytes()).hexdigest(),
        campaign_fingerprint=r005_contracts._campaign_fingerprint(document),
        r004_parent_sha256=dict(expected),
        runtime_manifest=manifest,
        control_runtime=control_runtime,
        raw=document,
    )
    admission = replace(published, r004_parent_sha256=current)
    return published, admission


__all__ = ["load_frozen_r005_contract"]
