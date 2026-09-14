"""Document blob storage on local disk + slug validation.

Layout: <STORE_ROOT>/<slug>/v<N>.html or v<N>.md — one immutable file per
version, the extension following the version's format.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil

# slug: lowercase alnum segments joined by '/'; segment chars [a-z0-9_-];
# each segment starts and ends alphanumeric; no '..', no leading/trailing '/'.
_SEGMENT = r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?"
_SLUG_RE = re.compile(rf"^{_SEGMENT}(/{_SEGMENT})*$")

# The extensions a version may be stored under, one per stored format.
# Checked alongside the slug so a caller that derives the extension wrongly
# cannot name a path outside that pair.
_EXTS = ("html", "md")


def is_valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= 200 and _SLUG_RE.fullmatch(slug) is not None


def _store_root() -> str:
    return os.environ["STORE_ROOT"]


def blob_path(slug: str, version: int, ext: str = "html") -> str:
    """Absolute path for a version's file.

    Raises ValueError on a bad slug or an extension outside `_EXTS`."""
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    if ext not in _EXTS:
        raise ValueError(f"invalid ext: {ext!r}")
    return os.path.join(_store_root(), slug, f"v{version}.{ext}")


def store_blob(slug: str, version: int, html: bytes,
               ext: str = "html") -> tuple[str, int, str]:
    """Write the version's file verbatim. Returns (path, byte_size, sha256_hex)."""
    path = blob_path(slug, version, ext)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(html)
    return path, len(html), hashlib.sha256(html).hexdigest()


def read_blob(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def delete_doc(slug: str) -> None:
    """Remove the blob directory for a slug. No-op if it does not exist."""
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    path = os.path.join(_store_root(), slug)
    shutil.rmtree(path, ignore_errors=True)
