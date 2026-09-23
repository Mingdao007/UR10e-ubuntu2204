#!/usr/bin/env python3
"""Compare fixed-camera contact-board images without opening any device.

The declared corridor is the expected 5 N path. Changes inside it (plus a
small registration tolerance) are permitted and are not counted as new
outside-corridor marks. A qualified zero is a visible-image finding only; it
is not a claim that the board has no damage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np


SCHEMA_VERSION = "contact-board-marks.v1"
_MAX_REGISTRATION_ERROR_PX = 1.50
_MIN_PHASE_RESPONSE = 0.20
_MAX_TRANSLATION_PX = 12.0
_MAX_SATURATED_FRACTION = 0.35
_MIN_BOARD_CONTRAST_LEVELS = 15.0
_MAX_NOISE_SIGMA_GRAY = 8.0
_MAX_DETECTION_THRESHOLD_GRAY = 48.0
_MIN_DETECTION_SNR = 4.0
_REPEATABILITY_IOU_MIN = 0.50
_REPEATABILITY_RELATIVE_AREA_SPREAD_MAX = 0.50
_REPEATABILITY_FACTORS = (0.85, 1.0, 1.15)


def _source_label(source: Any) -> str:
    return str(source) if isinstance(source, (str, Path)) else "<array>"


def _read_mask_source(source: Any) -> tuple[bytes | None, str | None]:
    if not isinstance(source, (str, Path)):
        return None, None
    try:
        contents = Path(source).read_bytes()
        return contents, hashlib.sha256(contents).hexdigest()
    except OSError:
        return None, None


def _canonical_mask_sha256(mask: np.ndarray) -> str:
    canonical = np.where(mask > 0, 255, 0).astype(np.uint8)
    digest = hashlib.sha256()
    digest.update(np.asarray(canonical.shape, dtype="<u8").tobytes())
    digest.update(np.ascontiguousarray(canonical).tobytes())
    return digest.hexdigest()


def _load_image(source: Any, *, grayscale: bool = False) -> np.ndarray:
    if isinstance(source, (str, Path)):
        flag = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_COLOR
        image = cv2.imread(str(source), flag)
        if image is None:
            raise ValueError(f"could not decode image: {source}")
    else:
        image = np.asarray(source)

    if image.dtype != np.uint8:
        raise ValueError("images and corridor mask must use uint8 pixels")
    if image.ndim not in (2, 3):
        raise ValueError("image must be a grayscale or color raster")
    if image.ndim == 3 and image.shape[2] not in (3, 4):
        raise ValueError("color image must have three or four channels")
    return image


def _gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _percentile_contrast(gray: np.ndarray, region: np.ndarray) -> float:
    values = gray[region]
    if values.size == 0:
        return 0.0
    low, high = np.percentile(values, (5, 95))
    return float(high - low)


def _saturated_fraction(image: np.ndarray, region: np.ndarray) -> float:
    if image.ndim == 2:
        channels = image[..., None]
    else:
        channels = image[..., :3]
    pixels = channels[region]
    if pixels.size == 0:
        return 1.0
    clipped = np.any((pixels <= 2) | (pixels >= 253), axis=1)
    return float(np.mean(clipped))


def _robust_sigma(values: np.ndarray) -> tuple[float, float]:
    flat = np.asarray(values, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return 0.0, 0.0
    median = float(np.median(flat))
    mad = float(np.median(np.abs(flat - median)))
    return median, 1.4826 * mad


def _component_mask(mask: np.ndarray, min_area_px: int) -> tuple[np.ndarray, int, int]:
    binary = np.asarray(mask, dtype=np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    kept = np.zeros(binary.shape, dtype=np.uint8)
    areas = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area >= min_area_px:
            kept[labels == label] = 1
            areas.append(area)
    return kept.astype(bool), len(areas), int(sum(areas))


def _unqualified_result(
    before_label: str,
    after_label: str,
    mask_label: str,
    reason: str,
    *,
    pixel_size_mm: float | None,
    corridor_mask_sha256: str | None,
    corridor_mask_sha256_basis: str | None,
) -> dict[str, Any]:
    safe_pixel_size = (
        float(pixel_size_mm)
        if pixel_size_mm is not None
        and isinstance(pixel_size_mm, (int, float, np.integer, np.floating))
        and np.isfinite(pixel_size_mm)
        and pixel_size_mm > 0
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "qualified": False,
        "finding": "unqualified",
        "claim_boundary": (
            "visible image comparison only; this result cannot establish physical damage absence"
        ),
        "expected_5n_path_marks_permitted": True,
        "inputs": {
            "before_image": before_label,
            "after_image": after_label,
            "nominal_path_corridor_mask": mask_label,
            "pixel_size_mm": safe_pixel_size,
        },
        "corridor_mask_sha256": corridor_mask_sha256,
        "corridor_mask_sha256_basis": corridor_mask_sha256_basis,
        "outside_corridor_area_px2": None,
        "max_outside_distance_px": None,
        "outside_mark_component_count": None,
        "outside_corridor_area_mm2": None,
        "max_outside_distance_mm": None,
        "registration_error_px": None,
        "registration": {"qualified": False},
        "detectability": {"qualified": False},
        "gates": {"qualified": False},
        "failure_reasons": [reason],
    }


def _phase_registration_error(
    reference: np.ndarray,
    aligned: np.ndarray,
    valid_region: np.ndarray,
) -> tuple[float, float]:
    reference_f = reference.astype(np.float32)
    aligned_f = aligned.astype(np.float32)
    reference_edges = cv2.magnitude(
        cv2.Sobel(reference_f, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(reference_f, cv2.CV_32F, 0, 1, ksize=3),
    )
    aligned_edges = cv2.magnitude(
        cv2.Sobel(aligned_f, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(aligned_f, cv2.CV_32F, 0, 1, ksize=3),
    )
    window = cv2.createHanningWindow((reference.shape[1], reference.shape[0]), cv2.CV_32F)
    weight = valid_region.astype(np.float32) * window
    reference_edges *= weight
    aligned_edges *= weight
    shift, response = cv2.phaseCorrelate(reference_edges, aligned_edges)
    error_px = float(np.hypot(shift[0], shift[1]))
    return error_px, float(response)


def measure_contact_board_marks(
    before: Any,
    after: Any,
    corridor_mask: Any,
    *,
    pixel_size_mm: float | None = None,
    corridor_margin_px: int = 2,
    min_component_area_px: int = 4,
) -> dict[str, Any]:
    """Return a fail-closed JSON-compatible image evidence result.

    ``pixel_size_mm`` is optional calibration supplied by the caller. No
    physical distance or area is emitted unless that scale is supplied.
    Inputs may be image paths or already loaded uint8 NumPy arrays.
    """
    before_label = _source_label(before)
    after_label = _source_label(after)
    mask_label = _source_label(corridor_mask)
    mask_file_bytes, corridor_mask_sha256 = _read_mask_source(corridor_mask)
    corridor_mask_sha256_basis = "raw_file_bytes" if corridor_mask_sha256 else None
    if pixel_size_mm is not None and (
        not isinstance(pixel_size_mm, (int, float, np.integer, np.floating))
        or not np.isfinite(pixel_size_mm)
        or pixel_size_mm <= 0
    ):
        return _unqualified_result(
            before_label,
            after_label,
            mask_label,
            "pixel_size_mm must be a finite positive calibration",
            pixel_size_mm=pixel_size_mm,
            corridor_mask_sha256=corridor_mask_sha256,
            corridor_mask_sha256_basis=corridor_mask_sha256_basis,
        )
    if corridor_margin_px < 0 or min_component_area_px < 1:
        return _unqualified_result(
            before_label,
            after_label,
            mask_label,
            "corridor margin and minimum component area must be nonnegative/positive",
            pixel_size_mm=pixel_size_mm,
            corridor_mask_sha256=corridor_mask_sha256,
            corridor_mask_sha256_basis=corridor_mask_sha256_basis,
        )

    try:
        before_image = _load_image(before)
        after_image = _load_image(after)
        if mask_file_bytes is None:
            corridor_image = _load_image(corridor_mask, grayscale=True)
        else:
            corridor_image = cv2.imdecode(
                np.frombuffer(mask_file_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE
            )
            if corridor_image is None:
                raise ValueError(f"could not decode corridor mask: {corridor_mask}")
        if corridor_mask_sha256 is None:
            corridor_mask_sha256 = _canonical_mask_sha256(corridor_image)
            corridor_mask_sha256_basis = "canonical_binary_u8_shape_and_row_major_bytes"
        if before_image.shape[:2] != after_image.shape[:2]:
            raise ValueError("before and after image dimensions differ")
        if corridor_image.shape != before_image.shape[:2]:
            raise ValueError("corridor mask dimensions must match the images")

        before_gray = _gray(before_image)
        after_gray = _gray(after_image)
        nominal_corridor = corridor_image > 0
        height, width = before_gray.shape
        pixel_count = height * width
        corridor_fraction = float(np.mean(nominal_corridor))
        if not np.any(nominal_corridor) or np.all(nominal_corridor):
            raise ValueError("corridor mask must include both path and outside-board pixels")
        if pixel_count < 256:
            raise ValueError("image is too small for stable registration and detection")

        margin_kernel_size = 2 * corridor_margin_px + 1
        if corridor_margin_px:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (margin_kernel_size, margin_kernel_size)
            )
            permitted_corridor = cv2.dilate(nominal_corridor.astype(np.uint8), kernel) > 0
        else:
            permitted_corridor = nominal_corridor
        registration_region = ~permitted_corridor
        if np.count_nonzero(registration_region) < max(100, int(0.05 * pixel_count)):
            raise ValueError("too little outside-corridor image area for registration")

        # The camera is fixed, so use a translation-only phase correlation.
        # Low-frequency structure suppresses sensor/JPEG texture; the full
        # frame keeps the mask edge from becoming a registration feature.
        template = cv2.GaussianBlur(before_gray, (9, 9), 1.6).astype(np.float32)
        moving = cv2.GaussianBlur(after_gray, (9, 9), 1.6).astype(np.float32)
        hanning = cv2.createHanningWindow((width, height), cv2.CV_32F)
        initial_shift, initial_response = cv2.phaseCorrelate(template, moving, hanning)
        if not np.isfinite(initial_shift).all():
            raise ValueError("phase correlation returned a nonfinite registration shift")
        tx, ty = float(initial_shift[0]), float(initial_shift[1])
        warp = np.array([[1.0, 0.0, tx], [0.0, 1.0, ty]], dtype=np.float32)
        aligned_gray = cv2.warpAffine(
            after_gray,
            warp,
            (width, height),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        valid = cv2.warpAffine(
            np.full((height, width), 255, dtype=np.uint8),
            warp,
            (width, height),
            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        ) > 0
        valid = cv2.erode(valid.astype(np.uint8), np.ones((3, 3), dtype=np.uint8)) > 0
        valid_region = valid & registration_region
        if np.count_nonzero(valid_region) < max(100, int(0.05 * pixel_count)):
            raise ValueError("registration left too little valid outside-corridor overlap")

        residual_error_px, phase_response = _phase_registration_error(
            before_gray, aligned_gray, valid_region
        )

        outside_values = registration_region & valid
        before_contrast = _percentile_contrast(before_gray, outside_values)
        after_contrast = _percentile_contrast(aligned_gray, outside_values)
        before_saturation = _saturated_fraction(before_image, registration_region)
        after_saturation = _saturated_fraction(after_image, registration_region)

        before_values = before_gray[outside_values].astype(np.float32)
        after_values = aligned_gray[outside_values].astype(np.float32)
        # Robust quantiles normalize modest exposure/gain changes. Large gain
        # changes are reported as a failed detectability gate below.
        before_low, before_high = np.percentile(before_values, (10, 90))
        after_low, after_high = np.percentile(after_values, (10, 90))
        before_span = float(before_high - before_low)
        after_span = float(after_high - after_low)
        photometric_span_valid = before_span >= 1.0 and after_span >= 1.0
        if not photometric_span_valid:
            gain = 1.0
        else:
            gain = before_span / after_span
        offset = (
            float(np.median(before_values) - gain * np.median(after_values))
            if np.isfinite(gain)
            else 0.0
        )
        aligned_normalized = np.clip(aligned_gray.astype(np.float32) * gain + offset, 0, 255)
        aligned_normalized_u8 = np.rint(aligned_normalized).astype(np.uint8)
        before_smooth = cv2.GaussianBlur(before_gray, (3, 3), 0.55).astype(np.float32)
        after_smooth = cv2.GaussianBlur(aligned_normalized_u8, (3, 3), 0.55).astype(np.float32)
        difference = np.abs(after_smooth - before_smooth)

        reference_gradient = cv2.magnitude(
            cv2.Sobel(before_gray.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(before_gray.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3),
        )
        flat_region = outside_values & (reference_gradient < 24.0)
        noise_region = flat_region if np.count_nonzero(flat_region) >= 100 else outside_values
        noise_median, noise_sigma = _robust_sigma(difference[noise_region])
        threshold = max(12.0, noise_median + 6.0 * noise_sigma)

        detection_region = valid & ~permitted_corridor
        candidate_mask = (difference >= threshold) & detection_region
        repeatability_masks: list[np.ndarray] = []
        repeatability_areas: list[int] = []
        repeatability_components: list[int] = []
        for factor in _REPEATABILITY_FACTORS:
            detected, components, area = _component_mask(
                (difference >= threshold * factor) & detection_region,
                min_component_area_px,
            )
            repeatability_masks.append(detected)
            repeatability_areas.append(area)
            repeatability_components.append(components)

        base_mask, component_count, area_px2 = _component_mask(
            candidate_mask, min_component_area_px
        )
        intersection = np.logical_and.reduce(repeatability_masks)
        union = np.logical_or.reduce(repeatability_masks)
        repeatability_iou = (
            1.0
            if not np.any(union)
            else float(np.count_nonzero(intersection) / np.count_nonzero(union))
        )
        max_area = max(repeatability_areas, default=0)
        min_area = min(repeatability_areas, default=0)
        area_spread = 0.0 if max_area == 0 else float((max_area - min_area) / max_area)
        repeatability_ok = (
            all(area == 0 for area in repeatability_areas)
            or (
                all(area > 0 for area in repeatability_areas)
                and repeatability_iou >= _REPEATABILITY_IOU_MIN
                and area_spread <= _REPEATABILITY_RELATIVE_AREA_SPREAD_MAX
            )
        )

        corridor_distance = cv2.distanceTransform(
            np.where(nominal_corridor, 0, 255).astype(np.uint8), cv2.DIST_L2, 5
        )
        max_distance_px = (
            float(np.max(corridor_distance[base_mask])) if area_px2 else None
        )
        snr = float(threshold / max(noise_sigma, 1.0))
        registration_gate = bool(
            np.isfinite(initial_response)
            and initial_response >= _MIN_PHASE_RESPONSE
            and np.isfinite(residual_error_px)
            and residual_error_px <= _MAX_REGISTRATION_ERROR_PX
            and phase_response >= _MIN_PHASE_RESPONSE
            and np.hypot(tx, ty) <= _MAX_TRANSLATION_PX
        )
        saturation_gate = bool(
            before_saturation <= _MAX_SATURATED_FRACTION
            and after_saturation <= _MAX_SATURATED_FRACTION
        )
        contrast_gate = bool(
            before_contrast >= _MIN_BOARD_CONTRAST_LEVELS
            and after_contrast >= _MIN_BOARD_CONTRAST_LEVELS
        )
        photometric_gate = bool(
            photometric_span_valid and np.isfinite(gain) and 0.5 <= gain <= 2.0
        )
        noise_gate = bool(
            np.isfinite(noise_sigma)
            and noise_sigma <= _MAX_NOISE_SIGMA_GRAY
            and threshold <= _MAX_DETECTION_THRESHOLD_GRAY
            and snr >= _MIN_DETECTION_SNR
        )
        detectability_gate = bool(
            saturation_gate
            and contrast_gate
            and photometric_gate
            and noise_gate
            and repeatability_ok
        )
        qualified = bool(registration_gate and detectability_gate)
        reasons: list[str] = []
        if not registration_gate:
            reasons.append("registration quality gate failed")
        if not saturation_gate:
            reasons.append("image saturation gate failed")
        if not contrast_gate:
            reasons.append("board contrast gate failed")
        if not photometric_gate:
            reasons.append("photometric gain gate failed")
        if not noise_gate:
            reasons.append("noise/detectability gate failed")
        if not repeatability_ok:
            reasons.append("threshold-perturbation repeatability gate failed")

        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "qualified": qualified,
            "finding": (
                "unqualified"
                if not qualified
                else "new_visible_change_outside_corridor"
                if area_px2
                else "no_detected_change_outside_corridor"
            ),
            "claim_boundary": (
                "visible image comparison only; a qualified zero means no change detected "
                "above this tool's pixel sensitivity, "
                "not proof of physical damage absence"
            ),
            "expected_5n_path_marks_permitted": True,
            "inputs": {
                "before_image": before_label,
                "after_image": after_label,
                "nominal_path_corridor_mask": mask_label,
                "pixel_size_mm": pixel_size_mm,
            },
            "corridor_mask_sha256": corridor_mask_sha256,
            "corridor_mask_sha256_basis": corridor_mask_sha256_basis,
            "outside_corridor_area_px2": area_px2 if qualified else None,
            "max_outside_distance_px": max_distance_px if qualified else None,
            "outside_mark_component_count": component_count if qualified else None,
            "outside_corridor_area_mm2": (
                float(area_px2 * pixel_size_mm * pixel_size_mm)
                if qualified and pixel_size_mm is not None
                else None
            ),
            "max_outside_distance_mm": (
                float(max_distance_px * pixel_size_mm)
                if qualified and pixel_size_mm is not None and max_distance_px is not None
                else None
            ),
            "corridor_margin_px": corridor_margin_px,
            "registration_error_px": residual_error_px,
            "registration": {
                "qualified": registration_gate,
                "method": "phase_correlation_translation",
                "initial_phase_correlation_response": float(initial_response),
                "residual_phase_correlation_error_px": residual_error_px,
                "phase_correlation_response": phase_response,
                "translation_px": {"x": tx, "y": ty},
                "rotation_or_scale_estimated": False,
                "valid_overlap_fraction": float(np.mean(valid)),
                "nominal_corridor_fraction": corridor_fraction,
            },
            "detectability": {
                "qualified": detectability_gate,
                "before_board_contrast_5_95_levels": before_contrast,
                "after_board_contrast_5_95_levels": after_contrast,
                "before_saturated_fraction": before_saturation,
                "after_saturated_fraction": after_saturation,
                "photometric_gain": gain,
                "photometric_offset_gray": offset,
                "noise_median_gray": noise_median,
                "noise_sigma_gray": noise_sigma,
                "detection_threshold_gray": threshold,
                "detection_threshold_to_noise_ratio": snr,
                "minimum_component_area_px2": min_component_area_px,
                "repeatability": {
                    "method": (
                        "connected-component detections under plus-or-minus-15-percent "
                        "threshold changes"
                    ),
                    "threshold_factors": list(_REPEATABILITY_FACTORS),
                    "areas_px2": repeatability_areas,
                    "component_counts": repeatability_components,
                    "mask_iou_across_sweep": repeatability_iou,
                    "relative_area_spread": area_spread,
                    "qualified": repeatability_ok,
                },
                "capture_repeatability_measured": False,
            },
            "gates": {
                "registration": registration_gate,
                "saturation": saturation_gate,
                "contrast": contrast_gate,
                "photometric_gain": photometric_gate,
                "noise_and_detection_floor": noise_gate,
                "threshold_repeatability": repeatability_ok,
                "qualified": qualified,
            },
            "failure_reasons": reasons,
        }
        # Guarantee JSON-safe finite numbers even if a dependency returns an
        # unexpected nonfinite intermediate. Such a result must fail closed.
        encoded = json.dumps(result, allow_nan=False)
        return json.loads(encoded)
    except (ValueError, cv2.error, FloatingPointError, OverflowError) as exc:
        return _unqualified_result(
            before_label,
            after_label,
            mask_label,
            str(exc),
            pixel_size_mm=pixel_size_mm,
            corridor_mask_sha256=corridor_mask_sha256,
            corridor_mask_sha256_basis=corridor_mask_sha256_basis,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--corridor-mask", required=True, type=Path)
    parser.add_argument("--pixel-size-mm", type=float, default=None)
    parser.add_argument("--corridor-margin-px", type=int, default=2)
    parser.add_argument("--min-component-area-px", type=int, default=4)
    parser.add_argument("--output", type=Path, default=None, help="optional JSON output file")
    args = parser.parse_args(argv)

    result = measure_contact_board_marks(
        args.before,
        args.after,
        args.corridor_mask,
        pixel_size_mm=args.pixel_size_mm,
        corridor_margin_px=args.corridor_margin_px,
        min_component_area_px=args.min_component_area_px,
    )
    serialized = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as target:
            target.write(serialized)
    print(serialized, end="")
    return 0 if result["qualified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
