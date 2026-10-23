"""Contain stored-file access and escape file-removal diagnostics."""
import errno
import logging
import os
import stat

import pytest
from fastapi.testclient import TestClient

from backend import db, docs_repo, storage
from backend.app import app


@pytest.mark.parametrize("operation", ["blob_path", "store_blob", "delete_doc"])
@pytest.mark.parametrize("symlink", ["directory", "version"])
def test_storage_refuses_symlink_escape(tmp_path, monkeypatch, operation, symlink):
    root = tmp_path / "store"
    outside = tmp_path / "store-outside" / "x"
    root.mkdir()
    outside.mkdir(parents=True)
    sentinel = outside / "v1.html"
    sentinel.write_bytes(b"outside version")
    monkeypatch.setenv("STORE_ROOT", str(root))
    if symlink == "directory":
        (root / "link").symlink_to(outside.parent, target_is_directory=True)
    else:
        (root / "link" / "x").mkdir(parents=True)
        (root / "link" / "x" / "v1.html").symlink_to(sentinel)
    assert sentinel.read_bytes() == b"outside version"
    assert outside.is_dir()

    error_type = OSError if operation == "delete_doc" else ValueError
    with pytest.raises(error_type):
        if operation == "blob_path":
            storage.blob_path("link/x", 1)
        elif operation == "store_blob":
            storage.store_blob("link/x", 1, b"replacement")
        else:
            storage.delete_doc("link/x")

    assert sentinel.read_bytes() == b"outside version"
    assert outside.is_dir()


def test_delete_refuses_symlink_to_empty_outside_directory(tmp_path, monkeypatch):
    root = tmp_path / "store"
    outside = tmp_path / "store-outside" / "x"
    root.mkdir()
    outside.mkdir(parents=True)
    (root / "link").symlink_to(outside.parent, target_is_directory=True)
    monkeypatch.setenv("STORE_ROOT", str(root))
    assert outside.is_dir()

    with pytest.raises(OSError) as exc:
        storage.delete_doc("link/x")
    assert exc.value.errno == errno.EPERM
    assert outside.is_dir()


def test_delete_refuses_internal_version_symlink_without_removing_child(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_ROOT", str(tmp_path))
    child_path, _, _ = storage.store_blob("parent/child", 1, b"child version")
    link = tmp_path / "parent" / "v1.html"
    link.symlink_to(child_path)
    assert link.read_bytes() == b"child version"
    assert storage.read_blob(child_path) == b"child version"

    with pytest.raises(OSError) as exc:
        storage.delete_doc("parent")
    assert exc.value.errno == errno.EPERM
    assert exc.value.filename == str(link)
    assert link.is_symlink()
    assert storage.read_blob(child_path) == b"child version"


def test_get_version_refuses_stored_path_outside_store(tmp_path, monkeypatch):
    outside = tmp_path / "outside.html"
    outside.write_bytes(b"outside private content")
    assert outside.read_bytes() == b"outside private content"
    with db.docs_conn() as c:
        doc_id = c.execute(
            "INSERT INTO docs (slug, title, latest_version) "
            "VALUES (%s,%s,%s) RETURNING id", ("unsafe/read", "Unsafe", 1),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO doc_versions "
            "(doc_id, version, posted_by, file_path, byte_size, sha256) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (doc_id, 1, "analyst", str(outside), outside.stat().st_size, "0" * 64),
        )
        c.commit()
        assert c.execute("SELECT file_path FROM doc_versions WHERE doc_id=%s",
                         (doc_id,)).fetchall() == [(str(outside),)]

    def fail_read(_path):
        pytest.fail("outside path reached the blob reader")

    monkeypatch.setattr(storage, "read_blob", fail_read)
    with pytest.raises(ValueError):
        docs_repo.get_version("unsafe/read", 1)

    assert outside.read_bytes() == b"outside private content"


@pytest.mark.parametrize("operation", ["delete", "purge", "expired-publish", "cleanup"])
@pytest.mark.parametrize("linebreak", ["\n", "\r\n", "\r"])
def test_removal_error_log_quotes_paths_and_escapes_newlines(
        tmp_path, monkeypatch, caplog, operation, linebreak):
    root = tmp_path / f"store{linebreak}forged path"
    monkeypatch.setenv("STORE_ROOT", str(root))
    slug = "safe/log"
    error = OSError(errno.EACCES, f"Permission denied{linebreak}forged error", "/store/x\ny")
    docs_repo.publish(slug, "Log", [], None, "analyst", b"old")
    assert docs_repo.get_latest(slug)["html"] == b"old"
    if operation in ("purge", "expired-publish"):
        with db.docs_conn() as c:
            c.execute("UPDATE docs SET expires_at=now() - interval '1 second' "
                      "WHERE slug=%s", (slug,))
            c.commit()

    def fail_remove(_slug):
        raise error

    monkeypatch.setattr(storage, "delete_doc", fail_remove)
    with caplog.at_level(logging.ERROR, logger="backend.docs_repo"):
        if operation == "delete":
            assert docs_repo.delete_docs([slug]) == (0, [slug])
        elif operation == "purge":
            assert docs_repo.purge_expired() == 0
        elif operation == "expired-publish":
            with pytest.raises(OSError) as exc:
                docs_repo.publish(slug, "New", [], None, "analyst", b"new")
            assert exc.value is error
        else:
            original = RuntimeError("write failed")

            def fail_write(*_args, **_kwargs):
                partial = root / slug / "v2.html"
                partial.write_bytes(b"partial")
                assert partial.read_bytes() == b"partial"
                raise original

            monkeypatch.setattr(storage, "store_blob", fail_write)
            monkeypatch.setattr(os, "unlink", fail_remove)
            with pytest.raises(RuntimeError) as exc:
                docs_repo.publish(slug, "New", [], None, "analyst", b"new")
            assert exc.value is original

    records = [r for r in caplog.records if r.name == "backend.docs_repo"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    message = records[0].getMessage()
    path = root / slug / "v2.html" if operation == "cleanup" else root / slug
    assert repr(str(path).replace("\r", "").replace("\n", "")) in message
    assert repr(str(error).replace("\r", "").replace("\n", "")) in message
    assert "\r" not in records[0].args[-2]
    assert "\n" not in records[0].args[-2]
    assert "\r" not in records[0].args[-1]
    assert "\n" not in records[0].args[-1]
    if operation != "cleanup":
        assert repr(slug) in message
    assert "\n" not in message
    assert "\r" not in message


def test_failed_publish_cleanup_refuses_retargeted_path(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "store-outside"
    outside.mkdir()
    sentinel = outside / "v1.html"
    sentinel.write_bytes(b"outside version")
    assert sentinel.read_bytes() == b"outside version"
    monkeypatch.setenv("STORE_ROOT", str(root))

    def fail_write(*_args, **_kwargs):
        (root / "link").symlink_to(outside, target_is_directory=True)
        raise RuntimeError("write failed after path changed")

    monkeypatch.setattr(storage, "store_blob", fail_write)
    with pytest.raises(ValueError):
        docs_repo.publish("link", "Unsafe", [], None, "analyst", b"new")

    assert sentinel.read_bytes() == b"outside version"


@pytest.mark.parametrize("leftover", ["symlink", "directory"])
def test_failed_publish_cleanup_skips_non_regular_entry(tmp_path, monkeypatch, caplog, leftover):
    root = tmp_path / "store\r\nforged path"
    root.mkdir()
    target = root / "other.html"
    target.write_bytes(b"keep target")
    assert target.read_bytes() == b"keep target"
    monkeypatch.setenv("STORE_ROOT", str(root))
    entry = root / "cleanup" / "v1.html"
    error = RuntimeError("injected write failure")

    def fail_write(*_args, **_kwargs):
        entry.parent.mkdir()
        if leftover == "symlink":
            entry.symlink_to(target)
            assert entry.is_symlink()
        else:
            entry.mkdir()
            assert entry.is_dir()
        raise error

    monkeypatch.setattr(storage, "store_blob", fail_write)
    with caplog.at_level(logging.ERROR, logger="backend.docs_repo"):
        with pytest.raises(RuntimeError) as exc:
            docs_repo.publish("cleanup", "Cleanup", [], None, "analyst", b"new")
    assert exc.value is error
    assert target.read_bytes() == b"keep target"
    assert entry.is_symlink() if leftover == "symlink" else entry.is_dir()
    records = [r for r in caplog.records if r.name == "backend.docs_repo"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    message = records[0].getMessage()
    kind = stat.S_IFLNK if leftover == "symlink" else stat.S_IFDIR
    assert records[0].args[-1] == kind
    assert repr("cleanup") in message
    assert repr(str(entry).replace("\r", "").replace("\n", "")) in message
    assert f"file kind {kind!r}" in message
    assert "\r" not in message
    assert "\n" not in message


def test_failed_publish_cleanup_propagates_lstat_error(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_ROOT", str(tmp_path))
    entry = tmp_path / "cleanup-stat" / "v1.html"
    entry.parent.mkdir()
    entry.write_bytes(b"partial")
    assert entry.read_bytes() == b"partial"
    error = OSError(errno.EACCES, "lstat denied", str(entry))
    real_lstat = os.lstat

    def fail_lstat(path, *args, **kwargs):
        if path == str(entry):
            raise error
        return real_lstat(path, *args, **kwargs)

    def fail_write(*_args, **_kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(os, "lstat", fail_lstat)
    monkeypatch.setattr(storage, "store_blob", fail_write)
    with pytest.raises(OSError) as exc:
        docs_repo.publish("cleanup-stat", "Cleanup", [], None, "analyst", b"new")
    assert exc.value is error
    assert entry.read_bytes() == b"partial"


def test_delete_refuses_regular_file_resolving_outside_store(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setenv("STORE_ROOT", str(root))
    storage.store_blob("guard", 1, b"own version")
    entry = root / "guard" / "v1.html"
    outside = tmp_path / "outside.html"
    outside.write_bytes(b"outside version")
    assert entry.read_bytes() == b"own version"
    assert outside.read_bytes() == b"outside version"
    realpath = os.path.realpath

    def escape_version(path, *args, **kwargs):
        if path == str(entry):
            return str(outside)
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", escape_version)
    with pytest.raises(OSError) as exc:
        storage.delete_doc("guard")
    assert exc.value.errno == errno.EPERM
    assert entry.read_bytes() == b"own version"
    assert outside.read_bytes() == b"outside version"


@pytest.mark.parametrize("operation", ["delete", "purge", "expired-publish"])
@pytest.mark.parametrize("symlink", ["directory", "version"])
def test_removal_anomaly_keeps_row_logs_error_and_returns_500(
        tmp_path, monkeypatch, caplog, operation, symlink):
    root = tmp_path / "store"
    monkeypatch.setenv("STORE_ROOT", str(root))
    slug = "anomaly/doc"
    docs_repo.publish(slug, "Original", [], None, "analyst", b"original")
    assert docs_repo.get_latest(slug)["html"] == b"original"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "v1.html"
    target.write_bytes(b"unrelated target")
    assert target.read_bytes() == b"unrelated target"
    directory = root / slug
    if symlink == "directory":
        directory.rename(tmp_path / "saved-doc")
        directory.symlink_to(outside, target_is_directory=True)
    else:
        (directory / "v1.html").rename(tmp_path / "saved-v1.html")
        (directory / "v1.html").symlink_to(target)
    with db.docs_conn() as c:
        if operation != "delete":
            c.execute("UPDATE docs SET expires_at=now() - interval '1 second' "
                      "WHERE slug=%s", (slug,))
            c.commit()
        before = c.execute("SELECT id, latest_version, expires_at FROM docs WHERE slug=%s",
                           (slug,)).fetchone()
    assert before is not None
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"x-docs-key": "test-api-key"}
    with caplog.at_level(logging.ERROR, logger="backend.docs_repo"):
        if operation == "delete":
            preview = client.post("/api/delete", json={"slug": slug}, headers=headers)
            assert preview.status_code == 200
            response = client.post(
                "/api/delete", headers=headers,
                json={"slug": slug, "confirm_token": preview.json()["confirm_token"]})
            assert response.status_code == 500
            assert response.json() == {
                "ok": False, "error": "failed to remove stored files",
                "deleted": 0, "failed": [slug],
            }
        elif operation == "purge":
            assert docs_repo.purge_expired() == 0
        else:
            response = client.post(
                "/api/publish", headers=headers,
                data={"slug": slug, "title": "New", "from": "analyst"},
                files={"file": ("doc.html", b"new version", "text/html")})
            assert response.status_code == 500
    with db.docs_conn() as c:
        assert c.execute("SELECT id, latest_version, expires_at FROM docs WHERE slug=%s",
                         (slug,)).fetchone() == before
    assert target.read_bytes() == b"unrelated target"
    records = [r for r in caplog.records if r.name == "backend.docs_repo"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert repr(slug) in records[0].getMessage()
    assert str(directory) in records[0].getMessage()
    assert "[Errno 1]" in records[0].getMessage()
