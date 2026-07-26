from __future__ import annotations

from ur10e_vic.contracts import ImpedanceObservation, PoseSample


def observation(
    *,
    timestamp_s: float = 0.0,
    current_position: tuple[float, float, float] = (0.01, 0.0, 0.0),
    nominal_position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    wrench: tuple[float, ...] = (4.0, 0.0, 0.0, 0.0, 0.0, 0.0),
) -> ImpedanceObservation:
    current = PoseSample(current_position, (1.0, 0.0, 0.0, 0.0))
    nominal = PoseSample(nominal_position, (1.0, 0.0, 0.0, 0.0))
    identity = tuple(
        tuple(1.0 if row == column else 0.0 for column in range(6))
        for row in range(6)
    )
    return ImpedanceObservation(
        sequence=int(timestamp_s * 200.0),
        timestamp_s=timestamp_s,
        pose_history=(current,) * 16,
        twist_history=((0.0,) * 6,) * 16,
        wrench_history=(wrench,) * 16,
        nominal_zft=nominal,
        joint_position_rad=(0.0,) * 6,
        joint_velocity_rad_s=(0.0,) * 6,
        jacobian_base=identity,
        frame_id="base",
        sensor_id="kunwei",
        calibration_hash="a" * 64,
    )
