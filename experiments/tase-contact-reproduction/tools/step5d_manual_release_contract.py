"""Historical Manual V2 release-certificate scope.

Manual V2 has no active launcher route.  This module exists only so retained
evidence readers do not make the active V3 release contract depend on Manual.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from promote_step5d_manual_release import load_manual_release
from step5d_autotune_v3.release_certificate import release_certificate_scope
from step5d_autotune_v3.release_contract import (
    PROFILE,
    ReleaseContractError,
    _control_environment_sha256,
    _require_sha256,
    _sha256_file,
)
from step5d_autotune_v3.runtime_installation import (
    load_runtime_pointer_identity,
    runtime_epoch,
)


def manual_release_contract_scope(
    experiment_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(experiment_root).resolve(strict=True)
    try:
        release = load_manual_release(root)
    except Exception as exc:
        raise ReleaseContractError(
            f"Manual release identity is invalid: {exc}"
        ) from exc
    source = _require_sha256(
        release.get("source_surface_sha256"), "Manual source surface"
    )
    pointer = load_runtime_pointer_identity(
        environ=os.environ if environment is None else environment
    )
    return release_certificate_scope(
        subject_kind="manual_v2",
        release_manifest_sha256=_require_sha256(
            release.get("manifest_sha256"), "Manual release manifest"
        ),
        source_fingerprint=source,
        source_files_fingerprint=source,
        launcher_sha256=_sha256_file(
            root / "scripts/step5d-autotune-v3.sh", "canonical launcher"
        ),
        control_environment_sha256=_control_environment_sha256(pointer),
        runtime_epoch=runtime_epoch(pointer),
        contract_profile=PROFILE,
    )


__all__ = ["manual_release_contract_scope"]
