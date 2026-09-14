# tests/test_docs_repo.py
import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from backend import db, docs_repo


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
    n = docs_repo.delete_docs(["a/del"])
    assert n == 1
    assert docs_repo.get_latest("a/del") is None
    assert docs_repo.list_versions("a/del") == []
    blob_dir = os.path.join(os.environ["STORE_ROOT"], "a", "del")
    assert not os.path.exists(blob_dir)


def test_delete_docs_unknown_slug_counts_zero():
    assert docs_repo.delete_docs(["nope/nope"]) == 0


def test_delete_docs_partial_set():
    docs_repo.publish("a/keep", "Keep", [], None, "analyst", b"<h1>k</h1>")
    docs_repo.publish("a/drop", "Drop", [], None, "analyst", b"<h1>d</h1>")
    assert docs_repo.delete_docs(["a/drop", "missing/x"]) == 1
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
    """The reaper removes a slug's blob directory, so it must not run while a
    publish of that slug is mid-flight — the publish would write v1.html and
    the rmtree would take it away. Both sides take the same per-slug advisory
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
