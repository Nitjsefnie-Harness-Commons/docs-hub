import errno
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


@pytest.mark.parametrize("parent_first", [True, False])
def test_delete_doc_preserves_child_versions(parent_first):
    slugs = ["parent", "parent/child"]
    if not parent_first:
        slugs.reverse()
    for slug in slugs:
        storage.store_blob(slug, 1, b"<h1>first</h1>")
        storage.store_blob(slug, 2, b"# second\n", ext="md")
    root = os.path.join(os.environ["STORE_ROOT"], "parent")
    parent_files = [os.path.join(root, "v1.html"), os.path.join(root, "v2.md")]
    child_files = [os.path.join(root, "child", "v1.html"),
                   os.path.join(root, "child", "v2.md")]
    assert all(os.path.isfile(path) for path in parent_files + child_files)

    storage.delete_doc("parent")

    assert all(not os.path.exists(path) for path in parent_files)
    assert os.path.isdir(root)
    assert all(os.path.isfile(path) for path in child_files)
    assert storage.read_blob(child_files[0]) == b"<h1>first</h1>"
    assert storage.read_blob(child_files[1]) == b"# second\n"


def test_delete_doc_preserves_non_version_entries():
    storage.store_blob("entries", 1, b"version")
    root = os.path.join(os.environ["STORE_ROOT"], "entries")
    names = ("notes.html", "v.html", "v1.html.bak", "prefix-v2.md", "v3.txt")
    for name in names:
        with open(os.path.join(root, name), "wb") as f:
            f.write(b"keep")
    os.mkdir(os.path.join(root, "v4.html"))
    version_file = os.path.join(root, "v1.html")
    assert os.path.isfile(version_file)

    storage.delete_doc("entries")

    assert not os.path.exists(version_file)
    assert os.path.isdir(os.path.join(root, "v4.html"))
    for name in names:
        assert storage.read_blob(os.path.join(root, name)) == b"keep"


def test_delete_doc_propagates_unlink_failure(monkeypatch):
    path, _, _ = storage.store_blob("unlink-error", 1, b"keep")
    assert os.path.isfile(path)

    def fail_unlink(*args, **kwargs):
        raise OSError(errno.EACCES, "unlink denied")

    monkeypatch.setattr(os, "unlink", fail_unlink)
    with pytest.raises(OSError) as exc:
        storage.delete_doc("unlink-error")
    assert exc.value.errno == errno.EACCES
    assert storage.read_blob(path) == b"keep"


def test_delete_doc_propagates_rmdir_failure(monkeypatch):
    path, _, _ = storage.store_blob("rmdir-error", 1, b"version")
    assert os.path.isfile(path)

    def fail_rmdir(*args, **kwargs):
        raise OSError(errno.EACCES, "rmdir denied")

    monkeypatch.setattr(os, "rmdir", fail_rmdir)
    with pytest.raises(OSError) as exc:
        storage.delete_doc("rmdir-error")
    assert exc.value.errno == errno.EACCES
    assert not os.path.exists(path)


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
