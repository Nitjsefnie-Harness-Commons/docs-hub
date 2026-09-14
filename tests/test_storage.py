import hashlib
import os
import pytest
from backend import storage


def test_valid_slugs():
    for s in ("analyst/2026-05-21-audit", "a", "a-b_c", "x/y/z"):
        assert storage.is_valid_slug(s), s


def test_invalid_slugs():
    for s in ("", "/lead", "trail/", "../etc", "Up", "a b", "a..b", "-lead"):
        assert not storage.is_valid_slug(s), s


def test_invalid_slugs_cover_every_traversal_shape():
    # CodeQL reports blob_path's os.path.join as py/path-injection because it
    # cannot see through is_valid_slug, which is the sanitizer. These are the
    # inputs that would make the alert real if that regex ever loosened, so
    # they are asserted here rather than argued about in a dismissal comment.
    for s in ("../etc/passwd", "a/../../b", "/absolute", "a/./b", "..",
              "a\\..\\b", "a%2f..%2fb", "a\x00b", "~/x", "a//b", " a"):
        assert not storage.is_valid_slug(s), s


def test_blob_path_rejects_traversal():
    for s in ("../etc/passwd", "/absolute", "a/../../b"):
        with pytest.raises(ValueError):
            storage.blob_path(s, 1)


def test_store_and_read_blob():
    html = b"<!doctype html><h1>hi</h1>"
    path, size, digest = storage.store_blob("analyst/doc", 1, html)
    assert size == len(html)
    assert digest == hashlib.sha256(html).hexdigest()
    assert os.path.isfile(path)
    assert storage.read_blob(path) == html


def test_store_blob_rejects_bad_slug():
    with pytest.raises(ValueError):
        storage.store_blob("../evil", 1, b"x")


def test_delete_doc_removes_dir():
    storage.store_blob("analyst/del", 1, b"<h1>x</h1>")
    storage.store_blob("analyst/del", 2, b"<h1>y</h1>")
    root = os.path.join(os.environ["STORE_ROOT"], "analyst", "del")
    assert os.path.isdir(root)
    storage.delete_doc("analyst/del")
    assert not os.path.exists(root)


def test_delete_doc_missing_is_noop():
    storage.delete_doc("analyst/never-stored")  # must not raise


def test_delete_doc_rejects_bad_slug():
    with pytest.raises(ValueError):
        storage.delete_doc("../evil")


def test_blob_path_uses_the_requested_extension():
    assert storage.blob_path("a/b", 3, ext="md").endswith("a/b/v3.md")


def test_blob_path_defaults_to_html():
    assert storage.blob_path("a/b", 3).endswith("a/b/v3.html")


def test_store_blob_writes_the_requested_extension():
    src = b"# hi\n"
    path, size, digest = storage.store_blob("analyst/md", 1, src, ext="md")
    assert path.endswith("analyst/md/v1.md")
    assert size == len(src)
    assert digest == hashlib.sha256(src).hexdigest()
    assert storage.read_blob(path) == src


def test_blob_path_rejects_an_unknown_extension():
    # Defence in depth beside the slug check: the extension is chosen from the
    # version's format, so anything outside the two stored formats means a
    # caller got the mapping wrong.
    for ext in ("php", "html/../x", "", "HTML"):
        with pytest.raises(ValueError, match="invalid ext"):
            storage.blob_path("a/b", 1, ext=ext)


def test_blob_path_accepts_both_stored_extensions():
    assert storage.blob_path("a/b", 1, ext="html").endswith("v1.html")
    assert storage.blob_path("a/b", 1, ext="md").endswith("v1.md")
