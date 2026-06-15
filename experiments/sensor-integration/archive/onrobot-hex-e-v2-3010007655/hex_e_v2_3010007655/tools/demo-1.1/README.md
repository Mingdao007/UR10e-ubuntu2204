# demo-1.1 OnRobot Tool-Z Contact Scripts

Prepared for the current UR10e bench with FT-OnRobot URCap variables.

Robot-side target folder:

```text
/programs/andyl/demo-1.1
```

## Files

- `00_onrobot_register_export_only.script`: no-motion export of `Fx/Fy/Fz/Tx/Ty/Tz` to `output_double_register_24..29`.
- `02_onrobot_data_path_check_wait_0p1s.script`: no-motion 0.1 s register-write check for the OnRobot RTDE data path.
- `05_tool_z_direction_probe_1mm.script`: optional free-space 1 mm move along configured Tool Z direction and back. Each phase has a `2 s` timeout.
- `10_onrobot_tool_z_contact_stop_3n.script`: first contact test. It expects an OnRobot `F/T Zero` in no-contact state before the script. It moves along fixed start Tool Z at `10 mm/s`, stops when OnRobot `abs(Fz - baseline_Fz) >= 5 N`, or when travel/time/raw-force/guard limits trip.
- `capture_onrobot_rtde_registers.py`: Ubuntu-side RTDE logger for `output_double_register_24..29` plus robot state.

## Configure Before Running

Edit only this line in motion scripts:

```text
contact_sign = 1
```

Current demo-1.1 setup uses `contact_sign = 1` because the user confirmed that
`+Tool Z` points toward the contact surface.

Use `contact_sign = 1` if `+Tool Z` points toward the contact surface.
Use `contact_sign = -1` if `-Tool Z` points toward the contact surface.

The contact-stop defaults are:

```text
target_fz_delta_n = 5.0
startup_fz_abs_abort_n = 2.0
raw_fz_abs_guard_n = 8.0
max_travel_m = 0.030
max_time_s = 10.0
approach_speed_mps = 0.010
accel_mps2 = 0.5
```

## Run Order

1. Keep OnRobot Hand Guide, Snap, and F/T Set TCP off.
2. Confirm PolyScope safety mode is normal and no program is running.
3. Confirm `Program Loops Forever` is unchecked.
4. Move manually to about `21 mm` above the contact surface after power-on/unlock is stable.
5. Run a no-motion data-path check before contact:
   - Codex starts `capture_onrobot_rtde_registers.py` on Ubuntu.
   - The operator runs `02_onrobot_data_path_check_wait_0p1s.script`.
   - Expected capture at `125 Hz`: roughly 12-13 samples during the 0.1 s register-write window, within a longer logger file.
6. In the contact program, place OnRobot `F/T Zero` before the script and execute it only while no contact exists.
7. Run `10_onrobot_tool_z_contact_stop_3n.script`.
8. Stop if the direction is wrong, any cable or fixture can be caught, safety mode changes, or force behavior is confusing.

## Data Source Boundary

These scripts do not use UR `actual_TCP_force` as the contact criterion.
The primary contact stop criterion is OnRobot URCap variable
`abs(Fz - baseline_Fz)` after a no-contact OnRobot `F/T Zero`.
Raw OnRobot `abs(Fz)`, `F3D`, and `T3D` are guards.

Contact-script diagnostics are exported as:

```text
output_double_register_30 = stop_reason
output_double_register_31 = travel_m
output_double_register_32 = abs_fz_delta
output_double_register_33 = base_fz
```

UR support logs are not treated as the primary data record for OnRobot
`Fx/Fy/Fz/Tx/Ty/Tz`. The accepted data record is the Ubuntu RTDE CSV captured
from `output_double_register_24..29`.

Output registers persist their last written values. A stopped program can still
show an old `output_double_register_26` value. Treat register values as live
OnRobot data only during a program window that is actively writing the
registers, such as `02_onrobot_data_path_check_wait_0p1s.script` or the
contact script.
