import pytest

from backend.ttl import parse_ttl


@pytest.mark.parametrize("text,seconds", [
    ("30", 30), ("45s", 45), ("2m", 120), ("1h", 3600), ("3d", 259200),
    ("1w", 604800), (" 10M ", 600), ("1H", 3600),
])
def test_parse_ttl_accepts_each_unit(text, seconds):
    assert parse_ttl(text) == seconds


@pytest.mark.parametrize("text", [
    "", "  ", "0", "0h", "-1", "1x", "h", "1.5h", "1h30m", "abc", "1 h 2",
])
def test_parse_ttl_rejects_bad_input(text):
    with pytest.raises(ValueError, match="invalid ttl"):
        parse_ttl(text)
