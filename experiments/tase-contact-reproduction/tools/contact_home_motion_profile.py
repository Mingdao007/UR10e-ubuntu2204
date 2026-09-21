"""Historical motion profile for the existing Step5d Home helper.

These values are copied from the original
``step5d_autotune_start_hover_r001`` program.  They are command values, not a
new tuning choice: vertical segments used 40 mm/s and the clearance transfer
used 90 mm/s.
"""

HOME_VERTICAL_SPEED_M_S = 0.040
HOME_VERTICAL_ACCEL_M_S2 = 0.060
HOME_TRANSFER_SPEED_M_S = 0.090
HOME_TRANSFER_ACCEL_M_S2 = 0.135

# Runtime guards include the original program's <=100 mm/s validation bound
# and a geometry-derived joint-speed margin.  They are not command values.
HOME_TCP_SPEED_GUARD_M_S = 0.100
HOME_JOINT_SPEED_GUARD_RAD_S = 0.150
HOME_ANGULAR_SPEED_GUARD_RAD_S = 0.040

HISTORICAL_PROFILE_ID = "step5d_autotune_start_hover_r001:segment-1/2/3"
