import pytest

from backend.ttl import MAX_TTL_SECONDS, parse_ttl


@pytest.mark.parametrize("text,seconds", [
    ("30", 30), ("45s", 45), ("2m", 120), ("1h", 3600), ("3d", 259200),
    ("1w", 604800), (" 10M ", 600), ("1H", 3600),
    ("3650d", MAX_TTL_SECONDS),
])
def test_parse_ttl_accepts_each_unit(text, seconds):
    assert parse_ttl(text) == seconds


@pytest.mark.parametrize("text", [
    "", "  ", "0", "0h", "-1", "1x", "h", "1.5h", "1h30m", "abc", "1 h 2",
    "3651d", "99999999999w",
    # Arabic-Indic three. `\d` matches it under Unicode semantics, and the
    # int() that followed would happily read it as 3 -- a ttl spelled in
    # digits the operator did not type. The pattern is ASCII-only.
    "\u0663h",
])
def test_parse_ttl_rejects_bad_input(text):
    with pytest.raises(ValueError, match="invalid ttl"):
        parse_ttl(text)


def test_parse_ttl_rejects_an_absurdly_long_digit_string():
    # Past CPython's 4300-digit int()-from-str limit the conversion itself
    # raises ValueError, whose message is about digits, not about a ttl.
    with pytest.raises(ValueError, match="^invalid ttl"):
        parse_ttl("9" * 5000)
