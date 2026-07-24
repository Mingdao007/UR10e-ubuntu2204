"""Small, immutable evidence for release publication and runtime deployment."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from typing import Any, Mapping

from .atomic_io import AtomicIOError, atomic_bytes


BASIS_SCHEMA = "step5d.autotune-v3/delivery-basis-v1"
LINEAGE_SCHEMA = "step5d.autotune-v3/release-publication-lineage-v1"
BASIS_ROOT = Path("config/step5d/delivery-bases")
PRIOR_RECEIPT_ROOT = BASIS_ROOT / "prior-full-readbacks"
LINEAGE_ROOT = Path("runs/step5d_autotune_v3/release-publication-lineage")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TRIPLET = frozenset({".script", ".txt", ".urp"})


class ReleaseTransitionError(RuntimeError):
    """Release delivery or deployment lineage is incomplete."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReleaseTransitionError(
            f"release transition evidence is not canonical JSON: {exc}"
        ) from exc


def _load(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseTransitionError(f"{role} is missing or unsafe")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseTransitionError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ReleaseTransitionError(
                    f"{role} contains non-finite {constant}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTransitionError(f"{role} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseTransitionError(f"{role} must be a JSON object")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseTransitionError(f"{role} is missing or unsafe")
    return _sha256_bytes(path.read_bytes())


def _sha256_text(value: Any, role: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseTransitionError(f"{role} must be a lowercase SHA-256")
    return value


def _commit_text(value: Any, role: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ReleaseTransitionError(f"{role} must be a full Git commit OID")
    return value


def _triplet(value: Any, role: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _TRIPLET:
        raise ReleaseTransitionError(f"{role} triplet fields differ")
    return {
        extension: _sha256_text(value[extension], f"{role} {extension}")
        for extension in sorted(_TRIPLET)
    }


def _release_triplet(release: Any) -> dict[str, str]:
    source = getattr(release, "artifact_sha256", None)
    if source is None:
        artifacts = getattr(release, "artifacts", None)
        if not isinstance(artifacts, Mapping):
            raise ReleaseTransitionError("release artifact triplet is missing")
        source = {
            extension: reference.get("sha256")
            for extension, reference in artifacts.items()
            if isinstance(reference, Mapping)
        }
    return _triplet(source, "release artifact")


def _relative(root: Path, path: Path, role: str) -> str:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ReleaseTransitionError(f"{role} escapes experiment root") from exc
    if path.is_symlink():
        raise ReleaseTransitionError(f"{role} must not be a symlink")
    return PurePosixPath(relative).as_posix()


def _rooted_file(root: Path, value: Any, role: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ReleaseTransitionError(f"{role} path is invalid")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ReleaseTransitionError(f"{role} path is unsafe")
    unresolved = root.resolve(strict=True) / Path(*relative.parts)
    if unresolved.is_symlink():
        raise ReleaseTransitionError(f"{role} path is unsafe")
    resolved = unresolved.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ReleaseTransitionError(f"{role} path is unsafe") from exc
    return resolved


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    repository = root.resolve(strict=True).parents[1]
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise ReleaseTransitionError(
            f"Git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed


def git_snapshot(root: Path, *, require_clean: bool = True) -> dict[str, Any]:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=no").stdout
    if require_clean and status:
        raise ReleaseTransitionError("tracked repository state is not clean")
    head = _commit_text(
        _git(root, "rev-parse", "--verify", "HEAD").stdout.strip(),
        "repository HEAD",
    )
    tree = _commit_text(
        _git(root, "rev-parse", "--verify", "HEAD^{tree}").stdout.strip(),
        "repository tree",
    )
    return {"head": head, "tree": tree, "tracked_clean": not bool(status)}


def _validate_prior_full_receipt(
    value: Mapping[str, Any],
    *,
    release: Any,
) -> dict[str, Any]:
    hashes = value.get("sha256")
    validation = value.get("validation")
    if (
        value.get("status") != "controller read-back verified"
        or value.get("delivery_mode") != "full_upload_readback"
        or value.get("readback_source") != "fresh_controller_get"
        or value.get("fresh_controller_sha_verified") is not True
        or value.get("target_dir")
        != PurePosixPath(str(release.controller_target)).parent.as_posix()
        or not isinstance(hashes, Mapping)
        or set(hashes) != {"local", "controller", "readback"}
        or not isinstance(validation, Mapping)
        or validation.get("program") != release.program_id
        or validation.get("script_node_path")
        != PurePosixPath(str(release.controller_target)).with_suffix(
            ".script"
        ).as_posix()
    ):
        raise ReleaseTransitionError("prior full-readback receipt identity differs")
    local = _triplet(hashes["local"], "prior receipt local")
    controller = _triplet(hashes["controller"], "prior receipt controller")
    readback = _triplet(hashes["readback"], "prior receipt readback")
    validation_triplet = {
        extension: validation.get(field)
        for extension, field in {
            ".script": "script_sha256",
            ".txt": "txt_sha256",
            ".urp": "urp_sha256",
        }.items()
    }
    if (
        local != controller
        or local != readback
        or readback != _release_triplet(release)
        or validation_triplet != readback
    ):
        raise ReleaseTransitionError("prior full-readback receipt SHA closure differs")
    checked_at = value.get("fresh_controller_checked_at")
    if not isinstance(checked_at, str):
        raise ReleaseTransitionError("prior full-readback timestamp is missing")
    try:
        parsed = datetime.fromisoformat(checked_at)
    except ValueError as exc:
        raise ReleaseTransitionError(
            "prior full-readback timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseTransitionError("prior full-readback timestamp lacks timezone")
    return dict(value)


def _write_immutable(path: Path, encoded: bytes, role: str) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise ReleaseTransitionError(f"immutable {role} differs")
        return
    try:
        atomic_bytes(path, encoded)
    except AtomicIOError as exc:
        raise ReleaseTransitionError(f"cannot write {role}: {exc}") from exc


def create_delivery_basis(
    root: Path,
    *,
    candidate_release: Any,
    basis_release: Any,
    prior_full_receipt: Path,
) -> tuple[Path, dict[str, Any]]:
    """Import one prior full readback and bind it to a no-upload candidate."""

    experiment = root.resolve(strict=True)
    snapshot = git_snapshot(experiment)
    candidate_triplet = _release_triplet(candidate_release)
    if (
        candidate_triplet != _release_triplet(basis_release)
        or candidate_release.program_id != basis_release.program_id
        or candidate_release.controller_target != basis_release.controller_target
    ):
        raise ReleaseTransitionError(
            "no-upload candidate differs from the basis release"
        )
    source_receipt = prior_full_receipt.expanduser()
    if source_receipt.is_symlink():
        raise ReleaseTransitionError("prior full-readback receipt is unsafe")
    source_receipt = source_receipt.resolve(strict=True)
    receipt = _validate_prior_full_receipt(
        _load(source_receipt, "prior full-readback receipt"),
        release=candidate_release,
    )
    receipt_bytes = _canonical_bytes(receipt)
    receipt_sha256 = _sha256_bytes(receipt_bytes)
    imported_receipt = experiment / PRIOR_RECEIPT_ROOT / f"{receipt_sha256}.json"
    _write_immutable(
        imported_receipt,
        receipt_bytes,
        "prior full-readback receipt",
    )
    basis_manifest = _rooted_file(
        experiment,
        basis_release.manifest_path,
        "basis release manifest",
    )
    row = {
        "schema": BASIS_SCHEMA,
        "candidate_release_manifest_sha256": candidate_release.manifest_sha256,
        "program_id": candidate_release.program_id,
        "controller_target": candidate_release.controller_target,
        "triplet_sha256": candidate_triplet,
        "basis_release": {
            "manifest_path": _relative(
                experiment,
                basis_manifest,
                "basis release manifest",
            ),
            "manifest_sha256": basis_release.manifest_sha256,
        },
        "prior_full_readback_receipt": {
            "path": _relative(
                experiment,
                imported_receipt,
                "imported prior full-readback receipt",
            ),
            "sha256": receipt_sha256,
        },
        "code_source_commit": snapshot["head"],
    }
    path = (
        experiment
        / BASIS_ROOT
        / f"{candidate_release.manifest_sha256}.json"
    )
    encoded = _canonical_bytes(row)
    _write_immutable(path, encoded, "delivery basis")
    return path, validate_delivery_basis(
        experiment,
        row,
        release=candidate_release,
    )


def validate_delivery_basis(
    root: Path,
    value: Mapping[str, Any],
    *,
    release: Any,
) -> dict[str, Any]:
    required = {
        "schema",
        "candidate_release_manifest_sha256",
        "program_id",
        "controller_target",
        "triplet_sha256",
        "basis_release",
        "prior_full_readback_receipt",
        "code_source_commit",
    }
    row = dict(value)
    if set(row) != required or row.get("schema") != BASIS_SCHEMA:
        raise ReleaseTransitionError("delivery basis fields or schema differ")
    if (
        _sha256_text(
            row["candidate_release_manifest_sha256"],
            "candidate release manifest",
        )
        != release.manifest_sha256
        or row["program_id"] != release.program_id
        or row["controller_target"] != release.controller_target
        or _triplet(row["triplet_sha256"], "delivery basis")
        != _release_triplet(release)
    ):
        raise ReleaseTransitionError("delivery basis release identity differs")
    experiment = root.resolve(strict=True)
    basis_release = row["basis_release"]
    prior = row["prior_full_readback_receipt"]
    if (
        not isinstance(basis_release, Mapping)
        or set(basis_release) != {"manifest_path", "manifest_sha256"}
        or not isinstance(prior, Mapping)
        or set(prior) != {"path", "sha256"}
    ):
        raise ReleaseTransitionError("delivery basis references differ")
    basis_manifest = _rooted_file(
        experiment,
        basis_release["manifest_path"],
        "basis release manifest",
    )
    if _sha256(basis_manifest, "basis release manifest") != _sha256_text(
        basis_release["manifest_sha256"],
        "basis release manifest",
    ):
        raise ReleaseTransitionError("basis release manifest SHA-256 differs")
    prior_path = _rooted_file(
        experiment,
        prior["path"],
        "prior full-readback receipt",
    )
    prior_sha256 = _sha256_text(
        prior["sha256"],
        "prior full-readback receipt",
    )
    if (
        _sha256(prior_path, "prior full-readback receipt") != prior_sha256
        or prior_path.parent != experiment / PRIOR_RECEIPT_ROOT
        or prior_path.name != f"{prior_sha256}.json"
    ):
        raise ReleaseTransitionError(
            "prior full-readback receipt reference differs"
        )
    _validate_prior_full_receipt(
        _load(prior_path, "prior full-readback receipt"),
        release=release,
    )
    source_commit = _commit_text(row["code_source_commit"], "code source")
    ancestry = _git(
        experiment,
        "merge-base",
        "--is-ancestor",
        source_commit,
        "HEAD",
        check=False,
    )
    if ancestry.returncode != 0:
        raise ReleaseTransitionError(
            "code source commit is not an ancestor of the deployed checkout"
        )
    return row


def delivery_basis_path(root: Path, release: Any) -> Path:
    return (
        root.resolve(strict=True)
        / BASIS_ROOT
        / f"{release.manifest_sha256}.json"
    )


def load_delivery_basis(root: Path, *, release: Any) -> tuple[Path, dict[str, Any]]:
    path = delivery_basis_path(root, release)
    return path, validate_delivery_basis(
        root,
        _load(path, "delivery basis"),
        release=release,
    )


def delivery_basis_reference(root: Path, *, release: Any) -> dict[str, str]:
    path, _basis = load_delivery_basis(root, release=release)
    return {
        "path": _relative(root.resolve(strict=True), path, "delivery basis"),
        "sha256": _sha256(path, "delivery basis"),
    }


def require_receipt_delivery_basis(
    root: Path,
    receipt: Mapping[str, Any],
    *,
    release: Any,
) -> dict[str, str]:
    expected = delivery_basis_reference(root, release=release)
    observed = receipt.get("delivery_basis")
    if not isinstance(observed, Mapping) or dict(observed) != expected:
        raise ReleaseTransitionError("delivery receipt basis reference differs")
    return expected


def _lineage_path(root: Path, value: Mapping[str, Any]) -> Path:
    release_sha256 = _sha256_text(
        value.get("current_release_manifest_sha256"),
        "lineage current release manifest",
    )
    digest = _sha256_bytes(_canonical_bytes(value))
    return (
        root.resolve(strict=True)
        / LINEAGE_ROOT
        / release_sha256
        / f"{digest}.json"
    )


def validate_publication_lineage(
    root: Path,
    value: Mapping[str, Any],
    *,
    release: Any,
) -> dict[str, Any]:
    required = {
        "schema",
        "recorded_at_unix_ns",
        "code_source_commit",
        "release_commit",
        "release_tree",
        "current_release_manifest_sha256",
        "delivery_basis",
        "candidate_recomposed",
        "tracked_clean",
    }
    row = dict(value)
    if set(row) != required or row.get("schema") != LINEAGE_SCHEMA:
        raise ReleaseTransitionError("publication lineage fields or schema differ")
    recorded_at = row["recorded_at_unix_ns"]
    if (
        isinstance(recorded_at, bool)
        or not isinstance(recorded_at, int)
        or recorded_at <= 0
        or row.get("candidate_recomposed") is not True
        or row.get("tracked_clean") is not True
        or _sha256_text(
            row["current_release_manifest_sha256"],
            "lineage current release manifest",
        )
        != release.manifest_sha256
    ):
        raise ReleaseTransitionError("publication lineage claims differ")
    snapshot = git_snapshot(root)
    if (
        _commit_text(row["release_commit"], "lineage release commit")
        != snapshot["head"]
        or _commit_text(row["release_tree"], "lineage release tree")
        != snapshot["tree"]
    ):
        raise ReleaseTransitionError("publication lineage checkout identity differs")
    _basis_path, basis = load_delivery_basis(root, release=release)
    if (
        _commit_text(row["code_source_commit"], "lineage code source")
        != basis["code_source_commit"]
        or row["delivery_basis"]
        != delivery_basis_reference(root, release=release)
    ):
        raise ReleaseTransitionError("publication lineage delivery basis differs")
    return row


def write_publication_lineage(
    root: Path,
    *,
    release: Any,
) -> tuple[Path, dict[str, Any]]:
    snapshot = git_snapshot(root)
    _basis_path, basis = load_delivery_basis(root, release=release)
    row = {
        "schema": LINEAGE_SCHEMA,
        "recorded_at_unix_ns": time.time_ns(),
        "code_source_commit": basis["code_source_commit"],
        "release_commit": snapshot["head"],
        "release_tree": snapshot["tree"],
        "current_release_manifest_sha256": release.manifest_sha256,
        "delivery_basis": delivery_basis_reference(root, release=release),
        "candidate_recomposed": True,
        "tracked_clean": True,
    }
    path = _lineage_path(root, row)
    encoded = _canonical_bytes(row)
    _write_immutable(path, encoded, "publication lineage")
    return path, validate_publication_lineage(root, row, release=release)


def resolve_publication_lineage(
    root: Path,
    *,
    release: Any,
) -> tuple[Path, dict[str, Any]]:
    experiment = root.resolve(strict=True)
    index = experiment / LINEAGE_ROOT / release.manifest_sha256
    if index.is_symlink() or not index.is_dir():
        raise ReleaseTransitionError(
            "current release has no publication lineage"
        )
    candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
    for path in sorted(index.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            digest = _sha256(path, "publication lineage")
            if path.stem != digest:
                continue
            row = validate_publication_lineage(
                experiment,
                _load(path, "publication lineage"),
                release=release,
            )
        except ReleaseTransitionError:
            continue
        candidates.append(
            (int(row["recorded_at_unix_ns"]), digest, path, row)
        )
    if not candidates:
        raise ReleaseTransitionError(
            "current release has no valid publication lineage"
        )
    _recorded, _digest, path, row = max(candidates)
    return path, row


__all__ = [
    "BASIS_SCHEMA",
    "LINEAGE_SCHEMA",
    "ReleaseTransitionError",
    "create_delivery_basis",
    "delivery_basis_reference",
    "git_snapshot",
    "load_delivery_basis",
    "require_receipt_delivery_basis",
    "resolve_publication_lineage",
    "validate_delivery_basis",
    "validate_publication_lineage",
    "write_publication_lineage",
]
