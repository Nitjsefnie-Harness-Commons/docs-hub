"""Backend environment settings with legacy-name fallbacks."""
import os


def setting(name: str, default: str | None = None) -> str | None:
    """Value for `name`: DOCS_HUB_<name> first, then legacy <name>, then
    default. An empty value counts as unset for BOTH spellings — the
    docs_hub_cli/_settings.py convention. When both are set the prefixed
    name wins."""
    return os.environ.get(f"DOCS_HUB_{name}") or os.environ.get(name) or default


def required(name: str) -> str:
    """Like setting() but raises when absent; the error names
    DOCS_HUB_<name> (the documented name)."""
    value = setting(name)
    if value is None:
        raise RuntimeError(f"DOCS_HUB_{name} is not set")
    return value
