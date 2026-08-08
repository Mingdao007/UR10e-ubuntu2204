"""Retired R008 diagnostic probe tombstone.

The former implementation installed an import-time source-closure bypass and
accepted a recovery hash for the mature writer.  That executable recovery path
is retired.  Historical diagnostics remain in the R008 audit/docs and git
history; importing this module cannot mutate validators or writers.
"""

from __future__ import annotations

from typing import Any, NoReturn


class R008ProbeBypassRetiredError(RuntimeError):
    """The historical probe/bypass is intentionally unavailable."""


def _retired(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise R008ProbeBypassRetiredError(
        "R008 diagnostic probe and source-closure recovery bypass are retired; "
        "use the historical read-only audit surface"
    )


install_source_closure_bypass = _retired
install_writer_probes = _retired
install_all = _retired


__all__ = [
    "R008ProbeBypassRetiredError",
    "install_all",
    "install_source_closure_bypass",
    "install_writer_probes",
]
