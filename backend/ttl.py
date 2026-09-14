r"""Parse the `ttl` a publish carries into whole seconds.

Grammar: surrounding whitespace is stripped, then an integer with an optional
unit — s, m, h, d, w (case-insensitive), optionally separated by spaces; no
unit means seconds. Compound forms (`1h30m`), fractions and zero are rejected:
a TTL is a coarse "how long should this exist", not a schedule.

The text comes straight off a publish form, so it is length-capped before the
pattern sees it and the pattern itself is unambiguous — `[0-9]+`, one `\s*`
run and the unit match disjoint characters, so there is nothing to backtrack
over when a long input fails to match.

Ten years is the ceiling. Nothing here needs longer, and without a bound a
big enough number reaches Postgres as an interval it cannot represent, so
the publish fails with `interval out of range` instead of a clear rejection.
"""
from __future__ import annotations

import re

_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# [0-9] explicitly, and re.ASCII so `\s` is ASCII too: under Unicode semantics
# `\d` matches `\u0663` (Arabic-Indic three), a lifetime nobody typed.
_TTL_RE = re.compile(r"^([0-9]+)\s*([smhdw]?)$", re.IGNORECASE | re.ASCII)

# Longest legitimate ttl is a handful of characters; the cap is what keeps an
# arbitrarily long form value from reaching the pattern at all.
MAX_TTL_LENGTH = 32
MAX_TTL_SECONDS = 10 * 365 * 86400


def parse_ttl(text: str) -> int:
    """Seconds for a duration like `30m`, `2h`, `7d` or `90` (bare = seconds).
    Raises ValueError for anything else, including zero, anything over
    MAX_TTL_SECONDS, and any text longer than MAX_TTL_LENGTH characters."""
    text = text or ""
    if len(text) > MAX_TTL_LENGTH:
        raise ValueError(f"invalid ttl: {text!r}")
    # Stripped here rather than in the pattern: leading/trailing `\s*` runs
    # either side of an optional unit are what made the match ambiguous.
    m = _TTL_RE.match(text.strip(" \t\r\n"))
    if m is None:
        raise ValueError(f"invalid ttl: {text!r}")
    seconds = int(m.group(1)) * _UNITS[m.group(2).lower()]
    if not 1 <= seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"invalid ttl: {text!r}")
    return seconds
