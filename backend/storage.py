"""Document blob storage on local disk + slug validation.

Layout: <STORE_ROOT>/<slug>/v<N>.html or v<N>.md — one immutable file per
version, the extension following the version's format.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from pathlib import Path

# slug: lowercase alnum segments joined by '/'; segment chars [a-z0-9_-];
# each segment starts and ends alphanumeric; no '..', no leading/trailing '/'.
_SEGMENT = r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?"
_SLUG_RE = re.compile(rf"^{_SEGMENT}(/{_SEGMENT})*$")

# The extensions a version may be stored under, one per stored format.
# Checked alongside the slug so a caller that derives the extension wrongly
# cannot name a path outside that pair.
_EXTS = ("html", "md")
_VERSION_FILE_RE = re.compile(r"v[0-9]+\.(html|md)")


def is_valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= 200 and _SLUG_RE.fullmatch(slug) is not None


def _store_root() -> str:
    return os.environ["STORE_ROOT"]


def checked_path(path: str) -> str:
    """Resolve symlinks before checking that file access stays in the store."""
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(_store_root()).resolve()):
        raise ValueError(f"path escapes store: {path!r}")
    return str(resolved)


def blob_path(slug: str, version: int, ext: str = "html") -> str:
    """Absolute path for a version's file.

    Raises ValueError on a bad slug, an extension outside `_EXTS`, or a path
    resolving outside the store."""
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    if ext not in _EXTS:
        raise ValueError(f"invalid ext: {ext!r}")
    return checked_path(os.path.join(_store_root(), slug, f"v{version}.{ext}"))


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
    """Remove only the slug's top-level version files; absence is a no-op.

    After every unlink succeeds, rmdir removes the directory if empty.
    ENOTEMPTY is allowed because child slug directories or other entries
    remain. The row's data is its version files: a surviving version file is
    a deletion failure, while an empty directory that cannot be removed is
    inert. Stat and unlink errors, and rmdir errors other than ENOTEMPTY,
    propagate. Paths resolving outside the store raise ValueError.
    """
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    path = checked_path(os.path.join(_store_root(), slug))
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return
    for name in names:
        file_path = os.path.join(path, name)
        if _VERSION_FILE_RE.fullmatch(name):
            checked_path(file_path)
            if stat.S_ISREG(os.stat(file_path).st_mode):
                os.unlink(file_path)
    try:
        os.rmdir(path)
    except OSError as exc:
        if exc.errno != errno.ENOTEMPTY:
            raise
