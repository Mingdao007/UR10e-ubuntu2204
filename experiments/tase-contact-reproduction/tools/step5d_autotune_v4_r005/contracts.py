"""Typed, content-addressed V4 r005 campaign contract."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_managed_runtime import (
    CONTROL_PROFILE,
    CONTROL_VERIFICATION_MODE,
    MANIFEST_SCHEMA,
    ManagedRuntimeError,
    ManagedRuntimeManifest,
    load_runtime_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r005.json"
PROGRAM = "step5d_strict_rnn_autotune_v4_r005"
LINEAGE = "step5d_strict_rnn_autotune_v4"
TARGET_FORCE_N = 5.0
D_ANCHOR = 28.0
P_ANCHOR = 0.0003535533906
I_ON_ANCHOR = 0.00001
TAU_ANCHOR = 0.35
KO_ANCHOR = 0.1
KP_ANCHOR = 1.5
STEP_OCTAVE = 0.25
I_GRID = (-1.0, -0.75, -0.5, -0.25, 0.0)
NAMED_DIMENSIONS = (
    "P",
    "D",
    "tau",
    "I_on_log2",
    "I_off",
    "Ko",
    "Kp",
)
RUNTIME_MANIFEST_PATH = ROOT / "config/step5d/autotune_v4_r005_runtime_manifest.json"
PATH_REFERENCE_MODULE = "tools/step5d_autotune_v4_r004/path_reference.py"
PATH_REFERENCE_STAGE_ID = "step5d_strict_rnn_autotune_v1"
PATH_REFERENCE_STAGE_ROW_SHA256 = (
    "3ba6468db434b6c5e5eca7ca387d01ad608d195f507caec27f56f081af40a925"
)
PATH_REFERENCE_FRAME_SHA256 = (
    "0a9b800b718c4058340961a4164eb2e5e48b1225b5c5b89f83f6da3a96dbfd82"
)
FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT = (
    "r005.force-mae-v2|stage=25|formal=[5,60)|legacy-shadow=[0,55)|"
    "bin=0.1s|bins=550|stat=mean(abs(mean(force_in_bin)-5N))|"
    "legacy=shadow-only"
)
FORCE_OBJECTIVE_RECEIPT_VERSION = "r005-sealed-sufficient-statistics-v1"

# This is the V4 domain, not the old V3 producer envelope.  Every value is a
# named 0.25-octave lattice point.  D=28 is only the immutable incumbent
# anchor; D is otherwise a searchable coordinate in this bounded V4 domain.
DOMAIN_EXPONENTS = {
    "P": tuple(-0.5 + STEP_OCTAVE * index for index in range(6)),
    "D": tuple(-0.5 + STEP_OCTAVE * index for index in range(5)),
    "tau": tuple(-0.5 + STEP_OCTAVE * index for index in range(5)),
    "Ko": tuple(STEP_OCTAVE * index for index in range(5)),
    "Kp": tuple(STEP_OCTAVE * index for index in range(5)),
}


class R005ContractError(RuntimeError):
    """A typed r005 contract, parent binding, or candidate is invalid."""


@dataclass(frozen=True)
class ControlRuntimeDeclaration:
    """Contract-level admission requirements for the managed control child."""

    schema: str
    managed_v3_profile: str
    verification_mode: str
    launcher_path: str
    required_imports: tuple[str, ...]
    required_ros_pythonpath: tuple[str, ...]
    required_ros_package: str
    optimizer_runtime_separate: bool
    no_system_install: bool


def _runtime_manifest_declaration(document: Mapping[str, Any]) -> ManagedRuntimeManifest:
    value = document.get("runtime_manifest")
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise R005ContractError("r005 managed runtime manifest declaration is missing")
    manifest_path = value.get("path")
    if not isinstance(manifest_path, str) or not manifest_path:
        raise R005ContractError("r005 runtime manifest path is invalid")
    selected = (ROOT / manifest_path).resolve()
    expected_owner = (ROOT / "config/step5d/autotune_v4_r005_runtime_manifest.json").resolve()
    if selected != expected_owner:
        raise R005ContractError("r005 runtime manifest path is not the declared owner")
    try:
        manifest = load_runtime_manifest(
            selected,
            root=ROOT,
            expected_sha256=value.get("sha256"),
        )
    except ManagedRuntimeError as exc:
        raise R005ContractError(f"r005 runtime manifest is invalid: {exc}") from exc
    if manifest.release_contract_path != "config/step5d/autotune_v4_r005.json":
        raise R005ContractError("r005 runtime manifest release contract path differs")
    if (
        manifest.release_contract_schema != document.get("schema")
        or manifest.release_program != document.get("program")
        or manifest.release_revision != document.get("revision")
    ):
        raise R005ContractError("r005 runtime manifest release identity differs")
    return manifest


def _control_runtime_declaration(manifest: ManagedRuntimeManifest) -> ControlRuntimeDeclaration:
    if len(manifest.control_required_ros_packages) != 1:
        raise R005ContractError("r005 compatibility control declaration requires one ROS package")
    return ControlRuntimeDeclaration(
        schema=MANIFEST_SCHEMA,
        managed_v3_profile=CONTROL_PROFILE,
        verification_mode=CONTROL_VERIFICATION_MODE,
        launcher_path=manifest.launcher_path,
        required_imports=manifest.control_required_imports,
        required_ros_pythonpath=manifest.control_required_ros_pythonpath,
        required_ros_package=manifest.control_required_ros_packages[0],
        optimizer_runtime_separate=True,
        no_system_install=True,
    )


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise R005ContractError(f"source is not a regular file: {path}")
    return sha256_bytes(path.read_bytes())


def _read_contract_document(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R005ContractError(f"r005 contract must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R005ContractError(f"r005 contract is unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise R005ContractError("r005 contract must be an object")
    return document


def _parent_entries(document: Mapping[str, Any]) -> dict[str, str]:
    parent = document.get("derived_from_r004")
    if not isinstance(parent, dict) or not parent:
        raise R005ContractError("r005 content-addressed r004 parent map is missing")
    entries: dict[str, str] = {}
    for raw_path, raw_hash in parent.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise R005ContractError("r004 parent path is invalid")
        entries[raw_path] = _digest(raw_hash, f"r004 parent {raw_path}")
    return entries


def _current_parent_digests(document: Mapping[str, Any]) -> dict[str, str]:
    actual: dict[str, str] = {}
    for raw_path in _parent_entries(document):
        source = (ROOT / raw_path).resolve()
        if ROOT not in source.parents or source.is_symlink() or not source.is_file():
            raise R005ContractError(f"r004 parent source is unavailable: {raw_path}")
        actual[raw_path] = sha256_file(source)
    return actual


def refresh_r004_parent_digests(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a contract document with every declared r004 parent rehashed.

    This is deliberately pure.  The explicit rebuild command owns the only
    write path; ordinary contract loading remains read-only and fail-closed.
    """

    refreshed = dict(document)
    refreshed["derived_from_r004"] = _current_parent_digests(document)
    return refreshed


def refresh_contract_document(path: Path = CONTRACT_PATH) -> tuple[dict[str, Any], bytes]:
    """Prepare refreshed contract bytes without changing the contract file."""

    document = _read_contract_document(Path(path))
    refreshed = refresh_r004_parent_digests(document)
    encoded = (json.dumps(refreshed, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return refreshed, encoded


def _stage_bytes(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _replace_staged(path: Path, temporary: Path) -> None:
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def rebuild_contract(path: Path = CONTRACT_PATH) -> "R005Contract":
    """Explicitly refresh parent digests, atomically write, and strictly load."""

    path = Path(path)
    _, encoded = refresh_contract_document(path)
    temporary = _stage_bytes(path, encoded)
    try:
        # Validate the complete refreshed document before replacing the live
        # contract.  A malformed non-parent field therefore cannot be written
        # by the explicit repair path.
        load_contract(temporary)
        _replace_staged(path, temporary)
    finally:
        if temporary.exists():
            temporary.unlink()
    return load_contract(path)


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R005ContractError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise R005ContractError(f"{role} must be finite")
    return result


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R005ContractError(f"{role} must be a lowercase SHA-256")
    return value


def _on_lattice(value: float, anchor: float, exponents: Sequence[float], role: str) -> None:
    if not any(math.isclose(value, anchor * (2.0**power), rel_tol=0.0, abs_tol=1e-12) for power in exponents):
        raise R005ContractError(f"{role} is outside the V4 named domain")


@dataclass(frozen=True)
class Candidate:
    """V4's seven physical fields with typed I-off/I-on encoding."""

    force_p_gain: float = P_ANCHOR
    force_i_gain: float = 0.0
    force_damping: float = D_ANCHOR
    normal_filter_tau_s: float = TAU_ANCHOR
    orientation_ko: float = KO_ANCHOR
    motion_kp: float = KP_ANCHOR
    target_force_n: float = TARGET_FORCE_N

    def __post_init__(self) -> None:
        values = {
            name: _finite(getattr(self, name), name)
            for name in (
                "force_p_gain",
                "force_i_gain",
                "force_damping",
                "normal_filter_tau_s",
                "orientation_ko",
                "motion_kp",
                "target_force_n",
            )
        }
        _on_lattice(values["force_p_gain"], P_ANCHOR, DOMAIN_EXPONENTS["P"], "P")
        _on_lattice(values["force_damping"], D_ANCHOR, DOMAIN_EXPONENTS["D"], "D")
        _on_lattice(values["normal_filter_tau_s"], TAU_ANCHOR, DOMAIN_EXPONENTS["tau"], "tau")
        _on_lattice(values["orientation_ko"], KO_ANCHOR, DOMAIN_EXPONENTS["Ko"], "Ko")
        _on_lattice(values["motion_kp"], KP_ANCHOR, DOMAIN_EXPONENTS["Kp"], "Kp")
        if values["force_i_gain"] != 0.0:
            _on_lattice(values["force_i_gain"], I_ON_ANCHOR, I_GRID, "I_on")
        if not math.isclose(values["target_force_n"], TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12):
            raise R005ContractError("target_force_n is immutable at 5 N")
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def i_off(self) -> bool:
        return self.force_i_gain == 0.0

    @property
    def canonical(self) -> dict[str, Any]:
        return {
            "force_p_gain": self.force_p_gain,
            "force_i_gain": self.force_i_gain,
            "force_damping": self.force_damping,
            "normal_filter_tau_s": self.normal_filter_tau_s,
            "orientation_ko": self.orientation_ko,
            "motion_kp": self.motion_kp,
            "target_force_n": self.target_force_n,
            "i_off": self.i_off,
        }

    @property
    def candidate_uid(self) -> str:
        return sha256_bytes(canonical_bytes(self.canonical))

    @property
    def uid(self) -> str:
        return self.candidate_uid

    @property
    def named7d(self) -> tuple[float, ...]:
        return (
            math.log2(self.force_p_gain / P_ANCHOR),
            math.log2(self.force_damping / D_ANCHOR),
            math.log2(self.normal_filter_tau_s / TAU_ANCHOR),
            0.0 if self.i_off else math.log2(self.force_i_gain / I_ON_ANCHOR),
            1.0 if self.i_off else 0.0,
            math.log2(self.orientation_ko / KO_ANCHOR),
            math.log2(self.motion_kp / KP_ANCHOR),
        )

    @classmethod
    def from_named7d(
        cls, coordinates: Sequence[float], *, target_force_n: float = TARGET_FORCE_N
    ) -> "Candidate":
        if len(coordinates) != 7:
            raise R005ContractError("V4 optimizer coordinate must be named 7D")
        values = tuple(_finite(value, NAMED_DIMENSIONS[index]) for index, value in enumerate(coordinates))
        p, d, tau, i_on, i_off, ko, kp = values
        if i_off not in (0.0, 1.0):
            raise R005ContractError("I_off must be categorical 0 or 1")
        if i_off == 1.0:
            if not math.isclose(i_on, 0.0, rel_tol=0.0, abs_tol=1e-12):
                raise R005ContractError("I-off canonical I_on coordinate must be zero")
            i_gain = 0.0
        else:
            if not any(math.isclose(i_on, power, rel_tol=0.0, abs_tol=1e-12) for power in I_GRID):
                raise R005ContractError("I-on coordinate is outside its fixed grid")
            i_gain = I_ON_ANCHOR * (2.0**i_on)
        _finite(target_force_n, "target_force_n")
        if not math.isclose(target_force_n, TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12):
            raise R005ContractError("decoded target force differs from 5 N")
        return cls(
            force_p_gain=P_ANCHOR * (2.0**p),
            force_i_gain=i_gain,
            force_damping=D_ANCHOR * (2.0**d),
            normal_filter_tau_s=TAU_ANCHOR * (2.0**tau),
            orientation_ko=KO_ANCHOR * (2.0**ko),
            motion_kp=KP_ANCHOR * (2.0**kp),
            target_force_n=target_force_n,
        )


def changed_physical_coordinates(previous: Candidate, current: Candidate) -> tuple[str, ...]:
    if not isinstance(previous, Candidate) or not isinstance(current, Candidate):
        raise R005ContractError("transition operands must be Candidate values")
    if not math.isclose(previous.target_force_n, current.target_force_n, rel_tol=0.0, abs_tol=1e-12):
        raise R005ContractError("target force cannot change")
    changed: list[str] = []
    for name in (
        "force_p_gain",
        "force_i_gain",
        "force_damping",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
    ):
        if not math.isclose(getattr(previous, name), getattr(current, name), rel_tol=0.0, abs_tol=1e-14):
            changed.append(name)
    return tuple(changed)


def validate_transition(previous: Candidate, current: Candidate) -> tuple[str, ...]:
    """Allow a hold or one physical 0.25-octave transition only."""

    changed = changed_physical_coordinates(previous, current)
    if len(changed) > 1:
        raise R005ContractError("dispatch transition changes more than one physical coordinate")
    if not changed:
        return changed
    field = changed[0]
    if field == "force_i_gain" and previous.i_off != current.i_off:
        nonzero = current.force_i_gain if previous.i_off else previous.force_i_gain
        if not math.isclose(nonzero, I_ON_ANCHOR * 0.5, rel_tol=0.0, abs_tol=1e-14):
            raise R005ContractError("I off/on transition must use the first -1 octave point")
        return changed
    before = getattr(previous, field)
    after = getattr(current, field)
    if abs(math.log2(after / before)) > STEP_OCTAVE + 1e-12:
        raise R005ContractError("dispatch transition exceeds 0.25 octave")
    return changed


def bootstrap_pd_candidates() -> tuple[Candidate, ...]:
    """The existing ten P/D bootstrap rows, reproduced by value, not identity."""

    base = Candidate()
    p_minus = replace(base, force_p_gain=P_ANCHOR * (2.0**-0.25))
    p_plus = replace(base, force_p_gain=P_ANCHOR * (2.0**0.25))
    d_minus = replace(base, force_damping=D_ANCHOR * (2.0**-0.25))
    d_plus = replace(base, force_damping=D_ANCHOR * (2.0**0.25))
    # Keep the existing ten-row P/D multiset while ordering each probe through
    # the anchor.  That preserves the bootstrap rows and makes every physical
    # dispatch transition a single 0.25-octave move.
    return (base, base, base, p_minus, base, p_plus, base, d_minus, base, d_plus)


def full_domain() -> tuple[Candidate, ...]:
    """Enumerate the full V4 named-7D lattice without target force."""

    rows: list[Candidate] = []
    for p in DOMAIN_EXPONENTS["P"]:
        for d in DOMAIN_EXPONENTS["D"]:
            for tau in DOMAIN_EXPONENTS["tau"]:
                for i_off in (True, False):
                    for i in (0.0,) if i_off else I_GRID:
                        for ko in DOMAIN_EXPONENTS["Ko"]:
                            for kp in DOMAIN_EXPONENTS["Kp"]:
                                rows.append(
                                    Candidate(
                                        force_p_gain=P_ANCHOR * (2.0**p),
                                        force_i_gain=0.0 if i_off else I_ON_ANCHOR * (2.0**i),
                                        force_damping=D_ANCHOR * (2.0**d),
                                        normal_filter_tau_s=TAU_ANCHOR * (2.0**tau),
                                        orientation_ko=KO_ANCHOR * (2.0**ko),
                                        motion_kp=KP_ANCHOR * (2.0**kp),
                                    )
                                )
    return tuple(sorted(set(rows), key=lambda candidate: candidate.candidate_uid))


@dataclass(frozen=True)
class R005Contract:
    path: Path
    sha256: str
    campaign_fingerprint: str
    r004_parent_sha256: Mapping[str, str]
    runtime_manifest: ManagedRuntimeManifest
    control_runtime: ControlRuntimeDeclaration
    raw: Mapping[str, Any]


def _campaign_fingerprint(document: Mapping[str, Any]) -> str:
    selected = {
        key: document[key]
        for key in (
            "schema",
            "lineage",
            "program",
            "revision",
            "invariants",
            "bootstrap",
            "named_7d_domain",
            "runtime",
            "runtime_manifest",
            "completion",
            "resume",
            "derived_from_r004",
        )
    }
    return sha256_bytes(canonical_bytes(selected))


def load_contract(path: Path = CONTRACT_PATH) -> R005Contract:
    path = Path(path)
    document = _read_contract_document(path)
    if (
        document.get("schema") != "step5d.autotune-v4/r005-release-contract-v1"
        or document.get("lineage") != LINEAGE
        or document.get("program") != PROGRAM
        or document.get("revision") != 5
        or document.get("status") != "offline_candidate_live_blocked"
    ):
        raise R005ContractError("r005 release identity differs")
    invariants = document.get("invariants")
    if not isinstance(invariants, dict):
        raise R005ContractError("r005 invariants are missing")
    if invariants.get("target_force_n") != TARGET_FORCE_N or invariants.get("anchor_damping") != D_ANCHOR:
        raise R005ContractError("r005 target force or anchor D differs")
    if invariants.get("old_r004_qualifications_imported") is not False:
        raise R005ContractError("r004 qualifications must be audit-only")
    eoat = invariants.get("eoat_profile")
    if not isinstance(eoat, dict) or not isinstance(eoat.get("path"), str):
        raise R005ContractError("r005 EOAT profile binding is missing")
    eoat_hash = _digest(eoat.get("sha256"), "r005 eoat_profile.sha256")
    eoat_path = (ROOT / eoat["path"]).resolve()
    if ROOT not in eoat_path.parents or eoat_path.is_symlink() or not eoat_path.is_file():
        raise R005ContractError("r005 EOAT profile source is unavailable")
    if sha256_file(eoat_path) != eoat_hash:
        raise R005ContractError("r005 EOAT profile source drift")
    domain = document.get("named_7d_domain")
    if not isinstance(domain, dict) or tuple(domain.get("dimensions", ())) != NAMED_DIMENSIONS:
        raise R005ContractError("r005 named 7D domain differs")
    bootstrap = document.get("bootstrap")
    if not isinstance(bootstrap, dict) or bootstrap.get("qualification_count") != 3 or bootstrap.get("pd_row_count") != 10:
        raise R005ContractError("r005 bootstrap counts differ")
    runtime = document.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("tp_hz") != 500.0 or runtime.get("host_hz") != 500.0:
        raise R005ContractError("r005 cadence differs")
    force_objective = runtime.get("force_objective")
    if (
        not isinstance(force_objective, dict)
        or force_objective.get("schema") != "step5d.force-objective/v2"
        or force_objective.get("version") != "force_mae_v2"
        or force_objective.get("target_force_n") != TARGET_FORCE_N
        or force_objective.get("target_is_optimizer_dimension") is not False
        or force_objective.get("path_stage") != 25
        or force_objective.get("formal_window_s") != [5.0, 60.0]
        or force_objective.get("legacy_window_s") != [0.0, 55.0]
        or force_objective.get("bin_width_s") != 0.1
        or force_objective.get("required_bins") != 550
        or force_objective.get("bin_statistic") != "mean_filtered_normal_then_absolute_error"
    ):
        raise R005ContractError("r005 force objective contract differs")
    if (
        force_objective.get("semantic_fingerprint")
        != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT
        or force_objective.get("receipt_version") != FORCE_OBJECTIVE_RECEIPT_VERSION
        or force_objective.get("receipt_requires_raw_evidence_digest") is not True
        or force_objective.get("receipt_requires_sufficient_statistics_digest") is not True
        or force_objective.get("receipt_requires_builder_seal") is not True
        or force_objective.get("persist_raw_sample_arrays") is not True
        or force_objective.get("persist_raw_sample_identity_ids") is not False
    ):
        raise R005ContractError("r005 sealed force-objective receipt contract differs")
    path_reference = runtime.get("path_reference")
    if (
        not isinstance(path_reference, dict)
        or set(path_reference)
        != {
            "module",
            "stage_id",
            "stage_row_semantic_sha256",
            "frame_semantic_sha256",
        }
        or path_reference.get("module") != PATH_REFERENCE_MODULE
        or path_reference.get("stage_id") != PATH_REFERENCE_STAGE_ID
        or path_reference.get("stage_row_semantic_sha256") != PATH_REFERENCE_STAGE_ROW_SHA256
        or path_reference.get("frame_semantic_sha256") != PATH_REFERENCE_FRAME_SHA256
    ):
        raise R005ContractError("r005 immutable PATH reference binding differs")
    optimizer_runtime = runtime.get("optimizer")
    profile = None if not isinstance(optimizer_runtime, dict) else optimizer_runtime.get("optimizer_profile")
    if (
        not isinstance(profile, dict)
        or profile.get("resolver") != "step5d_optimizer_runtime.resolve_optimizer_runtime"
        or profile.get("canonical_v3_profile") != "optimizer"
        or profile.get("stale_after_s") != 2592000.0
        or profile.get("child_attestation")
        != [
            "environment_id",
            "environment_hash",
            "torch_version",
            "botorch_version",
            "gpytorch_version",
            "cuda_available",
            "gpu_name",
            "gpu_uuid",
        ]
    ):
        raise R005ContractError("r005 optimizer runtime profile declaration differs")
    runtime_manifest = _runtime_manifest_declaration(document)
    control_runtime = _control_runtime_declaration(runtime_manifest)
    completion = document.get("completion")
    if not isinstance(completion, dict) or completion.get("mae_threshold_n") != 0.20 or completion.get("retest_count") != 3:
        raise R005ContractError("r005 completion contract differs")
    parent = _parent_entries(document)
    actual_parent_hashes = _current_parent_digests(document)
    parent_hashes: dict[str, str] = {}
    for raw_path, expected in parent.items():
        if actual_parent_hashes[raw_path] != expected:
            raise R005ContractError(f"r004 parent source drift: {raw_path}")
        parent_hashes[raw_path] = expected
    return R005Contract(
        path=path,
        sha256=sha256_file(path),
        campaign_fingerprint=_campaign_fingerprint(document),
        r004_parent_sha256=parent_hashes,
        runtime_manifest=runtime_manifest,
        control_runtime=control_runtime,
        raw=document,
    )


__all__ = [
    "Candidate",
    "CONTROL_PROFILE",
    "CONTROL_VERIFICATION_MODE",
    "ControlRuntimeDeclaration",
    "CONTRACT_PATH",
    "D_ANCHOR",
    "DOMAIN_EXPONENTS",
    "I_GRID",
    "I_ON_ANCHOR",
    "KP_ANCHOR",
    "KO_ANCHOR",
    "LINEAGE",
    "NAMED_DIMENSIONS",
    "P_ANCHOR",
    "PROGRAM",
    "R005Contract",
    "R005ContractError",
    "RUNTIME_MANIFEST_PATH",
    "STEP_OCTAVE",
    "TARGET_FORCE_N",
    "bootstrap_pd_candidates",
    "changed_physical_coordinates",
    "full_domain",
    "load_contract",
    "rebuild_contract",
    "refresh_contract_document",
    "refresh_r004_parent_digests",
    "sha256_file",
    "validate_transition",
]
