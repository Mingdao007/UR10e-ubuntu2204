#!/usr/bin/env python3
"""Build the fixed-size, allocation-free C solver. Never opens a device."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

import numpy as np
import osqp
from scipy import sparse


def build(output: Path) -> Path:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Explicit zeros retain all 36 Jacobian entries in the CSC pattern.
    rows = np.array([r for c in range(6) for r in (*range(6), 6 + c)])
    cols = np.repeat(np.arange(6), 7)
    values = np.array([float(r == c) for c in range(6) for r in range(6)]).reshape(6, 6)
    data = np.concatenate([np.r_[values[c], 1.0] for c in range(6)])
    matrix = sparse.csc_matrix((data, (rows, cols)), shape=(12, 6))
    solver = osqp.OSQP()
    solver.setup(P=sparse.eye(6, format="csc"), q=np.zeros(6), A=matrix,
                 l=np.r_[np.zeros(6), -np.ones(6)], u=np.r_[np.zeros(6), np.ones(6)],
                 verbose=False, eps_abs=1e-8, eps_rel=1e-8, max_iter=400,
                 check_termination=1, adaptive_rho_interval=25, polishing=False)
    solver.codegen(str(output), parameters="matrices", extension_name=None,
                   force_rewrite=True, prefix="contact_", compile=False)
    wrapper = output / "contact_api.c"
    wrapper.write_text('''#include <math.h>
#include "osqp.h"
#include "contact_workspace.h"

/* One bounded call; all workspace and staging buffers have static storage. */
int contact_qp_solve(const double *J, const double *v,
                    const double *lo, const double *hi,
                    const double *warm_x, const double *warm_y,
                    double *x, double *y, double *diagnostics) {
    OSQPFloat A[42], l[12], u[12];
    int k = 0;
    for (int c = 0; c < 6; ++c) {
        for (int r = 0; r < 6; ++r) A[k++] = J[r*6+c];
        A[k++] = 1.0;
    }
    for (int i = 0; i < 6; ++i) {
        l[i] = u[i] = v[i]; l[6+i] = lo[i]; u[6+i] = hi[i];
    }
    if (osqp_update_data_mat(&contact_solver, 0, 0, 0, A, 0, 42)) return -1;
    if (osqp_update_data_vec(&contact_solver, 0, l, u)) return -2;
    if (osqp_warm_start(&contact_solver, warm_x, warm_y)) return -3;
    if (osqp_solve(&contact_solver)) return -4;
    diagnostics[0] = contact_solver.info->iter;
    diagnostics[1] = contact_solver.info->prim_res;
    diagnostics[2] = contact_solver.info->dual_res;
    for (int i = 0; i < 6; ++i) x[i] = contact_solver.solution->x[i];
    for (int i = 0; i < 12; ++i) y[i] = contact_solver.solution->y[i];
    return contact_solver.info->status_val;
}
''', encoding="ascii")
    library = output / "libcontact_qp.so"
    sources = [wrapper, output / "contact_workspace.c", *sorted((output / "src").glob("*.c"))]
    subprocess.run(["cc", "-O3", "-std=c99", "-fPIC", "-shared", "-o", str(library),
                    "-I" + str(output), "-I" + str(output / "inc/public"),
                    "-I" + str(output / "inc/private"), *map(str, sources), "-lm"], check=True)
    (output / "build.json").write_text(json.dumps({
        "solver": "osqp", "version": osqp.__version__, "variables": 6,
        "constraints": 12, "max_iter": 400, "eps_abs": 1e-8, "eps_rel": 1e-8,
        "problem": "min 0.5*qdot.T*qdot; J*qdot=twist; lower<=qdot<=upper",
        "claim": "compiled solver only; no realtime or physical qualification",
    }, indent=2) + "\n", encoding="ascii")
    return library


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(build(parser.parse_args().output))
