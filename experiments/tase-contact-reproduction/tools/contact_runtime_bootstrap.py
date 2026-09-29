"""Import side effect: make the process the canonical contact runtime.

Import this first (before numpy/pinocchio/project imports) in any entrypoint or
module that hard-requires the composed runtime.  Under the wrong Python it
re-execs the original command line under ``.venv-contact-six`` plus the ROS
Humble paths, using only stdlib process facts; see ``experiment_runtime``.
"""
from experiment_runtime import ensure_managed_runtime_on_import

ensure_managed_runtime_on_import()
