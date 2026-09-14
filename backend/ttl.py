"""Parse the `ttl` a publish carries into whole seconds.

Grammar: an integer with an optional unit — s, m, h, d, w (case-insensitive);
no unit means seconds. Compound forms (`1h30m`), fractions and zero are
rejected: a TTL is a coarse "how long should this exist", not a schedule.

Ten years is the ceiling. Nothing here needs longer, and without a bound a
big enough number reaches Postgres as an interval it cannot represent, so
the publish fails with `interval out of range` instead of a clear rejection.
"""
from __future__ import annotations

import re

_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# re.ASCII so `\d` is [0-9] only: without it `\u0663h` (Arabic-Indic
# three) parses as three hours, a lifetime nobody typed.
_TTL_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$",
                     re.IGNORECASE | re.ASCII)

MAX_TTL_SECONDS = 10 * 365 * 86400


def parse_ttl(text: str) -> int:
    """Seconds for a duration like `30m`, `2h`, `7d` or `90` (bare = seconds).
    Raises ValueError for anything else, including zero and anything over
    MAX_TTL_SECONDS."""
    m = _TTL_RE.match(text or "")
    if m is None:
        raise ValueError(f"invalid ttl: {text!r}")
    try:
        # Past CPython's 4300-digit limit int() raises ValueError itself, with
        # a message about digit strings; the contract here is one message.
        digits = int(m.group(1))
    except ValueError as e:
        raise ValueError(f"invalid ttl: {text!r}") from e
    seconds = digits * _UNITS[m.group(2).lower()]
    if not 1 <= seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"invalid ttl: {text!r}")
    return seconds
