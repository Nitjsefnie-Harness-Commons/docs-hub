# tests/test_docs_repo.py
import errno
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from backend import db, docs_repo, storage


def test_publish_creates_v1():
    res = docs_repo.publish("analyst/x", "Title X", ["t1"], "proj", "analyst",
                            b"<h1>v1</h1>")
    assert res["version"] == 1
    assert res["slug"] == "analyst/x"
    latest = docs_repo.get_latest("analyst/x")
    assert latest["version"] == 1
    assert latest["html"] == b"<h1>v1</h1>"


def test_republish_increments_version():
    docs_repo.publish("analyst/x", "Title X", [], None, "analyst", b"<h1>v1</h1>")
    res2 = docs_repo.publish("analyst/x", "Title X v2", [], None, "kimi",
                             b"<h1>v2</h1>")
    assert res2["version"] == 2
    assert docs_repo.get_latest("analyst/x")["html"] == b"<h1>v2</h1>"
    # prior version still retrievable
    v1 = docs_repo.get_version("analyst/x", 1)
    assert v1["html"] == b"<h1>v1</h1>"


def _assert_no_publish_rows(slug):
    with db.docs_conn() as c:
        assert c.execute("SELECT count(*) FROM docs WHERE slug=%s",
                         (slug,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM doc_versions").fetchone()[0] == 0


@pytest.mark.parametrize("fmt, ext", [("html", "html"), ("markdown", "md")])
def test_publish_removes_partial_blob_on_write_failure(monkeypatch, fmt, ext):
    slug = "failure/partial-write-" + ext
    path = storage.blob_path(slug, 1, ext)
    error = OSError(errno.ENOSPC, "No space left on device", path)
    written = []

    def fail_write(candidate, version, body, ext="html"):
        partial_path = storage.blob_path(candidate, version, ext)
        assert partial_path == path
        os.makedirs(os.path.dirname(partial_path), exist_ok=True)
        with open(partial_path, "wb") as f:
            f.write(body[:3])
        assert storage.read_blob(partial_path) == b"par"
        written.append(partial_path)
        raise error

    monkeypatch.setattr(storage, "store_blob", fail_write)
    with pytest.raises(OSError) as exc:
        docs_repo.publish(slug, "Partial", [], None, "analyst", b"partial", fmt=fmt)
    assert exc.value is error
    assert written == [path]
    assert not os.path.exists(path)
    _assert_no_publish_rows(slug)


def test_failed_republish_preserves_prior_version(monkeypatch):
    docs_repo.publish("p/x", "Original", [], None, "analyst", b"<h1>v1</h1>")
    prior_path = storage.blob_path("p/x", 1, ext="html")
    path = storage.blob_path("p/x", 2, ext="html")
    error = OSError(errno.ENOSPC, "No space left on device", path)
    written = []
    assert os.path.isfile(prior_path)
    assert storage.read_blob(prior_path) == b"<h1>v1</h1>"
    assert docs_repo.get_version("p/x", 1)["html"] == b"<h1>v1</h1>"

    def fail_write(candidate, version, body, ext="html"):
        partial_path = storage.blob_path(candidate, version, ext)
        assert partial_path == path
        with open(partial_path, "wb") as f:
            f.write(body[:3])
        assert storage.read_blob(partial_path) == b"<h1"
        written.append(partial_path)
        raise error

    monkeypatch.setattr(storage, "store_blob", fail_write)
    with pytest.raises(OSError) as exc:
        docs_repo.publish("p/x", "Replacement", [], None, "analyst", b"<h1>v2</h1>")
    assert exc.value is error
    assert written == [path]
    assert not os.path.exists(path)
    # Failed-republish cleanup must preserve the prior version's file.
    assert os.path.isfile(prior_path)
    assert storage.read_blob(prior_path) == b"<h1>v1</h1>"
    assert docs_repo.get_version("p/x", 1)["html"] == b"<h1>v1</h1>"
    assert [v["version"] for v in docs_repo.list_versions("p/x")] == [1]
    with db.docs_conn() as c:
        assert c.execute("SELECT latest_version FROM docs WHERE slug=%s",
                         ("p/x",)).fetchall() == [(1,)]


@pytest.mark.parametrize("failure", ["insert", "update", "commit"])
def test_publish_cleans_blob_only_before_commit(monkeypatch, failure):
    slug = "failure/transaction-" + failure
    path = storage.blob_path(slug, 1)
    body = b"complete version"
    error = RuntimeError("injected " + failure + " failure")
    failed = []
    connect = db.docs_conn

    class Connection:
        def __init__(self, real):
            self.real = real

        def execute(self, query, params=None):
            if failure == "insert" and query.startswith("INSERT INTO doc_versions "):
                assert query.startswith("INSERT INTO doc_versions (doc_id, version, ")
                assert storage.read_blob(path) == body
                failed.append("insert")
                raise error
            if failure == "update" and query.startswith("UPDATE docs SET "):
                assert query.startswith("UPDATE docs SET latest_version=")
                assert storage.read_blob(path) == body
                failed.append("update")
                raise error
            return self.real.execute(query, params)

        def commit(self):
            if failure == "commit":
                assert storage.read_blob(path) == body
                failed.append("commit")
                raise error
            return self.real.commit()

    @contextmanager
    def failing_connection():
        with connect() as c:
            yield Connection(c)

    with monkeypatch.context() as patch:
        patch.setattr(docs_repo.db, "docs_conn", failing_connection)
        with pytest.raises(RuntimeError) as exc:
            docs_repo.publish(slug, "Transaction", [], None, "analyst", body)
    assert exc.value is error
    assert failed == [failure]
    if failure == "commit":
        assert storage.read_blob(path) == body
    else:
        assert not os.path.exists(path)
    _assert_no_publish_rows(slug)


@pytest.mark.parametrize("write_file", [False, True])
def test_publish_preserves_original_error_when_cleanup_fails(monkeypatch, caplog, write_file):
    slug = "failure/cleanup-" + str(write_file).lower()
    path = storage.blob_path(slug, 1)
    error = KeyboardInterrupt("interrupted write")
    unlink = os.unlink

    def fail_write(candidate, version, body, ext="html"):
        assert storage.blob_path(candidate, version, ext) == path
        if write_file:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(body[:3])
            assert storage.read_blob(path) == b"par"
        raise error

    def fail_unlink(candidate):
        if candidate == path:
            raise OSError(errno.EIO, "Input/output error", candidate)
        return unlink(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "store_blob", fail_write)
        if write_file:
            patch.setattr(os, "unlink", fail_unlink)
        with caplog.at_level(logging.ERROR, logger="backend.docs_repo"):
            with pytest.raises(KeyboardInterrupt) as exc:
                docs_repo.publish(slug, "Cleanup", [], None, "analyst", b"partial")
    assert exc.value is error
    _assert_no_publish_rows(slug)
    records = [r for r in caplog.records if r.name == "backend.docs_repo"]
    if write_file:
        assert storage.read_blob(path) == b"par"
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert path in records[0].getMessage()
        assert "Input/output error" in records[0].getMessage()
    else:
        assert not os.path.exists(path)
        assert records == []


def test_get_missing_returns_none():
    assert docs_repo.get_latest("nope/nope") is None
    assert docs_repo.get_version("nope/nope", 9) is None


def test_list_and_filter():
    docs_repo.publish("a/one", "One", [], "alpha", "analyst", b"<h1>1</h1>")
    docs_repo.publish("a/two", "Two", [], "beta", "kimi", b"<h1>2</h1>")
    assert {d["slug"] for d in docs_repo.list_docs()} == {"a/one", "a/two"}
    assert [d["slug"] for d in docs_repo.list_docs(project="alpha")] == ["a/one"]


def test_list_versions():
    docs_repo.publish("a/v", "V", [], None, "analyst", b"<h1>1</h1>")
    docs_repo.publish("a/v", "V", [], None, "analyst", b"<h1>2</h1>")
    vs = docs_repo.list_versions("a/v")
    assert [v["version"] for v in vs] == [2, 1]


def test_publish_bad_slug_raises():
    with pytest.raises(ValueError):
        docs_repo.publish("../evil", "X", [], None, "analyst", b"x")


def test_list_docs_includes_byte_size():
    body = b"<h1>sized body</h1>"
    docs_repo.publish("a/sz", "Sized", [], None, "analyst", body)
    d = docs_repo.list_docs()[0]
    assert d["byte_size"] == len(body)


def test_find_docs_by_each_filter():
    docs_repo.publish("a/one", "Alpha One", ["x"], "alpha", "analyst", b"<h1>1</h1>")
    docs_repo.publish("a/two", "Beta Two", ["y"], "beta", "kimi", b"<h1>2</h1>")
    assert [d["slug"] for d in docs_repo.find_docs({"slug": "a/one"})] == ["a/one"]
    assert [d["slug"] for d in docs_repo.find_docs({"project": "alpha"})] == ["a/one"]
    assert [d["slug"] for d in docs_repo.find_docs({"author": "kimi"})] == ["a/two"]
    assert [d["slug"] for d in docs_repo.find_docs({"tag": "x"})] == ["a/one"]
    assert [d["slug"] for d in docs_repo.find_docs({"q": "beta"})] == ["a/two"]
    assert {d["slug"] for d in docs_repo.find_docs({})} == {"a/one", "a/two"}


def test_find_docs_and_combines():
    docs_repo.publish("a/one", "Alpha One", [], "alpha", "analyst", b"<h1>1</h1>")
    docs_repo.publish("a/two", "Alpha Two", [], "alpha", "kimi", b"<h1>2</h1>")
    res = docs_repo.find_docs({"project": "alpha", "author": "kimi"})
    assert [d["slug"] for d in res] == ["a/two"]


def test_find_docs_shape():
    docs_repo.publish("a/one", "Alpha One", [], "alpha", "analyst", b"<h1>1</h1>")
    d = docs_repo.find_docs({"project": "alpha"})[0]
    assert set(d) == {"slug", "title", "latest_version"}


def test_delete_docs_removes_rows_versions_blobs():
    docs_repo.publish("a/del", "Del", [], None, "analyst", b"<h1>1</h1>")
    docs_repo.publish("a/del", "Del", [], None, "analyst", b"<h1>2</h1>")
    blob_dir = os.path.join(os.environ["STORE_ROOT"], "a", "del")
    assert os.path.isdir(blob_dir)
    assert docs_repo.get_latest("a/del") is not None
    assert len(docs_repo.list_versions("a/del")) == 2
    n = docs_repo.delete_docs(["a/del"])
    assert n == (1, [])
    assert docs_repo.get_latest("a/del") is None
    assert docs_repo.list_versions("a/del") == []
    assert not os.path.exists(blob_dir)


def test_delete_docs_unknown_slug_counts_zero():
    assert docs_repo.delete_docs(["nope/nope"]) == (0, [])


def test_delete_docs_partial_set():
    docs_repo.publish("a/keep", "Keep", [], None, "analyst", b"<h1>k</h1>")
    docs_repo.publish("a/drop", "Drop", [], None, "analyst", b"<h1>d</h1>")
    assert docs_repo.get_latest("a/drop") is not None
    assert docs_repo.delete_docs(["a/drop", "missing/x"]) == (1, [])
    assert docs_repo.get_latest("a/keep") is not None
    assert docs_repo.get_latest("a/drop") is None


def test_list_tags_empty():
    assert docs_repo.list_tags() == []


def test_list_tags_distinct_with_counts_most_used_first():
    docs_repo.publish("a/one", "One", ["spec", "draft"], "alpha", "analyst", b"<h1>1</h1>")
    docs_repo.publish("a/two", "Two", ["spec"], "beta", "kimi", b"<h1>2</h1>")
    tags = docs_repo.list_tags()
    by = {t["tag"]: t["count"] for t in tags}
    assert by == {"spec": 2, "draft": 1}
    assert tags[0]["tag"] == "spec" and tags[0]["count"] == 2


def test_list_tags_scoped_to_project():
    docs_repo.publish("a/one", "One", ["spec"], "alpha", "analyst", b"<h1>1</h1>")
    docs_repo.publish("a/two", "Two", ["draft"], "beta", "kimi", b"<h1>2</h1>")
    assert docs_repo.list_tags(project="alpha") == [{"tag": "spec", "count": 1}]


def _backdate(slug):
    with db.docs_conn() as c:
        c.execute("UPDATE docs SET expires_at = now() - interval '1 second' "
                  "WHERE slug=%s", (slug,))
        c.commit()


def _assert_removal_error(caplog, slug, path):
    records = [r for r in caplog.records if r.name == "backend.docs_repo"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    message = records[0].getMessage()
    assert f"for {slug!r} at " in message
    assert f"at {os.path.dirname(path)!r}:" in message
    assert path in message
    assert "[Errno 13] Permission denied" in message


@pytest.mark.parametrize("partial", [False, True])
def test_delete_docs_keeps_failed_slug_and_continues(monkeypatch, caplog, partial):
    slug = "failure/delete"
    docs_repo.publish(slug, "Old", [], None, "analyst", b"old")
    docs_repo.publish(slug, "Latest", [], None, "analyst", b"latest")
    docs_repo.publish("failure/other", "Other", [], None, "analyst", b"other")
    first_path = storage.blob_path(slug, 1)
    path = storage.blob_path(slug, 2)
    other_path = storage.blob_path("failure/other", 1)
    assert docs_repo.get_latest(slug)["html"] == b"latest"
    assert docs_repo.get_latest("failure/other")["html"] == b"other"
    assert os.path.isfile(first_path)
    assert os.path.isfile(path)
    assert os.path.isfile(other_path)
    if partial:
        os.unlink(first_path)
    remove = storage.delete_doc

    def fail_one(candidate):
        if candidate == slug:
            raise OSError(errno.EACCES, "Permission denied", path)
        remove(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "delete_doc", fail_one)
        with caplog.at_level(logging.ERROR, logger="backend.docs_repo"):
            assert docs_repo.delete_docs([slug, "failure/other"]) == (1, [slug])
    assert docs_repo.get_latest(slug)["html"] == b"latest"
    assert [v["version"] for v in docs_repo.list_versions(slug)] == [2, 1]
    assert os.path.isfile(path)
    assert os.path.exists(first_path) is not partial
    assert [d["slug"] for d in docs_repo.list_docs()] == [slug]
    assert docs_repo.get_latest("failure/other") is None
    assert not os.path.exists(other_path)
    _assert_removal_error(caplog, slug, path)

    assert docs_repo.delete_docs([slug]) == (1, [])
    assert docs_repo.get_latest(slug) is None
    assert not os.path.exists(first_path)
    assert not os.path.exists(path)


@pytest.mark.parametrize("with_success", [False, True])
def test_purge_expired_keeps_failed_row_for_retry(monkeypatch, caplog, with_success):
    slug = "failure/purge"
    docs_repo.publish(slug, "Old", [], None, "analyst", b"old")
    assert docs_repo.get_latest(slug)["html"] == b"old"
    path = storage.blob_path(slug, 1)
    assert os.path.isfile(path)
    _backdate(slug)
    with db.docs_conn() as c:
        before = c.execute("SELECT id, expires_at FROM docs WHERE slug=%s",
                           (slug,)).fetchone()
    assert before is not None
    if with_success:
        docs_repo.publish("failure/purge-other", "Other", [], None, "analyst", b"other")
        assert docs_repo.get_latest("failure/purge-other") is not None
        _backdate("failure/purge-other")
    remove = storage.delete_doc

    def fail_one(candidate):
        if candidate == slug:
            raise OSError(errno.EACCES, "Permission denied", path)
        remove(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "delete_doc", fail_one)
        with caplog.at_level(logging.ERROR, logger="backend.docs_repo"):
            assert docs_repo.purge_expired() == int(with_success)
    with db.docs_conn() as c:
        assert c.execute("SELECT id, expires_at FROM docs WHERE slug=%s",
                         (slug,)).fetchone() == before
        assert c.execute("SELECT slug FROM docs").fetchall() == [(slug,)]
    assert storage.read_blob(path) == b"old"
    _assert_removal_error(caplog, slug, path)

    assert docs_repo.purge_expired() == 1
    with db.docs_conn() as c:
        assert c.execute("SELECT id FROM docs WHERE slug=%s", (slug,)).fetchone() is None
    assert not os.path.exists(path)


def test_expired_publish_keeps_old_life_when_removal_fails(monkeypatch, caplog):
    slug = "failure/publish"
    docs_repo.publish(slug, "Old", [], None, "analyst", b"old")
    assert docs_repo.get_latest(slug)["html"] == b"old"
    _backdate(slug)
    path = storage.blob_path(slug, 1)
    directory = os.path.dirname(path)
    assert os.listdir(directory) == ["v1.html"]
    with db.docs_conn() as c:
        before = c.execute(
            "SELECT d.id, d.expires_at, d.latest_version, v.version, v.file_path "
            "FROM docs d JOIN doc_versions v ON v.doc_id=d.id WHERE d.slug=%s",
            (slug,)).fetchall()
    assert len(before) == 1

    def fail_remove(_slug):
        raise OSError(errno.EACCES, "Permission denied", path)

    monkeypatch.setattr(storage, "delete_doc", fail_remove)
    with caplog.at_level(logging.ERROR, logger="backend.docs_repo"):
        with pytest.raises(OSError) as exc:
            docs_repo.publish(slug, "New", [], None, "analyst", b"# new", fmt="markdown")
    assert exc.value.errno == errno.EACCES
    with db.docs_conn() as c:
        assert c.execute(
            "SELECT d.id, d.expires_at, d.latest_version, v.version, v.file_path "
            "FROM docs d JOIN doc_versions v ON v.doc_id=d.id WHERE d.slug=%s",
            (slug,)).fetchall() == before
    assert storage.read_blob(path) == b"old"
    assert os.listdir(directory) == ["v1.html"]
    _assert_removal_error(caplog, slug, path)


@pytest.mark.parametrize("removal", ["delete", "purge"])
def test_removal_counts_an_already_absent_directory(removal):
    slug = "absent/" + removal
    docs_repo.publish(slug, "Gone", [], None, "analyst", b"old")
    assert docs_repo.get_latest(slug) is not None
    if removal == "purge":
        _backdate(slug)
    with db.docs_conn() as c:
        assert c.execute("SELECT id FROM docs WHERE slug=%s", (slug,)).fetchone() is not None
    storage.delete_doc(slug)
    if removal == "delete":
        assert docs_repo.delete_docs([slug]) == (1, [])
    else:
        assert docs_repo.purge_expired() == 1
    with db.docs_conn() as c:
        assert c.execute("SELECT id FROM docs WHERE slug=%s", (slug,)).fetchone() is None


@pytest.mark.parametrize("parent_first", [True, False])
@pytest.mark.parametrize("removal", ["delete", "purge", "republish"])
def test_parent_removal_preserves_child_versions(parent_first, removal):
    slugs = ["family", "family/child"]
    if not parent_first:
        slugs.reverse()
    for slug in slugs:
        docs_repo.publish(slug, "Family", [], None, "analyst", b"<h1>v1</h1>")
        docs_repo.publish(slug, "Family", [], None, "analyst", b"# v2\n",
                          fmt="markdown")
    child_versions = [docs_repo.get_version("family/child", n) for n in (1, 2)]
    child_files = [os.path.join(os.environ["STORE_ROOT"], "family", "child", name)
                   for name in ("v1.html", "v2.md")]
    child_stats = [os.stat(path) for path in child_files]
    old_parent_file = os.path.join(os.environ["STORE_ROOT"], "family", "v2.md")
    assert os.path.isfile(old_parent_file)
    assert docs_repo.get_latest("family") is not None

    if removal == "delete":
        assert docs_repo.delete_docs(["family"]) == (1, [])
    else:
        _backdate("family")
        if removal == "purge":
            assert docs_repo.purge_expired() == 1
        else:
            result = docs_repo.publish("family", "New", [], None, "analyst",
                                       b"<h1>new</h1>")
            assert result["version"] == 1
            assert docs_repo.get_latest("family")["html"] == b"<h1>new</h1>"
            assert [v["version"] for v in docs_repo.list_versions("family")] == [1]

    assert not os.path.exists(old_parent_file)
    assert [os.stat(path) for path in child_files] == child_stats
    assert [v["version"] for v in docs_repo.list_versions("family/child")] == [2, 1]
    assert [docs_repo.get_version("family/child", n) for n in (1, 2)] == child_versions
    assert docs_repo.get_version("family/child", 1)["html"] == b"<h1>v1</h1>"
    assert docs_repo.get_latest("family/child")["html"] == b"# v2\n"
    if removal != "republish":
        assert docs_repo.get_latest("family") is None


def test_publish_with_ttl_sets_expires_at_in_the_future():
    before = datetime.now(timezone.utc)
    res = docs_repo.publish("e/one", "E", [], None, "analyst", b"<h1>1</h1>",
                            ttl_seconds=3600)
    assert res["expires_at"] is not None
    assert before + timedelta(minutes=59) < res["expires_at"]
    assert res["expires_at"] < before + timedelta(minutes=61)
    d = next(x for x in docs_repo.list_docs() if x["slug"] == "e/one")
    assert d["expires_at"] == res["expires_at"]


def test_publish_without_ttl_is_permanent():
    res = docs_repo.publish("e/perm", "E", [], None, "analyst", b"<h1>1</h1>")
    assert res["expires_at"] is None
    d = next(x for x in docs_repo.list_docs() if x["slug"] == "e/perm")
    assert d["expires_at"] is None


def test_republish_without_ttl_clears_expiry():
    docs_repo.publish("e/clear", "E", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=60)
    res = docs_repo.publish("e/clear", "E", [], None, "analyst", b"<h1>2</h1>")
    assert res["version"] == 2
    assert res["expires_at"] is None


def test_republish_with_ttl_resets_expiry_from_now():
    docs_repo.publish("e/reset", "E", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=60)
    res = docs_repo.publish("e/reset", "E", [], None, "analyst", b"<h1>2</h1>",
                            ttl_seconds=7200)
    assert res["version"] == 2
    assert res["expires_at"] > datetime.now(timezone.utc) + timedelta(hours=1)


def test_expired_doc_is_invisible_on_every_read_path():
    docs_repo.publish("e/gone", "Gone", ["t"], "proj", "analyst",
                      b"<h1>1</h1>", ttl_seconds=60)
    docs_repo.set_public("e/gone", True)
    _backdate("e/gone")
    assert docs_repo.get_latest("e/gone") is None
    assert docs_repo.get_version("e/gone", 1) is None
    assert docs_repo.list_versions("e/gone") == []
    assert all(d["slug"] != "e/gone" for d in docs_repo.list_docs())
    assert docs_repo.find_docs({"slug": "e/gone"}) == []
    assert docs_repo.find_docs({"tag": "t"}) == []
    assert docs_repo.list_tags() == []
    assert docs_repo.is_public("e/gone") is False
    assert docs_repo.set_public("e/gone", False) is False


def test_live_ttl_doc_is_still_readable():
    docs_repo.publish("e/live", "Live", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=3600)
    assert docs_repo.get_latest("e/live")["html"] == b"<h1>1</h1>"
    assert [d["slug"] for d in docs_repo.list_docs()] == ["e/live"]


def test_republish_of_expired_slug_starts_fresh_at_v1():
    docs_repo.publish("e/again", "A", [], None, "analyst", b"<h1>old</h1>",
                      ttl_seconds=60)
    _backdate("e/again")
    old_blob = os.path.join(os.environ["STORE_ROOT"], "e/again", "v1.html")
    assert os.path.exists(old_blob)
    res = docs_repo.publish("e/again", "A", [], None, "analyst", b"<h1>new</h1>")
    assert res["version"] == 1
    assert docs_repo.get_version("e/again", 1)["html"] == b"<h1>new</h1>"
    assert docs_repo.list_versions("e/again")[0]["version"] == 1
    assert len(docs_repo.list_versions("e/again")) == 1


def test_purge_expired_removes_rows_and_blobs():
    docs_repo.publish("e/p1", "P", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=60)
    docs_repo.publish("e/p1", "P", [], None, "analyst", b"<h1>2</h1>",
                      ttl_seconds=60)
    docs_repo.publish("e/keep", "K", [], None, "analyst", b"<h1>k</h1>",
                      ttl_seconds=60)
    docs_repo.publish("e/perm", "K", [], None, "analyst", b"<h1>k</h1>")
    _backdate("e/p1")
    assert docs_repo.purge_expired() == 1
    assert not os.path.exists(os.path.join(os.environ["STORE_ROOT"], "e/p1"))
    with db.docs_conn() as c:
        assert c.execute("SELECT count(*) FROM docs WHERE slug='e/p1'"
                         ).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM doc_versions").fetchone()[0] == 2
    assert {d["slug"] for d in docs_repo.list_docs()} == {"e/keep", "e/perm"}
    assert docs_repo.purge_expired() == 0


def test_purge_expired_waits_for_the_per_slug_lock():
    """The reaper removes only a slug's own version files, preserving descendant
    slug directories. It must not run while a publish of that slug is mid-flight:
    it could unlink the new v1.html. Both sides take the same per-slug advisory
    lock; here a third connection holds it and purge_expired has to wait."""
    docs_repo.publish("e/lock", "L", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=60)
    _backdate("e/lock")
    done, purged = threading.Event(), []

    def run():
        purged.append(docs_repo.purge_expired())
        done.set()

    with psycopg.connect(os.environ["DATABASE_URL_DOCS"]) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(hashtext('e/lock'))")
        threading.Thread(target=run, daemon=True).start()
        assert not done.wait(0.3), "purge_expired ignored the per-slug lock"
    # Leaving the block ends holder's transaction, dropping the lock.
    assert done.wait(10), "purge_expired never finished once the lock was freed"
    assert purged == [1]
    assert not os.path.exists(os.path.join(os.environ["STORE_ROOT"], "e/lock"))


def test_delete_docs_waits_for_the_per_slug_lock():
    """delete_docs removes only a slug's own version files, preserving descendant
    slug directories. It needs the same lock because it could unlink v1.html
    written by a publish of that slug mid-flight."""
    docs_repo.publish("e/dlock", "D", [], None, "analyst", b"<h1>1</h1>")
    assert docs_repo.get_latest("e/dlock") is not None
    assert os.path.isdir(os.path.join(os.environ["STORE_ROOT"], "e/dlock"))
    done, deleted = threading.Event(), []

    def run():
        deleted.append(docs_repo.delete_docs(["e/dlock"]))
        done.set()

    with psycopg.connect(os.environ["DATABASE_URL_DOCS"]) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(hashtext('e/dlock'))")
        threading.Thread(target=run, daemon=True).start()
        assert not done.wait(0.3), "delete_docs ignored the per-slug lock"
    # Leaving the block ends holder's transaction, dropping the lock.
    assert done.wait(10), "delete_docs never finished once the lock was freed"
    assert deleted == [(1, [])]
    assert not os.path.exists(os.path.join(os.environ["STORE_ROOT"], "e/dlock"))


def test_publish_markdown_stores_md_blob_and_format():
    res = docs_repo.publish("m/one", "M", [], None, "analyst", b"# hi\n",
                            fmt="markdown")
    assert res["format"] == "markdown"
    v = docs_repo.get_version("m/one", 1)
    assert v["format"] == "markdown"
    assert v["html"] == b"# hi\n"
    assert v["file_path"].endswith("m/one/v1.md")
    assert os.path.exists(v["file_path"])


def test_publish_default_format_is_html():
    docs_repo.publish("m/h", "H", [], None, "analyst", b"<h1>h</h1>")
    v = docs_repo.get_latest("m/h")
    assert v["format"] == "html"
    assert v["file_path"].endswith("m/h/v1.html")


def test_versions_of_one_slug_may_differ_in_format():
    docs_repo.publish("m/mix", "X", [], None, "analyst", b"<h1>1</h1>")
    docs_repo.publish("m/mix", "X", [], None, "analyst", b"# 2\n", fmt="markdown")
    vs = docs_repo.list_versions("m/mix")
    assert [(v["version"], v["format"]) for v in vs] == [(2, "markdown"), (1, "html")]
    d = next(x for x in docs_repo.list_docs() if x["slug"] == "m/mix")
    assert d["format"] == "markdown"


def test_publish_rejects_unknown_format():
    with pytest.raises(ValueError, match="invalid format"):
        docs_repo.publish("m/bad", "B", [], None, "analyst", b"x", fmt="rtf")
    # The rejection happens before anything is written, so the slug is left
    # with neither a doc row nor a blob directory.
    assert docs_repo.get_latest("m/bad") is None
    assert not os.path.exists(os.path.join(os.environ["STORE_ROOT"], "m", "bad"))
