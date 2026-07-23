# Step5d Remote Control

Step5d now has one public operator entrypoint:

```bash
./step5d_remote_control.sh status
./step5d_remote_control.sh dry-run
./step5d_remote_control.sh canary --live
./step5d_remote_control.sh run --live
```

The default parameter selector is `config/step5d_remote/current.json`; it
resolves to `config/step5d_remote/r012.yaml`. Runtime parameters are not exposed
as shell flags. A later tuning module changes or supplies a parameter file
without changing the control code.

`status` and `dry-run` are read-only. `canary --live` is the only first live
step. `run --live` requires a fresh, same-boot, same-parameter successful
canary. Remote Control mode is required but is not motion authorization.

The runtime uses ROS 2 control directly. It does not upload, load, play, arm,
or execute a TP program. The controller watchdog enforces command freshness,
acceleration bounds, exact-zero fault latching, and fail-closed cleanup.

The legacy TP/autotune/BO route was removed from the active tree. Its final
recovery point is the Git tag
`archive/step5d-tp-autotune-r012-20260723`.

## Other Step5 routes

The unrelated Local Control Textbook Alignment remains defined by
`config/local_control_textbook_spec.json`,
`config/step5a_local_control_spec.json`, the Step5a TP v3 script,
`programs/step5/step5b_contact_cycloid_baseline_v1.script`, and its safe-frame
evidence. None of these is a Step5d entrypoint.
