"""Autotune V4 r008: domain repair over an unmodified r006 release.

r008 owns no campaign identity of its own.  It reuses r006's release
contract, campaign fingerprint and controller triplet verbatim, and changes
only host-side concerns: the parameter domain, the search that walks it, and
the controller seams that r006's frozen closure cannot express.

Nothing in this package performs controller, network, package or live-writer
I/O.  The greybox subpackage in particular is a pure offline reader of sealed
raw evidence.
"""

__all__: tuple[str, ...] = ()
