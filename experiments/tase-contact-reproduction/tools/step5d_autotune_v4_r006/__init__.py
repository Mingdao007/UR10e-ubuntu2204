"""Offline, content-addressed Autotune V4 r006 implementation.

The package has no controller or network side effects.  Live delivery and
acceptance remain separate owner-gated states; this package only supplies the
typed offline contract and deterministic preparation harness.
"""

from .contracts import R006Contract, load_contract

__all__ = ["R006Contract", "load_contract"]
