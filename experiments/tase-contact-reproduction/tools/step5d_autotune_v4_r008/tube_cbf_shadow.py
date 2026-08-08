"""Offline shadow replay / injection gates for Tube + CBF-QP.

Uses sealed ``r008-path-xy.jsonl`` tracking errors as nominal lateral
offsets (millimetres → metres). Soft CBF is evaluated with an analytic
ellipse; a circular hard radius models the existing HardTube backstop.

No robot I/O. Does not enable launch policies.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from step5d_autotune_v4_r008.tube_cbf import TubeCbfQp, TubeCbfResult

DEFAULT_SOFT_A_M = 0.035
DEFAULT_SOFT_B_M = 0.035
DEFAULT_HARD_R_M = 0.030
DEFAULT_ALPHA_S = 2.0
DEFAULT_ENGAGE_RHO = 0.67
# Worst-case outward drift: must exceed alpha*h near the engage band so the
# soft layer projects (0.003 m/s is too gentle — alpha*h already satisfies CBF).
DEFAULT_OUTWARD_SPEED_M_S = 0.05


@dataclass(frozen=True, slots=True)
class ShadowSample:
    label: str
    u_m: float
    v_m: float
    v_nom_xyz: tuple[float, float, float]
    soft: TubeCbfResult
    hard_breach: bool

    @property
    def hard_while_soft_feasible(self) -> bool:
        """Hard circle breached while still inside the soft ellipse."""

        return bool(self.hard_breach and not self.soft.infeasible)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "u_m": self.u_m,
            "v_m": self.v_m,
            "v_nom_xyz": list(self.v_nom_xyz),
            "hard_breach": self.hard_breach,
            "hard_while_soft_feasible": self.hard_while_soft_feasible,
            "soft": {
                "fired": self.soft.fired,
                "infeasible": self.soft.infeasible,
                "engaged": self.soft.engaged,
                "h": self.soft.h,
                "phi": self.soft.phi,
                "rho": self.soft.rho,
                "lambda_step": self.soft.lambda_step,
                "delta_u_norm": _delta_norm(self.v_nom_xyz, self.soft.v_xyz),
            },
        }


@dataclass(frozen=True, slots=True)
class ShadowReport:
    n: int
    fire_count: int
    infeasible_count: int
    hard_breach_count: int
    hard_while_soft_feasible_count: int
    fire_rate: float
    samples: tuple[ShadowSample, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "fire_count": self.fire_count,
            "infeasible_count": self.infeasible_count,
            "hard_breach_count": self.hard_breach_count,
            "hard_while_soft_feasible_count": self.hard_while_soft_feasible_count,
            "fire_rate": self.fire_rate,
            "samples": [s.as_dict() for s in self.samples],
        }


def _delta_norm(
    v_nom: Sequence[float], v_out: Sequence[float]
) -> float:
    return math.hypot(v_out[0] - v_nom[0], v_out[1] - v_nom[1])


def hard_breach(u_m: float, v_m: float, *, hard_r_m: float = DEFAULT_HARD_R_M) -> bool:
    """Circular HardTube-style breach: Euclidean radius >= R."""

    if hard_r_m <= 0.0 or not math.isfinite(hard_r_m):
        raise ValueError("hard_r_m must be finite and positive")
    return math.hypot(u_m, v_m) >= hard_r_m


def load_path_xy_rows(path: Path | str) -> list[dict[str, Any]]:
    root = Path(path)
    rows: list[dict[str, Any]] = []
    for line in root.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def nominal_offsets_from_path_xy(
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[str, float, float]]:
    """Map sealed tracking errors to PATH-frame (u,v) samples in metres."""

    out: list[tuple[str, float, float]] = []
    for row in rows:
        seq = row.get("attempt_sequence", "?")
        kind = row.get("kind", "?")
        err = row.get("xy_error_p95_mm")
        if err is None:
            err = row.get("xy_error_max_mm")
        if err is None:
            continue
        u_m = float(err) * 1e-3
        out.append((f"nominal:{kind}:{seq}", u_m, 0.0))
    return out


def evaluate_sample(
    *,
    label: str,
    u_m: float,
    v_m: float,
    v_nom_xyz: Sequence[float],
    qp: TubeCbfQp,
    hard_r_m: float = DEFAULT_HARD_R_M,
    alpha: float = DEFAULT_ALPHA_S,
    engage_rho: float = DEFAULT_ENGAGE_RHO,
) -> ShadowSample:
    soft = qp.filter_twist_xy(
        v_nom_xyz,
        (u_m, v_m),
        alpha=alpha,
        engage_rho=engage_rho,
    )
    return ShadowSample(
        label=label,
        u_m=float(u_m),
        v_m=float(v_m),
        v_nom_xyz=(float(v_nom_xyz[0]), float(v_nom_xyz[1]), float(v_nom_xyz[2])),
        soft=soft,
        hard_breach=hard_breach(u_m, v_m, hard_r_m=hard_r_m),
    )


def run_shadow(
    positions: Iterable[tuple[str, float, float]],
    *,
    soft_a_m: float = DEFAULT_SOFT_A_M,
    soft_b_m: float = DEFAULT_SOFT_B_M,
    hard_r_m: float = DEFAULT_HARD_R_M,
    alpha: float = DEFAULT_ALPHA_S,
    engage_rho: float = DEFAULT_ENGAGE_RHO,
    outward_speed_m_s: float = DEFAULT_OUTWARD_SPEED_M_S,
) -> ShadowReport:
    """Evaluate soft CBF + hard circle on labeled (u,v) positions.

    Nominal twist is radially outward in the UV plane at ``outward_speed_m_s``
    (worst-case for the barrier); ``vz=0``.
    """

    qp = TubeCbfQp.from_semi_axes(soft_a_m, soft_b_m)
    samples: list[ShadowSample] = []
    for label, u_m, v_m in positions:
        radial = math.hypot(u_m, v_m)
        if radial > 0.0:
            ux, uy = u_m / radial, v_m / radial
        else:
            ux, uy = 1.0, 0.0
        v_nom = (outward_speed_m_s * ux, outward_speed_m_s * uy, 0.0)
        samples.append(
            evaluate_sample(
                label=label,
                u_m=u_m,
                v_m=v_m,
                v_nom_xyz=v_nom,
                qp=qp,
                hard_r_m=hard_r_m,
                alpha=alpha,
                engage_rho=engage_rho,
            )
        )
    n = len(samples)
    fire_count = sum(1 for s in samples if s.soft.fired)
    infeasible_count = sum(1 for s in samples if s.soft.infeasible)
    hard_count = sum(1 for s in samples if s.hard_breach)
    hard_soft_feasible = sum(1 for s in samples if s.hard_while_soft_feasible)
    return ShadowReport(
        n=n,
        fire_count=fire_count,
        infeasible_count=infeasible_count,
        hard_breach_count=hard_count,
        hard_while_soft_feasible_count=hard_soft_feasible,
        fire_rate=(fire_count / n) if n else 0.0,
        samples=tuple(samples),
    )


def shadow_nominal_from_path_xy(
    path_xy: Path | str,
    **kwargs: Any,
) -> ShadowReport:
    rows = load_path_xy_rows(path_xy)
    return run_shadow(nominal_offsets_from_path_xy(rows), **kwargs)


def shadow_injection_ladder(
    offsets_m: Sequence[float] = (0.020, 0.022, 0.026, 0.028, 0.030, 0.031),
    **kwargs: Any,
) -> ShadowReport:
    positions = [(f"inject_u={o:.3f}", float(o), 0.0) for o in offsets_m]
    return run_shadow(positions, **kwargs)


def soft_before_hard_ok(report: ShadowReport) -> bool:
    """True if some soft fire occurs at a sample that is not yet hard-breach,
    and every hard-breach sample is soft-infeasible or beyond soft engage.
    """

    soft_pre_hard = any(s.soft.fired and not s.hard_breach for s in report.samples)
    if not soft_pre_hard:
        return False
    for s in report.samples:
        if s.hard_breach and not (s.soft.infeasible or s.soft.fired or s.soft.engaged):
            return False
    return True


def write_shadow_report(report: ShadowReport, path: Path | str) -> Path:
    """Write sidecar JSON with h/phi/delta_u_norm/fired/infeasible fields."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path-xy",
        type=Path,
        help="Sealed r008-path-xy.jsonl for nominal fire-rate gate",
    )
    parser.add_argument(
        "--inject",
        action="store_true",
        help="Run default injection ladder (soft-before-hard)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tube_cbf_shadow_report.json"),
        help="Output sidecar JSON path",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    payload: dict[str, Any] = {}
    if args.path_xy is not None:
        nom = shadow_nominal_from_path_xy(args.path_xy)
        payload["nominal"] = nom.as_dict()
        print(
            f"nominal n={nom.n} fire_rate={nom.fire_rate:.4f} "
            f"hard_breach={nom.hard_breach_count}"
        )
    if args.inject or args.path_xy is None:
        inj = shadow_injection_ladder()
        payload["injection"] = inj.as_dict()
        payload["soft_before_hard_ok"] = soft_before_hard_ok(inj)
        print(
            f"injection n={inj.n} fire_count={inj.fire_count} "
            f"hard_breach={inj.hard_breach_count} "
            f"soft_before_hard_ok={payload['soft_before_hard_ok']}"
        )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


__all__ = [
    "DEFAULT_ALPHA_S",
    "DEFAULT_ENGAGE_RHO",
    "DEFAULT_HARD_R_M",
    "DEFAULT_OUTWARD_SPEED_M_S",
    "DEFAULT_SOFT_A_M",
    "DEFAULT_SOFT_B_M",
    "ShadowReport",
    "ShadowSample",
    "evaluate_sample",
    "hard_breach",
    "load_path_xy_rows",
    "nominal_offsets_from_path_xy",
    "run_shadow",
    "shadow_injection_ladder",
    "shadow_nominal_from_path_xy",
    "soft_before_hard_ok",
    "write_shadow_report",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
