#!/usr/bin/env python3
"""Deprecated compatibility shim for the misnamed Step5c joint-RNN module."""

from __future__ import annotations

from step5c_dls_joint_solver import (  # noqa: F401
    CONTACT_STAGE_ID,
    DEFAULT_MODEL_PATH,
    DEFAULT_SITE_NAME,
    DRYRUN_STAGE_ID,
    STATUS_CLIPPED,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_PROJECTED,
    JointCommandResult,
    JointSolverConfig,
    Step5cDlsJointSolver,
    main,
    numeric_sanity,
    solve_joint_velocity,
)

JointRnnSolver = Step5cDlsJointSolver


if __name__ == "__main__":
    raise SystemExit(main())
