"""Parse the `ttl` a publish carries into whole seconds.

Grammar: an integer with an optional unit — s, m, h, d, w (case-insensitive);
no unit means seconds. Compound forms (`1h30m`), fractions and zero are
rejected: a TTL is a coarse "how long should this exist", not a schedule.
"""
from __future__ import annotations

import re

_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_TTL_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)


def parse_ttl(text: str) -> int:
    """Seconds for a duration like `30m`, `2h`, `7d` or `90` (bare = seconds).
    Raises ValueError for anything else, including zero."""
    m = _TTL_RE.match(text or "")
    if m is None:
        raise ValueError(f"invalid ttl: {text!r}")
    seconds = int(m.group(1)) * _UNITS[m.group(2).lower()]
    if seconds < 1:
        raise ValueError(f"invalid ttl: {text!r}")
    return seconds
