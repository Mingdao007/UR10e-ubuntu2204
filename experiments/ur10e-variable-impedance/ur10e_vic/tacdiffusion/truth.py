"""Canonical component-level truth for the offline TacDiffusion lane.

This module is intentionally limited to repository-local JSON validation and
view generation.  It has no controller, network, subprocess, or runtime
dependencies.  The canonical JSON source is the only place where current
component truth is authored; the two generated views below are projections of
that source.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Literal, Mapping


CANONICAL_SCHEMA = "ur10e_tacdiffusion_component_truth/v1"
OFFLINE_READINESS_SCHEMA = "ur10e_tacdiffusion_offline_readiness/v1"
CURRENT_VALIDATION_SCHEMA = "ur10e_tacdiffusion_current_validation/v1"
TRUTH_VERSION = "v1"
REVIEW_POLICY_SCHEMA = "ur10e_tacdiffusion_review_policy/v3"

CLAIM_CLASSES = (
    "offline_fixture",
    "live_no_contact_diagnostic",
    "formal_expert_data",
    "formal_checkpoint",
    "model_active",
)
BOUND_STATES = ("bound", "unavailable", "deferred")
COMPONENT_STATUSES = ("accepted", "not_accepted", "unsupported", "deferred")
BINDING_ROOTS = ("experiment", "repository")
BindingRoot = Literal["experiment", "repository"]
_RECORD_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")

COMPONENT_CLAIM_CLASSES = {
    "tracking": "offline_fixture",
    "offline_fixture": "offline_fixture",
    "direct_torque_no_contact_diagnostic": "live_no_contact_diagnostic",
    "internal_wrench_production_conformance": "formal_expert_data",
    "deterministic_expert_dataset": "formal_expert_data",
    "formal_checkpoint": "formal_checkpoint",
    "model_rate_selection": "formal_checkpoint",
    "model_active": "model_active",
}
REQUIRED_COMPONENTS = tuple(COMPONENT_CLAIM_CLASSES)
DIRECT_TORQUE_COMPONENT = "direct_torque_no_contact_diagnostic"
DIRECT_TORQUE_STAGE_RECORD_ID = "step5d_direct_torque_remote_live_v4"
DIRECT_TORQUE_STAGE_TABLE_PATH = (
    "experiments/tase-contact-reproduction/config/step5_stage_table.json"
)
DIRECT_TORQUE_ROOT_CAUSE_PATH = (
    "experiments/tase-contact-reproduction/config/direct_torque_v4_offline_root_cause_20260728.json"
)

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRUTH_SOURCE = DEFAULT_REPO_ROOT / "config" / "tacdiffusion_component_truth_v1.json"
DEFAULT_READINESS_VIEW = DEFAULT_REPO_ROOT / "OFFLINE_READINESS.json"
DEFAULT_CURRENT_VALIDATION_VIEW = (
    DEFAULT_REPO_ROOT / "config" / "tacdiffusion_current_validation.json"
)


class TruthContractError(ValueError):
    """Raised when canonical truth is malformed or internally contradictory."""


@dataclass(frozen=True)
class TruthValidation:
    """Small deterministic summary returned by :func:`validate_truth_document`."""

    schema: str
    truth_version: str
    component_statuses: tuple[tuple[str, str], ...]

    @property
    def accepted_components(self) -> tuple[str, ...]:
        return tuple(
            component
            for component, status in self.component_statuses
            if status == "accepted"
        )


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a local file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TruthContractError(f"{name} must be an object")
    return value


def _nonempty_string(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TruthContractError(f"{name} must be a non-empty string")
    return value


def _sha256(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise TruthContractError(f"{name} must be a 64-character SHA-256 digest")
    if value.lower() != value or any(character not in "0123456789abcdef" for character in value):
        raise TruthContractError(f"{name} must be lowercase hexadecimal SHA-256")
    return value


def _relative_path(value: Any, *, name: str) -> str:
    path = _nonempty_string(value, name=name).replace("\\", "/")
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or PureWindowsPath(path).is_absolute()
        or ".." in parsed.parts
        or path.startswith("/")
    ):
        raise TruthContractError(f"{name} must be repository-relative")
    if ".codex-worktrees" in parsed.parts:
        raise TruthContractError(f"{name} must not bind a temporary worktree")
    normalized = parsed.as_posix()
    if normalized == "." or normalized != path:
        raise TruthContractError(f"{name} must be normalized repository-relative")
    return normalized


def _binding_root(
    binding: Mapping[str, Any], *, component: str, field: str
) -> BindingRoot:
    value = binding.get("root", "experiment")
    if value not in BINDING_ROOTS:
        raise TruthContractError(
            f"{component}.{field}.root must be experiment or repository"
        )
    return value  # type: ignore[return-value]


def _record_id(
    binding: Mapping[str, Any], *, component: str, field: str
) -> str | None:
    value = binding.get("record_id")
    if value is None:
        return None
    if binding.get("kind") != "evidence":
        raise TruthContractError(
            f"{component}.{field}.record_id is only valid for evidence bindings"
        )
    record_id = _nonempty_string(value, name=f"{component}.{field}.record_id")
    if _RECORD_ID.fullmatch(record_id) is None:
        raise TruthContractError(
            f"{component}.{field}.record_id contains unsafe selector characters"
        )
    return record_id


def _binding_base(
    repo_root: Path, *, root: BindingRoot, component: str, field: str
) -> Path:
    experiment_root = repo_root.resolve()
    if root == "experiment":
        return experiment_root
    repository_root = experiment_root.parent.parent
    expected_experiment_root = (
        repository_root / "experiments" / "ur10e-variable-impedance"
    ).resolve()
    if expected_experiment_root != experiment_root:
        raise TruthContractError(
            f"{component}.{field}.root=repository requires the experiment root layout"
        )
    return repository_root


def _binding_candidate(
    path: str,
    *,
    repo_root: Path,
    root: BindingRoot,
    component: str,
    field: str,
) -> Path:
    base = _binding_base(repo_root, root=root, component=component, field=field)
    candidate = (base / path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise TruthContractError(
            f"{component}.{field}.path escapes its {root} binding root"
        ) from exc
    return candidate


def _find_json_record(
    path: Path, *, record_id: str, component: str, field: str
) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TruthContractError(
            f"{component}.{field}.record_id source is not valid JSON"
        ) from exc
    matches: list[Mapping[str, Any]] = []

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            if item.get("id") == record_id:
                matches.append(item)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    if len(matches) != 1:
        raise TruthContractError(
            f"{component}.{field}.record_id must resolve to exactly one tracked JSON record"
        )
    return matches[0]


def _verify_bound_file(
    binding: Mapping[str, Any],
    *,
    repo_root: Path | None,
    component: str,
    field: str,
) -> None:
    state = binding.get("state")
    identity = _nonempty_string(binding.get("identity"), name=f"{component}.{field}.identity")
    root = _binding_root(binding, component=component, field=field)
    record_id = _record_id(binding, component=component, field=field)
    binding_class = _nonempty_string(
        binding.get("claim_class"), name=f"{component}.{field}.claim_class"
    )
    if binding_class not in CLAIM_CLASSES:
        raise TruthContractError(
            f"{component}.{field} uses unknown claim class {binding_class!r}"
        )
    if state not in BOUND_STATES:
        raise TruthContractError(
            f"{component}.{field}.state must be bound, unavailable, or deferred"
        )

    provenance = _nonempty_string(
        binding.get("provenance"), name=f"{component}.{field}.provenance"
    )
    if provenance not in {"repository", "synthetic", "external_deferred"}:
        raise TruthContractError(
            f"{component}.{field}.provenance is not a supported provenance"
        )

    if state == "bound":
        path = _relative_path(binding.get("path"), name=f"{component}.{field}.path")
        expected_identity = path if record_id is None else f"{path}#{record_id}"
        if identity != expected_identity:
            raise TruthContractError(
                f"{component}.{field}.identity must equal its normalized path identity"
            )
        _sha256(binding.get("sha256"), name=f"{component}.{field}.sha256")
        if provenance == "external_deferred":
            raise TruthContractError(
                f"{component}.{field} cannot mark a bound file external_deferred"
            )
        if repo_root is None:
            return
        candidate = _binding_candidate(
            path,
            repo_root=repo_root,
            root=root,
            component=component,
            field=field,
        )
        if not candidate.is_file():
            raise TruthContractError(
                f"{component}.{field}.path does not exist: {path}"
            )
        actual = sha256_file(candidate)
        if actual != binding["sha256"]:
            raise TruthContractError(
                f"{component}.{field}.sha256 does not match {path}"
            )
        if record_id is not None:
            _find_json_record(
                candidate,
                record_id=record_id,
                component=component,
                field=field,
            )
        return

    if binding.get("path") is not None or binding.get("sha256") is not None:
        raise TruthContractError(
            f"{component}.{field} unavailable/deferred bindings cannot carry path or sha256"
        )
    if record_id is not None:
        raise TruthContractError(
            f"{component}.{field} unavailable/deferred bindings cannot carry record_id"
        )
    reason = _nonempty_string(binding.get("reason"), name=f"{component}.{field}.reason")
    if state == "unavailable" and provenance == "synthetic":
        raise TruthContractError(
            f"{component}.{field} unavailable evidence cannot be synthetic"
        )
    if not reason:
        raise TruthContractError(f"{component}.{field} requires a deferred reason")


def _validate_policy_binding(document: Mapping[str, Any], repo_root: Path | None) -> None:
    policy = _mapping(document.get("review_policy"), name="review_policy")
    path = _relative_path(policy.get("path"), name="review_policy.path")
    if path != "config/tacdiffusion_review_policy_v3.json":
        raise TruthContractError("current truth must bind review_policy_v3")
    if policy.get("schema") != REVIEW_POLICY_SCHEMA:
        raise TruthContractError("current truth must use review_policy_v3 schema")
    _sha256(policy.get("sha256"), name="review_policy.sha256")
    if repo_root is None:
        return
    policy_path = repo_root / path
    if not policy_path.is_file():
        raise TruthContractError("bound review_policy_v3 file is missing")
    if sha256_file(policy_path) != policy["sha256"]:
        raise TruthContractError("review_policy_v3 digest does not match")
    policy_document = _mapping(
        json.loads(policy_path.read_text(encoding="utf-8")),
        name="review_policy_v3",
    )
    if policy_document.get("schema") != REVIEW_POLICY_SCHEMA:
        raise TruthContractError("review_policy_v3 file has an unexpected schema")
    if policy_document.get("effective") is not True:
        raise TruthContractError("review_policy_v3 must be effective")
    if policy_document.get("historical_artifacts_are_not_rewritten") is not True:
        raise TruthContractError(
            "review_policy_v3 must preserve historical artifacts"
        )


def _validate_component(
    component: str,
    record: Mapping[str, Any],
    *,
    repo_root: Path | None,
) -> None:
    expected_class = COMPONENT_CLAIM_CLASSES[component]
    claim_class = _nonempty_string(
        record.get("claim_class"), name=f"components.{component}.claim_class"
    )
    if claim_class not in CLAIM_CLASSES:
        raise TruthContractError(
            f"components.{component} uses unknown claim class {claim_class!r}"
        )
    if claim_class != expected_class:
        raise TruthContractError(
            f"components.{component} must use claim class {expected_class!r}"
        )
    claim_classes = record.get("claim_classes")
    if claim_classes != [claim_class]:
        raise TruthContractError(
            f"components.{component}.claim_classes must explicitly contain only its claim_class"
        )
    status = record.get("status")
    if status not in COMPONENT_STATUSES:
        raise TruthContractError(
            f"components.{component}.status is not a known component status"
        )
    accepted = record.get("accepted")
    if not isinstance(accepted, bool) or accepted != (status == "accepted"):
        raise TruthContractError(
            f"components.{component} has contradictory status and accepted fields"
        )
    fixture_only = record.get("fixture_only")
    if not isinstance(fixture_only, bool):
        raise TruthContractError(f"components.{component}.fixture_only must be boolean")
    if fixture_only != (claim_class == "offline_fixture"):
        raise TruthContractError(
            f"components.{component}.fixture_only contradicts its claim class"
        )
    promotion_targets = record.get("promotion_targets")
    if not isinstance(promotion_targets, list) or any(
        target not in CLAIM_CLASSES for target in promotion_targets
    ):
        raise TruthContractError(
            f"components.{component}.promotion_targets contains an unknown claim class"
        )
    if len(set(promotion_targets)) != len(promotion_targets):
        raise TruthContractError(
            f"components.{component}.promotion_targets must be deterministic and unique"
        )
    if fixture_only and promotion_targets:
        raise TruthContractError("offline fixtures cannot promote to production claims")
    if component == DIRECT_TORQUE_COMPONENT and promotion_targets:
        raise TruthContractError(
            "Direct Torque evidence cannot promote beyond live_no_contact_diagnostic"
        )

    sources = record.get("source_bindings")
    evidence = record.get("evidence_bindings")
    if not isinstance(sources, list) or not sources:
        raise TruthContractError(f"components.{component}.source_bindings is required")
    if not isinstance(evidence, list) or not evidence:
        raise TruthContractError(f"components.{component}.evidence_bindings is required")
    for index, binding in enumerate(sources):
        item = _mapping(binding, name=f"components.{component}.source_bindings[{index}]")
        if item.get("kind") != "source":
            raise TruthContractError(
                f"components.{component}.source_bindings[{index}] kind must be source"
            )
        _verify_bound_file(
            item,
            repo_root=repo_root,
            component=component,
            field=f"source_bindings[{index}]",
        )
        if item.get("claim_class") != claim_class:
            raise TruthContractError(
                f"components.{component}.source_bindings[{index}] claim class mismatch"
            )
    for index, binding in enumerate(evidence):
        item = _mapping(binding, name=f"components.{component}.evidence_bindings[{index}]")
        if item.get("kind") != "evidence":
            raise TruthContractError(
                f"components.{component}.evidence_bindings[{index}] kind must be evidence"
            )
        _verify_bound_file(
            item,
            repo_root=repo_root,
            component=component,
            field=f"evidence_bindings[{index}]",
        )
        if item.get("claim_class") != claim_class:
            raise TruthContractError(
                f"components.{component}.evidence_bindings[{index}] claim class mismatch"
            )
        if item.get("provenance") == "synthetic" and claim_class != "offline_fixture":
            raise TruthContractError(
                f"synthetic evidence cannot support {claim_class}"
            )

    if component == DIRECT_TORQUE_COMPONENT and accepted:
        direct_paths = {
            item.get("path")
            for item in evidence
            if item.get("state") == "bound"
        }
        required_paths = {
            DIRECT_TORQUE_STAGE_TABLE_PATH,
            DIRECT_TORQUE_ROOT_CAUSE_PATH,
        }
        if not required_paths.issubset(direct_paths):
            raise TruthContractError(
                "accepted Direct Torque evidence must bind the tracked stage table and offline root-cause summary"
            )
        stage_binding = next(
            (
                item
                for item in evidence
                if item.get("path") == DIRECT_TORQUE_STAGE_TABLE_PATH
                and item.get("record_id") == DIRECT_TORQUE_STAGE_RECORD_ID
            ),
            None,
        )
        if stage_binding is None:
            raise TruthContractError(
                "accepted Direct Torque evidence must select the tracked v4 stage record"
            )
        if repo_root is not None:
            stage_path = _binding_candidate(
                DIRECT_TORQUE_STAGE_TABLE_PATH,
                repo_root=repo_root,
                root=_binding_root(
                    stage_binding,
                    component=component,
                    field="stage_table_evidence",
                ),
                component=component,
                field="stage_table_evidence",
            )
            stage_record = _find_json_record(
                stage_path,
                record_id=DIRECT_TORQUE_STAGE_RECORD_ID,
                component=component,
                field="stage_table_evidence",
            )
            current_binding = stage_record.get("current_binding")
            diagnostic = _mapping(
                stage_record.get("diagnostic_extensions"),
                name="Direct Torque stage diagnostic_extensions",
            ).get("entry_excursion_and_motor_diagnostics_20260728")
            diagnostic = _mapping(
                diagnostic,
                name="Direct Torque stage diagnostic extension",
            )
            current_binding = _mapping(
                current_binding,
                name="Direct Torque stage current_binding",
            )
            if not (
                stage_record.get("complete") is True
                and stage_record.get("blocked") is False
                and stage_record.get("active") is False
                and stage_record.get("contact") is False
                and current_binding.get("no_contact_canary_chain_accepted") is True
                and current_binding.get("live_authorized") is False
                and diagnostic.get("claim_class") == "live_no_contact_diagnostic_only"
                and diagnostic.get("contact") is False
                and diagnostic.get("training_dataset") is False
                and isinstance(diagnostic.get("status"), str)
                and diagnostic["status"].startswith("fresh_no_contact_chain_passed")
            ):
                raise TruthContractError(
                    "tracked Direct Torque stage record does not prove the bounded accepted no-contact diagnostic"
                )

            root_cause_binding = next(
                item
                for item in evidence
                if item.get("path") == DIRECT_TORQUE_ROOT_CAUSE_PATH
            )
            root_cause_path = _binding_candidate(
                DIRECT_TORQUE_ROOT_CAUSE_PATH,
                repo_root=repo_root,
                root=_binding_root(
                    root_cause_binding,
                    component=component,
                    field="root_cause_evidence",
                ),
                component=component,
                field="root_cause_evidence",
            )
            try:
                root_cause = json.loads(root_cause_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TruthContractError(
                    "tracked Direct Torque root-cause summary is not valid JSON"
                ) from exc
            root_cause = _mapping(root_cause, name="Direct Torque root-cause summary")
            scope = _mapping(root_cause.get("scope"), name="Direct Torque root-cause scope")
            readiness = _mapping(
                root_cause.get("expert_data_readiness"),
                name="Direct Torque root-cause expert_data_readiness",
            )
            if not (
                scope.get("offline_only") is True
                and scope.get("robot_or_controller_access") is False
                and scope.get("live_writer_access") is False
                and readiness.get("ready") is False
                and readiness.get("training_dataset") is False
            ):
                raise TruthContractError(
                    "tracked Direct Torque root-cause summary has an unsafe promotion state"
                )

    if accepted and any(binding.get("state") != "bound" for binding in evidence):
        raise TruthContractError(
            f"accepted component {component} requires bound evidence"
        )
    truth = _mapping(record.get("truth"), name=f"components.{component}.truth")
    if component == "tracking":
        if truth.get("supported") is not False or truth.get("noise_model") is not None:
            raise TruthContractError("tracking must remain unsupported with null noise")
        if status != "unsupported":
            raise TruthContractError("tracking status must be unsupported")
    elif component == "offline_fixture":
        if truth.get("synthetic") is not True:
            raise TruthContractError("offline_fixture must be explicitly synthetic")
        if truth.get("production_promotion_allowed") is not False:
            raise TruthContractError(
                "offline_fixture production promotion must be false"
            )
    elif component == "internal_wrench_production_conformance":
        if truth.get("production_conformance") is not accepted:
            raise TruthContractError(
                "internal wrench truth contradicts component acceptance"
            )
    elif component == "deterministic_expert_dataset":
        if truth.get("deterministic") is not accepted:
            raise TruthContractError(
                "expert dataset truth contradicts component acceptance"
            )
    elif component == DIRECT_TORQUE_COMPONENT:
        if truth.get("evidence_scope") != "live_no_contact_diagnostic":
            raise TruthContractError(
                "Direct Torque evidence scope must be live_no_contact_diagnostic"
            )
        for field in (
            "promotes_formal_expert_data",
            "promotes_formal_checkpoint",
            "promotes_model_active",
        ):
            if truth.get(field) is not False:
                raise TruthContractError(
                    f"Direct Torque truth cannot promote {field.removeprefix('promotes_')}"
                )
    elif component == "formal_checkpoint":
        if truth.get("formal_checkpoint") is not accepted:
            raise TruthContractError(
                "checkpoint truth contradicts component acceptance"
            )
    elif component == "model_rate_selection":
        selected_rate = record.get("selected_rate_hz")
        if selected_rate is not None and (
            isinstance(selected_rate, bool)
            or not isinstance(selected_rate, int)
            or selected_rate <= 0
        ):
            raise TruthContractError("model_rate_selection.selected_rate_hz is malformed")
        if accepted != (selected_rate is not None):
            raise TruthContractError(
                "model rate selection status contradicts selected_rate_hz"
            )
    elif component == "model_active":
        if truth.get("active") is not accepted:
            raise TruthContractError("model active truth contradicts acceptance")


def _validate_overall(
    overall: Mapping[str, Any], components: Mapping[str, Mapping[str, Any]]
) -> None:
    expected_scope = "UR10e force-domain TacDiffusion adaptation preparation"
    if overall.get("adaptation_scope") != expected_scope:
        raise TruthContractError("adaptation_scope is not the locked UR10e preparation scope")
    if overall.get("reproduction_boundary") != "not Panda 1 kHz paper-exact reproduction":
        raise TruthContractError(
            "reproduction_boundary must exclude Panda 1 kHz paper-exact reproduction"
        )
    if overall.get("reproduction_status") != "not_claimed":
        raise TruthContractError("reproduction_status must remain not_claimed")
    for key in (
        "formal_checkpoint",
        "tracking_supported",
        "internal_wrench_production_conformance",
        "deterministic_expert_dataset",
        "model_active",
        "active",
    ):
        if overall.get(key) is not False:
            raise TruthContractError(f"locked overall claim {key} must be false")
    if overall.get("model_rate_selected_hz") is not None:
        raise TruthContractError("model_rate_selected_hz must remain null")
    if overall.get("tracking_noise_model") is not None:
        raise TruthContractError("tracking_noise_model must remain null")

    if components["tracking"]["accepted"]:
        raise TruthContractError("unsupported tracking cannot be accepted")
    if overall["tracking_supported"] != components["tracking"]["truth"]["supported"]:
        raise TruthContractError("tracking component and overall truth contradict")
    if overall["tracking_noise_model"] != components["tracking"]["truth"]["noise_model"]:
        raise TruthContractError("tracking noise and overall truth contradict")
    for component, overall_key, truth_key in (
        (
            "internal_wrench_production_conformance",
            "internal_wrench_production_conformance",
            "production_conformance",
        ),
        ("deterministic_expert_dataset", "deterministic_expert_dataset", "deterministic"),
        ("formal_checkpoint", "formal_checkpoint", "formal_checkpoint"),
        ("model_active", "model_active", "active"),
    ):
        record = components[component]
        if overall[overall_key] != record["accepted"]:
            raise TruthContractError(
                f"{component} and overall claim {overall_key} contradict"
            )
        if record["truth"][truth_key] != record["accepted"]:
            raise TruthContractError(f"{component} has contradictory truth fields")
    rate = components["model_rate_selection"]
    if rate.get("selected_rate_hz") is not None:
        raise TruthContractError("current model rate selection must remain null")
    if overall["model_rate_selected_hz"] != rate.get("selected_rate_hz"):
        raise TruthContractError("model rate component and overall truth contradict")

    if components["formal_checkpoint"]["accepted"] and not (
        components["deterministic_expert_dataset"]["accepted"]
        and components["internal_wrench_production_conformance"]["accepted"]
    ):
        raise TruthContractError(
            "formal checkpoint cannot be promoted without formal expert data and internal wrench conformance"
        )
    if rate["accepted"] and not components["formal_checkpoint"]["accepted"]:
        raise TruthContractError("model rate cannot be promoted without a formal checkpoint")
    if components["model_active"]["accepted"] and not (
        components["formal_checkpoint"]["accepted"] and rate["accepted"]
    ):
        raise TruthContractError(
            "model active cannot be promoted without formal checkpoint and rate"
        )
    if overall["active"] != components["model_active"]["accepted"]:
        raise TruthContractError("active and model_active component contradict")


def validate_truth_document(
    document: Mapping[str, Any], *, repo_root: Path | None = DEFAULT_REPO_ROOT
) -> TruthValidation:
    """Validate a canonical truth document and return its deterministic summary.

    Passing ``repo_root`` additionally rehashes every bound repository file and
    checks the effective review policy.  Pass ``repo_root=None`` for a pure
    in-memory schema test that deliberately has no repository to rehash.
    """

    root = _mapping(document, name="truth document")
    if root.get("schema") != CANONICAL_SCHEMA:
        raise TruthContractError("unexpected canonical truth schema")
    if root.get("schema_version") != 1 or root.get("truth_version") != TRUTH_VERSION:
        raise TruthContractError("canonical truth version is unsupported")
    if root.get("claim_classes") != list(CLAIM_CLASSES):
        raise TruthContractError("canonical claim class vocabulary is not exact")
    _validate_policy_binding(root, repo_root)

    components_object = _mapping(root.get("components"), name="components")
    if set(components_object) != set(REQUIRED_COMPONENTS):
        missing = sorted(set(REQUIRED_COMPONENTS) - set(components_object))
        extra = sorted(set(components_object) - set(REQUIRED_COMPONENTS))
        raise TruthContractError(
            f"component set mismatch; missing={missing}, extra={extra}"
        )
    components: dict[str, Mapping[str, Any]] = {}
    statuses: list[tuple[str, str]] = []
    for component in REQUIRED_COMPONENTS:
        record = _mapping(components_object[component], name=f"components.{component}")
        _validate_component(component, record, repo_root=repo_root)
        components[component] = record
        statuses.append((component, record["status"]))
    _validate_overall(_mapping(root.get("overall"), name="overall"), components)
    return TruthValidation(
        schema=CANONICAL_SCHEMA,
        truth_version=TRUTH_VERSION,
        component_statuses=tuple(statuses),
    )


def load_truth_source(
    path: Path = DEFAULT_TRUTH_SOURCE, *, repo_root: Path | None = None
) -> dict[str, Any]:
    """Load and validate the canonical source from disk."""

    source_path = Path(path)
    root = Path(repo_root) if repo_root is not None else source_path.parent.parent
    try:
        document = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TruthContractError(f"cannot load canonical truth source: {source_path}") from exc
    validate_truth_document(document, repo_root=root)
    return document


def _policy_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    policy = _mapping(document["review_policy"], name="review_policy")
    return {
        "schema": policy["schema"],
        "effective": True,
        "path": policy["path"],
        "sha256": policy["sha256"],
        "historical_artifacts_are_not_rewritten": True,
    }


def _source_metadata(document: Mapping[str, Any], repo_root: Path) -> dict[str, Any]:
    source_path = repo_root / "config" / "tacdiffusion_component_truth_v1.json"
    return {
        "path": "config/tacdiffusion_component_truth_v1.json",
        "sha256": sha256_file(source_path),
        "version": document["truth_version"],
    }


def _view_components(document: Mapping[str, Any]) -> dict[str, Any]:
    components = _mapping(document["components"], name="components")
    return {component: deepcopy(components[component]) for component in sorted(components)}


def _accepted_claims(document: Mapping[str, Any]) -> list[dict[str, str]]:
    components = _mapping(document["components"], name="components")
    return [
        {
            "component": component,
            "claim_class": components[component]["claim_class"],
            "status": components[component]["status"],
        }
        for component in sorted(components)
        if components[component]["accepted"]
    ]


def _common_view(
    document: Mapping[str, Any], *, repo_root: Path, schema: str
) -> dict[str, Any]:
    overall = deepcopy(_mapping(document["overall"], name="overall"))
    return {
        "schema": schema,
        "schema_version": 1,
        "truth_version": document["truth_version"],
        "generated_from": {
            "truth_source": _source_metadata(document, repo_root),
            "review_policy": _policy_metadata(document),
            "historical_artifacts_rewritten": False,
        },
        "review_policy": _policy_metadata(document),
        "overall": overall,
        "tacdiffusion": {
            "adaptation_scope": overall["adaptation_scope"],
            "reproduction_boundary": overall["reproduction_boundary"],
            "reproduction_status": overall["reproduction_status"],
            "formal_checkpoint": overall["formal_checkpoint"],
            "model_rate_selected_hz": overall["model_rate_selected_hz"],
            "active": overall["active"],
            "tracking_supported": overall["tracking_supported"],
            "tracking_noise_model": overall["tracking_noise_model"],
            "internal_wrench_production_conformance": overall[
                "internal_wrench_production_conformance"
            ],
            "deterministic_expert_dataset": overall["deterministic_expert_dataset"],
            "model_active": overall["model_active"],
        },
        "components": _view_components(document),
        "accepted_claims": _accepted_claims(document),
        "live_qualification": "external_deferred",
        "external_deferred": True,
    }


def _blockers(document: Mapping[str, Any]) -> list[str]:
    return [
        "tracking is unsupported and its noise model is null",
        "internal wrench production conformance is not accepted",
        "no deterministic formal expert dataset is accepted",
        "no formal checkpoint is accepted",
        "no formal model rate is selected",
        "model active is false",
        "all live and robot qualification remains external_deferred",
    ]


def build_offline_readiness(
    document: Mapping[str, Any], *, repo_root: Path = DEFAULT_REPO_ROOT
) -> dict[str, Any]:
    """Build the deterministic ``OFFLINE_READINESS`` projection."""

    validate_truth_document(document, repo_root=repo_root)
    view = _common_view(document, repo_root=repo_root, schema=OFFLINE_READINESS_SCHEMA)
    view.update(
        {
            "readiness_status": "offline_component_truth_validated_not_claimed",
            "claim_state": {
                "reproduction_status": document["overall"]["reproduction_status"],
                "formal_checkpoint": document["overall"]["formal_checkpoint"],
                "model_rate_selected_hz": document["overall"]["model_rate_selected_hz"],
                "active": document["overall"]["active"],
            },
            "blockers": _blockers(document),
            "validation": {
                "canonical_truth_valid": True,
                "source_bindings_rehashed": True,
                "deterministic": True,
                "offline_only": True,
            },
        }
    )
    return view


def build_current_validation(
    document: Mapping[str, Any], *, repo_root: Path = DEFAULT_REPO_ROOT
) -> dict[str, Any]:
    """Build the deterministic current validation projection."""

    validate_truth_document(document, repo_root=repo_root)
    view = _common_view(document, repo_root=repo_root, schema=CURRENT_VALIDATION_SCHEMA)
    components = _mapping(document["components"], name="components")
    not_accepted = [
        {
            "component": component,
            "claim_class": components[component]["claim_class"],
            "status": components[component]["status"],
        }
        for component in sorted(components)
        if not components[component]["accepted"]
    ]
    view.update(
        {
            "validation_status": "validated_not_claimed",
            "not_accepted_claims": not_accepted,
            "direct_torque_claim_boundary": {
                "component": DIRECT_TORQUE_COMPONENT,
                "allowed_claim_class": "live_no_contact_diagnostic",
                "promotion_targets": [],
            },
            "validation": {
                "canonical_truth_valid": True,
                "source_bindings_rehashed": True,
                "review_policy_v3_effective": True,
                "legacy_review_policy_v2_effective": False,
                "deterministic": True,
                "offline_only": True,
            },
        }
    )
    return view


def render_json(document: Mapping[str, Any]) -> str:
    """Render a view with stable key and whitespace ordering."""

    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def generate_current_views(
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    truth_path: Path | None = None,
    readiness_path: Path | None = None,
    current_validation_path: Path | None = None,
    write: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate or check the two current views without touching historical evidence."""

    root = Path(repo_root)
    source = Path(truth_path) if truth_path is not None else root / "config" / "tacdiffusion_component_truth_v1.json"
    readiness_target = (
        Path(readiness_path) if readiness_path is not None else root / "OFFLINE_READINESS.json"
    )
    validation_target = (
        Path(current_validation_path)
        if current_validation_path is not None
        else root / "config" / "tacdiffusion_current_validation.json"
    )
    document = load_truth_source(source, repo_root=root)
    readiness = build_offline_readiness(document, repo_root=root)
    validation = build_current_validation(document, repo_root=root)
    if write:
        readiness_target.write_text(render_json(readiness), encoding="utf-8")
        validation_target.write_text(render_json(validation), encoding="utf-8")
    return readiness, validation


def check_current_views(
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    truth_path: Path | None = None,
    readiness_path: Path | None = None,
    current_validation_path: Path | None = None,
) -> None:
    """Raise if checked-in current views differ from canonical generated output."""

    root = Path(repo_root)
    readiness_target = (
        Path(readiness_path) if readiness_path is not None else root / "OFFLINE_READINESS.json"
    )
    validation_target = (
        Path(current_validation_path)
        if current_validation_path is not None
        else root / "config" / "tacdiffusion_current_validation.json"
    )
    readiness, validation = generate_current_views(
        repo_root=root,
        truth_path=truth_path,
        readiness_path=readiness_target,
        current_validation_path=validation_target,
        write=False,
    )
    expected = (
        (readiness_target, render_json(readiness)),
        (validation_target, render_json(validation)),
    )
    for path, content in expected:
        if not path.is_file():
            raise TruthContractError(f"generated current view is missing: {path}")
        if path.read_text(encoding="utf-8") != content:
            raise TruthContractError(f"generated current view is stale: {path}")


__all__ = [
    "CANONICAL_SCHEMA",
    "CLAIM_CLASSES",
    "CURRENT_VALIDATION_SCHEMA",
    "DEFAULT_CURRENT_VALIDATION_VIEW",
    "DEFAULT_READINESS_VIEW",
    "DEFAULT_REPO_ROOT",
    "DEFAULT_TRUTH_SOURCE",
    "OFFLINE_READINESS_SCHEMA",
    "TruthContractError",
    "TruthValidation",
    "build_current_validation",
    "build_offline_readiness",
    "check_current_views",
    "generate_current_views",
    "load_truth_source",
    "render_json",
    "sha256_file",
    "validate_truth_document",
]
