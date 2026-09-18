"""Build the offline native six-law contact benchmark library.

The benchmark uses the reviewed native controller implementation from the
Ubuntu follow-up checkout.  This module copies the four source files into a
content-addressed, temporary build directory before compiling a small C API
wrapper.  The source checkout is never modified and the generated manifest
records the exact source digests used for the build.

The resulting shared object has no network, robot, or endpoint code.  It is a
library for deterministic offline replay; :mod:`contact_laws` provides the
Python/ctypes facade.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterable


DEFAULT_SOURCE_DIR = Path(
    "/home/andy/.codex-worktrees/smysfc-tro-ubuntu-followup-20260906/cpp/controller_suite"
)
DEFAULT_BUILD_ROOT = Path(tempfile.gettempdir()) / "ur10e_contact_laws"
SOURCE_NAMES = (
    "controller_suite.hpp",
    "controller_suite.cpp",
    "native_seven_laws.hpp",
    "native_seven_laws.cpp",
)
WRAPPER_NAME = "contact_laws_c_api.cpp"
LIBRARY_NAME = "libcontact_laws.so"
COMPILE_FLAGS = (
    "-std=c++20",
    "-O2",
    "-fPIC",
    "-fvisibility=hidden",
    "-shared",
)


# The native source owns all numerical formulas.  This wrapper deliberately
# calls the lower-level free functions instead of SevenLawKernel: the latter
# exposes the legacy warm-start path and has no state setter suitable for an
# exact replay restore.
WRAPPER_SOURCE = r'''#include "controller_suite.hpp"
#include "native_seven_laws.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <exception>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>

using sfc_controller_suite::KernelStep;
using sfc_controller_suite::LacParameters;
using sfc_controller_suite::NacParameters;
using sfc_controller_suite::SfcParameters;
using sfc_controller_suite::SmysfcMemory;
using sfc_controller_suite::SmysfcParameters;
using sfc_controller_suite::Vector3;
using sfc_controller_suite::YsfcParameters;
using sfc_controller_suite::IsfcParameters;

#if defined(__GNUC__)
#define CONTACT_LAW_EXPORT __attribute__((visibility("default")))
#else
#define CONTACT_LAW_EXPORT
#endif

namespace {

constexpr std::size_t kSnapshotSize = 22U;
constexpr double kSnapshotMagic = 20260917.0;

enum class Kind : int {
    Lac = 1,
    Nac = 2,
    Sfc = 3,
    Dsfc = 4,
    Isfc = 5,
    Msfc = 6,
};

thread_local std::string g_last_error;

[[noreturn]] void reject(std::string_view detail) {
    throw std::invalid_argument(std::string("contact_law: ") + std::string(detail));
}

void finite(double value, std::string_view name) {
    if (!std::isfinite(value)) {
        reject(std::string(name) + " must be finite");
    }
}

void positive(double value, std::string_view name) {
    finite(value, name);
    if (!(value > 0.0)) {
        reject(std::string(name) + " must be positive");
    }
}

void at_least(double value, double lower, std::string_view name) {
    finite(value, name);
    if (!(value >= lower)) {
        reject(std::string(name) + " is below its lower bound");
    }
}

std::size_t integer_parameter(
    double value,
    std::size_t lower,
    std::size_t upper,
    std::string_view name) {
    finite(value, name);
    if (std::floor(value) != value ||
        value < static_cast<double>(lower) ||
        value > static_cast<double>(upper)) {
        reject(std::string(name) + " must be an integer in the configured bounds");
    }
    return static_cast<std::size_t>(value);
}

double parameter_at(
    const double* parameters,
    std::size_t index,
    std::string_view name) {
    if (parameters == nullptr) {
        reject("parameter array is null");
    }
    const double value = parameters[index];
    finite(value, name);
    return value;
}

void validate_lac(const LacParameters& parameters) {
    positive(parameters.m, "LAC m");
    positive(parameters.mu, "LAC mu");
    positive(parameters.g, "LAC g");
}

void validate_nac(const NacParameters& parameters) {
    positive(parameters.m, "NAC m");
    positive(parameters.mu, "NAC mu");
    positive(parameters.g, "NAC g");
    positive(parameters.alpha, "NAC alpha");
    positive(parameters.sigma, "NAC sigma");
}

void validate_sfc(const SfcParameters& parameters) {
    positive(parameters.m, "SFC m");
    positive(parameters.mu, "SFC mu");
    at_least(parameters.n, 1.0, "SFC n");
    positive(parameters.g, "SFC g");
}

void validate_dsfc(const YsfcParameters& parameters) {
    positive(parameters.m, "DSFC m");
    positive(parameters.g, "DSFC g");
    positive(parameters.p, "DSFC p");
    if (!(parameters.p < 1.0)) {
        reject("DSFC p must be below one");
    }
    positive(parameters.a, "DSFC a");
    if (!(parameters.n > 1.0)) {
        reject("DSFC n must be greater than one");
    }
    positive(parameters.mu, "DSFC mu");
    if (parameters.max_iterations == 0U || parameters.max_iterations > 32U) {
        reject("DSFC max_iterations must be in [1, 32]");
    }
    positive(parameters.residual_tolerance_n, "DSFC residual_tolerance_n");
    positive(parameters.relative_radius_tolerance, "DSFC relative_radius_tolerance");
}

void validate_isfc(const IsfcParameters& parameters) {
    positive(parameters.m, "ISFC m");
    positive(parameters.mu, "ISFC mu");
    if (!(parameters.n > 1.0)) {
        reject("ISFC n must be greater than one");
    }
    positive(parameters.eta0, "ISFC eta0");
    positive(parameters.g, "ISFC g");
    positive(parameters.v_ref, "ISFC v_ref");
    if (parameters.substeps == 0U || parameters.substeps > 1024U) {
        reject("ISFC substeps must be in [1, 1024]");
    }
}

void validate_msfc(const SmysfcParameters& parameters) {
    positive(parameters.m, "MSFC m");
    positive(parameters.g, "MSFC g");
    positive(parameters.p, "MSFC p");
    if (!(parameters.p < 1.0)) {
        reject("MSFC p must be below one");
    }
    positive(parameters.a, "MSFC a");
    if (!(parameters.n > 1.0)) {
        reject("MSFC n must be greater than one");
    }
    positive(parameters.mu, "MSFC mu");
    positive(parameters.force_scale_n, "MSFC force_scale_n");
    positive(parameters.tau_force_s, "MSFC tau_force_s");
    positive(parameters.tau_recovery_s, "MSFC tau_recovery_s");
    positive(parameters.kappa_per_n2_s, "MSFC kappa_per_n2_s");
    if (parameters.max_iterations == 0U || parameters.max_iterations > 32U) {
        reject("MSFC max_iterations must be in [1, 32]");
    }
    positive(parameters.residual_tolerance_n, "MSFC residual_tolerance_n");
    positive(parameters.structure_tolerance, "MSFC structure_tolerance");
    finite(parameters.minimum_metric_eigenvalue, "MSFC minimum_metric_eigenvalue");
    if (parameters.minimum_metric_eigenvalue < 0.0 ||
        parameters.minimum_metric_eigenvalue > 1.0) {
        reject("MSFC minimum_metric_eigenvalue must be in [0, 1]");
    }
}

struct contact_law_handle final {
    Kind kind = Kind::Lac;
    std::size_t dimension = 3U;
    double dt_s = 0.002;
    Vector3 state{};
    LacParameters lac{};
    NacParameters nac{};
    SfcParameters sfc{};
    YsfcParameters dsfc{};
    IsfcParameters isfc{};
    SmysfcParameters msfc{};
    SmysfcMemory msfc_memory{};
    std::string last_error;
};

void clear_error(contact_law_handle* handle) noexcept {
    if (handle != nullptr) {
        handle->last_error.clear();
    } else {
        g_last_error.clear();
    }
}

void save_error(contact_law_handle* handle, std::string message) noexcept {
    if (handle != nullptr) {
        handle->last_error = std::move(message);
    } else {
        g_last_error = std::move(message);
    }
}

template <typename Function>
int guarded(contact_law_handle* handle, Function&& function) noexcept {
    try {
        function();
        clear_error(handle);
        return 0;
    } catch (const std::exception& error) {
        save_error(handle, error.what());
        return -1;
    } catch (...) {
        save_error(handle, "contact_law: unknown native exception");
        return -1;
    }
}

void require_handle(const contact_law_handle* handle) {
    if (handle == nullptr) {
        reject("handle is null");
    }
}

void require_pointer(const void* pointer, std::string_view name) {
    if (pointer == nullptr) {
        reject(std::string(name) + " is null");
    }
}

void require_finite_vector(const Vector3& vector, std::string_view name) {
    for (const double value : vector) {
        finite(value, name);
    }
}

void copy_vector(double* output, const Vector3& vector) {
    std::copy(vector.begin(), vector.end(), output);
}

void require_inactive_zero(double value, std::string_view name) {
    if (value != 0.0) {
        reject(std::string(name) + " has nonzero inactive coordinates");
    }
}

void validate_snapshot_memory(
    const contact_law_handle* handle,
    const Vector3& state,
    const SmysfcMemory& memory) {
    require_finite_vector(state, "snapshot state");
    require_finite_vector(memory.force_history, "snapshot force_history");
    for (const double value : memory.structure) {
        finite(value, "snapshot structure");
    }
    for (const double value : memory.metric_eigenvalues) {
        finite(value, "snapshot metric_eigenvalues");
    }
    const std::size_t dimension = handle->dimension;
    for (std::size_t index = dimension; index < 3U; ++index) {
        require_inactive_zero(state[index], "snapshot state");
        require_inactive_zero(memory.force_history[index], "snapshot force_history");
        require_inactive_zero(memory.metric_eigenvalues[index], "snapshot metric_eigenvalues");
    }
    for (std::size_t row = 0U; row < 3U; ++row) {
        for (std::size_t column = 0U; column < 3U; ++column) {
            const double value = memory.structure[row * 3U + column];
            if (row >= dimension || column >= dimension) {
                require_inactive_zero(value, "snapshot structure");
            }
            if (std::abs(value - memory.structure[column * 3U + row]) > 1e-14) {
                reject("snapshot structure must be symmetric");
            }
        }
    }
}

void fill_snapshot(const contact_law_handle* handle, double* output) {
    require_handle(handle);
    require_pointer(output, "snapshot output");
    validate_snapshot_memory(handle, handle->state, handle->msfc_memory);
    output[0] = kSnapshotMagic;
    output[1] = static_cast<double>(static_cast<int>(handle->kind));
    output[2] = static_cast<double>(handle->dimension);
    output[3] = handle->dt_s;
    std::copy(handle->state.begin(), handle->state.end(), output + 4U);
    std::copy(
        handle->msfc_memory.force_history.begin(),
        handle->msfc_memory.force_history.end(),
        output + 7U);
    std::copy(
        handle->msfc_memory.structure.begin(),
        handle->msfc_memory.structure.end(),
        output + 10U);
    std::copy(
        handle->msfc_memory.metric_eigenvalues.begin(),
        handle->msfc_memory.metric_eigenvalues.end(),
        output + 19U);
}

std::size_t snapshot_integer(double value, std::size_t lower, std::size_t upper, std::string_view name) {
    return integer_parameter(value, lower, upper, name);
}

void restore_snapshot(contact_law_handle* handle, const double* input) {
    require_handle(handle);
    require_pointer(input, "snapshot input");
    for (std::size_t index = 0U; index < kSnapshotSize; ++index) {
        finite(input[index], "snapshot");
    }
    if (input[0] != kSnapshotMagic) {
        reject("snapshot magic does not match this library");
    }
    const std::size_t kind = snapshot_integer(input[1], 1U, 6U, "snapshot law kind");
    const std::size_t dimension = snapshot_integer(input[2], 1U, 3U, "snapshot dimension");
    if (kind != static_cast<std::size_t>(static_cast<int>(handle->kind))) {
        reject("snapshot law kind does not match handle");
    }
    if (dimension != handle->dimension || input[3] != handle->dt_s) {
        reject("snapshot dimension or dt_s does not match handle");
    }

    Vector3 next_state{};
    std::copy(input + 4U, input + 7U, next_state.begin());
    SmysfcMemory next_memory;
    std::copy(input + 7U, input + 10U, next_memory.force_history.begin());
    std::copy(input + 10U, input + 19U, next_memory.structure.begin());
    std::copy(input + 19U, input + 22U, next_memory.metric_eigenvalues.begin());
    validate_snapshot_memory(handle, next_state, next_memory);
    handle->state = next_state;
    handle->msfc_memory = next_memory;
}

KernelStep run_step(
    contact_law_handle* handle,
    const Vector3& force,
    SmysfcMemory& next_memory, double step_dt_s) {
    switch (handle->kind) {
    case Kind::Lac:
        return sfc_controller_suite::lac_step(
            force, handle->state, handle->lac, step_dt_s, handle->dimension);
    case Kind::Nac:
        return sfc_controller_suite::nac_step(
            force, handle->state, handle->nac, step_dt_s, handle->dimension);
    case Kind::Sfc:
        return sfc_controller_suite::sfc_step(
            force, handle->state, handle->sfc, step_dt_s, handle->dimension);
    case Kind::Dsfc:
        return sfc_controller_suite::ysfc_step(
            force, handle->state, handle->dsfc, step_dt_s, handle->dimension);
    case Kind::Isfc:
        return sfc_controller_suite::isfc_step(
            force, handle->state, handle->isfc, step_dt_s, handle->dimension);
    case Kind::Msfc:
        next_memory = handle->msfc_memory;
        return sfc_controller_suite::smysfc_step(
            force,
            handle->state,
            handle->msfc,
            step_dt_s,
            handle->dimension,
            next_memory);
    }
    reject("unsupported law kind");
}

}  // namespace

extern "C" {

CONTACT_LAW_EXPORT contact_law_handle* contact_law_create(
    const char* law,
    std::size_t dimension,
    const double* parameters,
    std::size_t parameter_count,
    double dt_s) noexcept {
    try {
        if (law == nullptr) {
            reject("law label is null");
        }
        if (dimension < 1U || dimension > 3U) {
            reject("dimension must be in [1, 3]");
        }
        positive(dt_s, "dt_s");
        const std::string_view label(law);
        std::unique_ptr<contact_law_handle> holder =
            std::make_unique<contact_law_handle>();
        auto* handle = holder.get();
        handle->dimension = dimension;
        handle->dt_s = dt_s;
        if (label == "LAC") {
            if (parameter_count != 3U) reject("LAC expects 3 parameters");
            handle->kind = Kind::Lac;
            handle->lac = LacParameters{
                parameter_at(parameters, 0U, "LAC m"),
                parameter_at(parameters, 1U, "LAC mu"),
                parameter_at(parameters, 2U, "LAC g")};
            validate_lac(handle->lac);
        } else if (label == "NAC") {
            if (parameter_count != 5U) reject("NAC expects 5 parameters");
            handle->kind = Kind::Nac;
            handle->nac = NacParameters{
                parameter_at(parameters, 0U, "NAC m"),
                parameter_at(parameters, 1U, "NAC mu"),
                parameter_at(parameters, 2U, "NAC g"),
                parameter_at(parameters, 3U, "NAC alpha"),
                parameter_at(parameters, 4U, "NAC sigma")};
            validate_nac(handle->nac);
        } else if (label == "SFC") {
            if (parameter_count != 4U) reject("SFC expects 4 parameters");
            handle->kind = Kind::Sfc;
            handle->sfc = SfcParameters{
                parameter_at(parameters, 0U, "SFC m"),
                parameter_at(parameters, 1U, "SFC mu"),
                parameter_at(parameters, 2U, "SFC n"),
                parameter_at(parameters, 3U, "SFC g")};
            validate_sfc(handle->sfc);
        } else if (label == "DSFC") {
            if (parameter_count != 9U) reject("DSFC expects 9 parameters");
            handle->kind = Kind::Dsfc;
            handle->dsfc = YsfcParameters{
                parameter_at(parameters, 0U, "DSFC m"),
                parameter_at(parameters, 1U, "DSFC g"),
                parameter_at(parameters, 2U, "DSFC p"),
                parameter_at(parameters, 3U, "DSFC a"),
                parameter_at(parameters, 4U, "DSFC n"),
                parameter_at(parameters, 5U, "DSFC mu"),
                integer_parameter(parameter_at(parameters, 6U, "DSFC max_iterations"), 1U, 32U, "DSFC max_iterations"),
                parameter_at(parameters, 7U, "DSFC residual_tolerance_n"),
                parameter_at(parameters, 8U, "DSFC relative_radius_tolerance")};
            validate_dsfc(handle->dsfc);
        } else if (label == "ISFC") {
            if (parameter_count != 7U) reject("ISFC expects 7 parameters");
            handle->kind = Kind::Isfc;
            handle->isfc = IsfcParameters{
                parameter_at(parameters, 0U, "ISFC m"),
                parameter_at(parameters, 1U, "ISFC mu"),
                parameter_at(parameters, 2U, "ISFC n"),
                parameter_at(parameters, 3U, "ISFC eta0"),
                parameter_at(parameters, 4U, "ISFC g"),
                parameter_at(parameters, 5U, "ISFC v_ref"),
                integer_parameter(parameter_at(parameters, 6U, "ISFC substeps"), 1U, 1024U, "ISFC substeps")};
            validate_isfc(handle->isfc);
        } else if (label == "MSFC") {
            if (parameter_count != 14U) reject("MSFC expects 14 parameters");
            handle->kind = Kind::Msfc;
            handle->msfc = SmysfcParameters{
                parameter_at(parameters, 0U, "MSFC m"),
                parameter_at(parameters, 1U, "MSFC g"),
                parameter_at(parameters, 2U, "MSFC p"),
                parameter_at(parameters, 3U, "MSFC a"),
                parameter_at(parameters, 4U, "MSFC n"),
                parameter_at(parameters, 5U, "MSFC mu"),
                parameter_at(parameters, 6U, "MSFC force_scale_n"),
                parameter_at(parameters, 7U, "MSFC tau_force_s"),
                parameter_at(parameters, 8U, "MSFC tau_recovery_s"),
                parameter_at(parameters, 9U, "MSFC kappa_per_n2_s"),
                integer_parameter(parameter_at(parameters, 10U, "MSFC max_iterations"), 1U, 32U, "MSFC max_iterations"),
                parameter_at(parameters, 11U, "MSFC residual_tolerance_n"),
                parameter_at(parameters, 12U, "MSFC structure_tolerance"),
                parameter_at(parameters, 13U, "MSFC minimum_metric_eigenvalue")};
            validate_msfc(handle->msfc);
        } else {
            reject("law must be one of LAC, NAC, SFC, DSFC, ISFC, MSFC");
        }
        clear_error(handle);
        return holder.release();
    } catch (const std::exception& error) {
        save_error(nullptr, error.what());
    } catch (...) {
        save_error(nullptr, "contact_law: unknown native exception");
    }
    return nullptr;
}

CONTACT_LAW_EXPORT int contact_law_step_elapsed(
    contact_law_handle* handle,
    const double* force,
    double step_dt_s,
    double* state,
    double* command,
    double* acceleration) noexcept {
    return guarded(handle, [&]() {
        require_handle(handle);
        if (!std::isfinite(step_dt_s) || step_dt_s <= 0.0) reject("step dt must be finite positive");
        require_pointer(force, "force");
        require_pointer(state, "state output");
        require_pointer(command, "command output");
        require_pointer(acceleration, "acceleration output");
        Vector3 input_force{};
        std::copy(force, force + 3U, input_force.begin());
        require_finite_vector(input_force, "force");

        // MSFC owns a mutable memory object.  Work on a copy and commit both
        // state and memory only after the complete native step succeeds.
        SmysfcMemory next_memory = handle->msfc_memory;
        const KernelStep result = run_step(handle, input_force, next_memory, step_dt_s);
        require_finite_vector(result.xd_next_d_m_s, "native state");
        require_finite_vector(result.command_d_m_s, "native command");
        require_finite_vector(result.xdd_d_m_s2, "native acceleration");
        handle->state = result.xd_next_d_m_s;
        if (handle->kind == Kind::Msfc) {
            handle->msfc_memory = next_memory;
        }
        copy_vector(state, result.xd_next_d_m_s);
        copy_vector(command, result.command_d_m_s);
        copy_vector(acceleration, result.xdd_d_m_s2);
    });
}

CONTACT_LAW_EXPORT int contact_law_step(
    contact_law_handle* handle, const double* force, double* state,
    double* command, double* acceleration) noexcept {
    return contact_law_step_elapsed(handle, force, handle ? handle->dt_s : 0.0,
                                    state, command, acceleration);
}

CONTACT_LAW_EXPORT std::size_t contact_law_snapshot_size() noexcept {
    return kSnapshotSize;
}

CONTACT_LAW_EXPORT int contact_law_snapshot(
    const contact_law_handle* handle,
    double* output,
    std::size_t output_count) noexcept {
    return guarded(const_cast<contact_law_handle*>(handle), [&]() {
        require_handle(handle);
        if (output_count != kSnapshotSize) {
            reject("snapshot output count must be 22");
        }
        fill_snapshot(handle, output);
    });
}

CONTACT_LAW_EXPORT int contact_law_restore(
    contact_law_handle* handle,
    const double* input,
    std::size_t input_count) noexcept {
    return guarded(handle, [&]() {
        require_handle(handle);
        if (input_count != kSnapshotSize) {
            reject("snapshot input count must be 22");
        }
        restore_snapshot(handle, input);
    });
}

CONTACT_LAW_EXPORT int contact_law_reset(contact_law_handle* handle) noexcept {
    return guarded(handle, [&]() {
        require_handle(handle);
        handle->state = Vector3{};
        handle->msfc_memory.reset();
    });
}

CONTACT_LAW_EXPORT const char* contact_law_last_error(const contact_law_handle* handle) noexcept {
    if (handle != nullptr) {
        return handle->last_error.c_str();
    }
    return g_last_error.c_str();
}

CONTACT_LAW_EXPORT void contact_law_destroy(contact_law_handle* handle) noexcept {
    delete handle;
}

}  // extern "C"
'''


@dataclass(frozen=True)
class ContactLawBuild:
    """Paths and provenance for one content-addressed native build."""

    library_path: Path
    build_dir: Path
    provenance_path: Path
    fingerprint: str


class ContactLawBuildError(RuntimeError):
    """Raised when the offline native library cannot be built."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_records(source_dir: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for name in SOURCE_NAMES:
        source_path = source_dir / name
        if not source_path.is_file():
            raise ContactLawBuildError(f"missing native source: {source_path}")
        records.append(
            {
                "filename": name,
                "source_path": str(source_path),
                "sha256": _sha256(source_path),
            }
        )
    return records


def _reference_record(path: Path) -> dict[str, str | bool]:
    record: dict[str, str | bool] = {
        "path": str(path),
        "exists": path.is_file(),
    }
    if path.is_file():
        record["sha256"] = _sha256(path)
    return record


def _cache_manifest_matches(
    manifest: object,
    *,
    fingerprint: str,
    source_records: list[dict[str, str]],
    wrapper_path: Path,
    library_path: Path,
) -> bool:
    if not isinstance(manifest, dict) or manifest.get("fingerprint") != fingerprint:
        return False
    manifest_sources = manifest.get("source_files")
    if not isinstance(manifest_sources, list) or len(manifest_sources) != len(source_records):
        return False
    for expected, recorded in zip(source_records, manifest_sources):
        if not isinstance(recorded, dict):
            return False
        if recorded.get("filename") != expected["filename"] or recorded.get("sha256") != expected["sha256"]:
            return False
        copied = Path(str(recorded.get("copied_path", "")))
        expected_copied = wrapper_path.parent / expected["filename"]
        if copied != expected_copied or not copied.is_file() or _sha256(copied) != expected["sha256"]:
            return False
    wrapper_record = manifest.get("wrapper")
    if (
        not isinstance(wrapper_record, dict)
        or wrapper_record.get("path") != str(wrapper_path)
        or not wrapper_path.is_file()
    ):
        return False
    if wrapper_record.get("sha256") != hashlib.sha256(WRAPPER_SOURCE.encode("utf-8")).hexdigest():
        return False
    if _sha256(wrapper_path) != wrapper_record.get("sha256"):
        return False
    library_record = manifest.get("library")
    return (
        isinstance(library_record, dict)
        and library_path.is_file()
        and library_record.get("sha256") == _sha256(library_path)
    )


def build_shared_library(
    *,
    source_dir: Path | str = DEFAULT_SOURCE_DIR,
    build_root: Path | str | None = None,
    compiler: str = "g++",
    force: bool = False,
    expected_source_hashes: Mapping[str, str] | None = None,
) -> ContactLawBuild:
    """Copy the reviewed sources and compile the offline six-law library.

    The returned directory is named by the source and wrapper content hash.
    Existing matching output is reused; ``force=True`` recompiles that same
    content-addressed output.  No process other than the local compiler is
    started, and no source checkout file is changed.
    """

    source_path = Path(source_dir).expanduser().resolve()
    root = (
        Path(build_root).expanduser().resolve()
        if build_root is not None
        else DEFAULT_BUILD_ROOT.resolve()
    )
    records = _source_records(source_path)
    if expected_source_hashes is not None:
        expected = {str(name): str(digest).lower() for name, digest in expected_source_hashes.items()}
        if set(expected) != set(SOURCE_NAMES):
            raise ContactLawBuildError(
                "expected native source hashes must cover exactly the four copied source files"
            )
        for record in records:
            if expected[record["filename"]] != record["sha256"]:
                raise ContactLawBuildError(
                    f"native source hash mismatch for {record['filename']}: "
                    f"expected {expected[record['filename']]}, got {record['sha256']}"
                )
    compiler_text = str(compiler)
    fingerprint_input = json.dumps(
        {
            "sources": [{"filename": r["filename"], "sha256": r["sha256"]} for r in records],
            "wrapper_sha256": hashlib.sha256(WRAPPER_SOURCE.encode("utf-8")).hexdigest(),
            "compiler": compiler_text,
            "compile_flags": COMPILE_FLAGS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(fingerprint_input).hexdigest()
    build_dir = root / f"contact-laws-{fingerprint[:24]}"
    library_path = build_dir / LIBRARY_NAME
    provenance_path = build_dir / "provenance.json"
    root.mkdir(parents=True, exist_ok=True)

    if not force and library_path.is_file() and provenance_path.is_file():
        try:
            existing = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if _cache_manifest_matches(
            existing,
            fingerprint=fingerprint,
            source_records=records,
            wrapper_path=build_dir / WRAPPER_NAME,
            library_path=library_path,
        ):
            return ContactLawBuild(library_path, build_dir, provenance_path, fingerprint)

    build_dir.mkdir(parents=True, exist_ok=True)
    copied_records: list[dict[str, str]] = []
    for record in records:
        name = record["filename"]
        destination = build_dir / name
        shutil.copy2(source_path / name, destination)
        if _sha256(destination) != record["sha256"]:
            raise ContactLawBuildError(
                f"native source changed while copying {name}; refusing to build"
            )
        copied = dict(record)
        copied["copied_path"] = str(destination)
        copied_records.append(copied)
    wrapper_path = build_dir / WRAPPER_NAME
    wrapper_path.write_text(WRAPPER_SOURCE, encoding="utf-8")

    command = [
        compiler_text,
        *COMPILE_FLAGS,
        "-I",
        str(build_dir),
        str(build_dir / "controller_suite.cpp"),
        str(build_dir / "native_seven_laws.cpp"),
        str(wrapper_path),
        "-o",
        str(library_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=build_dir,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr.strip() or error.stdout.strip()
        else:
            detail = str(error)
        raise ContactLawBuildError(f"native contact law build failed: {detail}") from error
    if completed.returncode != 0 or not library_path.is_file():
        raise ContactLawBuildError("native contact law compiler produced no shared library")

    provenance = {
        "schema": "ur10e.contact-laws-build-provenance-v1",
        "fingerprint": fingerprint,
        "offline_only": True,
        "endpoint": "none",
        "hardware_qualified": False,
        "source_checkout_is_unchanged": True,
        "source_dir": str(source_path),
        "source_files": copied_records,
        "wrapper": {
            "filename": WRAPPER_NAME,
            "path": str(wrapper_path),
            "sha256": _sha256(wrapper_path),
        },
        "library": {
            "path": str(library_path),
            "sha256": _sha256(library_path),
        },
        "compiler": command,
        "reference_artifacts": [
            _reference_record(
                Path("/home/andy/CodexReport/Library/source/reports/sfc-traction-20260908/story/parameters.json")
            ),
            _reference_record(
                Path("/home/andy/CodexReport/Library/source/reports/msfc-research-20260912/guidance/lac-matching/search.json")
            ),
            _reference_record(
                Path("/home/andy/CodexReport/Library/source/reports/msfc-research-20260912/baseline_parameters.json")
            ),
        ],
    }
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ContactLawBuild(library_path, build_dir, provenance_path, fingerprint)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--build-root", type=Path, default=None)
    parser.add_argument("--compiler", default="g++")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = build_shared_library(
        source_dir=args.source_dir,
        build_root=args.build_root,
        compiler=args.compiler,
        force=args.force,
    )
    print(result.library_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
