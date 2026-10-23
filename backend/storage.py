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


def blob_path(slug: str, version: int, ext: str = "html") -> str:
    """Absolute path for a version's file.

    Raises ValueError on a bad slug, an extension outside `_EXTS`, or a path
    resolving outside the store."""
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    if ext not in _EXTS:
        raise ValueError(f"invalid ext: {ext!r}")
    root = os.path.realpath(_store_root())
    path = os.path.realpath(os.path.join(_store_root(), slug, f"v{version}.{ext}"))
    # Every checked path is strictly below the store root, so include the separator.
    if not path.startswith(root + os.sep):
        raise ValueError(f"path escapes store: {slug!r}")
    return path


def store_blob(slug: str, version: int, html: bytes,
               ext: str = "html") -> tuple[str, int, str]:
    """Write the version's file verbatim. Returns (path, byte_size, sha256_hex)."""
    root = os.path.realpath(_store_root())
    path = os.path.realpath(blob_path(slug, version, ext))
    if not path.startswith(root + os.sep):
        raise ValueError(f"path escapes store: {slug!r}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(html)
    return path, len(html), hashlib.sha256(html).hexdigest()


def read_blob(path: str) -> bytes:
    root = os.path.realpath(_store_root())
    path = os.path.realpath(path)
    if not path.startswith(root + os.sep):
        raise ValueError(f"path escapes store: {path!r}")
    with open(path, "rb") as f:
        return f.read()


def delete_doc(slug: str) -> None:
    """Remove a slug's own regular version files; listdir absence is a no-op.

    Each matching entry is classified with lstat, whose errors propagate.
    Directories are skipped; other nonregular entries raise OSError(EPERM).
    Regular entries are realpath-guarded inside the store, then unlinked.
    Escaping directory or regular-file paths raise OSError(EPERM).
    Finally rmdir removes an empty directory; ENOTEMPTY preserves children
    and other entries, while other rmdir errors propagate.
    """
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    root = os.path.realpath(_store_root())
    path = os.path.realpath(os.path.join(_store_root(), slug))
    if not path.startswith(root + os.sep):
        raise OSError(errno.EPERM, "path escapes store", path)
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return
    for name in names:
        if _VERSION_FILE_RE.fullmatch(name):
            file_path = os.path.normpath(os.path.join(path, name))
            if not file_path.startswith(root + os.sep):
                raise OSError(errno.EPERM, "path escapes store", file_path)
            st = os.lstat(file_path)
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EPERM, "not a regular file", file_path)
            file_path = os.path.realpath(file_path)
            if not file_path.startswith(root + os.sep):
                raise OSError(errno.EPERM, "path escapes store", file_path)
            os.unlink(file_path)
    try:
        os.rmdir(path)
    except OSError as exc:
        if exc.errno != errno.ENOTEMPTY:
            raise
