"""Document + version operations against the docs DB and the blob store."""
from __future__ import annotations

from typing import LiteralString

from backend import db, storage

# A doc is live until its expiry passes; NULL means permanent. Every query
# that returns or mutates a doc applies this so an expired doc is gone the
# moment its time passes, whether or not the reaper has run yet.
_LIVE: LiteralString = "(d.expires_at IS NULL OR d.expires_at > now())"


def publish(slug: str, title: str, tags: list[str], project: str | None,
            posted_by: str, html: bytes,
            ttl_seconds: int | None = None) -> dict:
    """Create a new version of `slug` (creating the doc on first publish).

    `ttl_seconds` states the doc's lifetime from now; None makes it
    permanent. Each publish restates it — the expiry is not sticky. A slug
    whose previous life has expired is purged first and starts over at v1.
    Returns {slug, version, doc_id, expires_at}."""
    if not storage.is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    with db.docs_conn() as c:
        stale = c.execute(
            "DELETE FROM docs WHERE slug=%s AND expires_at IS NOT NULL "
            "AND expires_at <= now() RETURNING id", (slug,)).fetchone()
        if stale is not None:
            storage.delete_doc(slug)
        row = c.execute("SELECT id, latest_version FROM docs WHERE slug=%s",
                        (slug,)).fetchone()
        if row is None:
            inserted = c.execute(
                "INSERT INTO docs (slug, title, tags, project, latest_version) "
                "VALUES (%s,%s,%s,%s,0) RETURNING id",
                (slug, title, tags, project),
            ).fetchone()
            if inserted is None:
                raise RuntimeError("INSERT ... RETURNING id yielded no row")
            doc_id = inserted[0]
            version = 1
        else:
            doc_id, latest = row
            version = latest + 1
        path, size, digest = storage.store_blob(slug, version, html)
        c.execute(
            "INSERT INTO doc_versions "
            "(doc_id, version, posted_by, file_path, byte_size, sha256) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (doc_id, version, posted_by, path, size, digest),
        )
        updated = c.execute(
            "UPDATE docs SET latest_version=%s, title=%s, tags=%s, project=%s, "
            "updated_at=now(), expires_at=now() + make_interval(secs => %s) "
            "WHERE id=%s RETURNING expires_at",
            (version, title, tags, project, ttl_seconds, doc_id),
        ).fetchone()
        c.commit()
    expires_at = updated[0] if updated is not None else None
    return {"slug": slug, "version": version, "doc_id": doc_id,
            "expires_at": expires_at}


def _version_row(slug: str, version: int) -> dict | None:
    with db.docs_conn() as c:
        row = c.execute(
            "SELECT v.version, v.posted_by, v.created_at, v.file_path, "
            "v.byte_size, v.sha256, d.title "
            "FROM doc_versions v JOIN docs d ON d.id=v.doc_id "
            f"WHERE d.slug=%s AND v.version=%s AND {_LIVE}",
            (slug, version),
        ).fetchone()
    if row is None:
        return None
    return {
        "version": row[0], "posted_by": row[1], "created_at": row[2],
        "file_path": row[3], "byte_size": row[4], "sha256": row[5],
        "title": row[6],
    }


def get_version(slug: str, version: int) -> dict | None:
    meta = _version_row(slug, version)
    if meta is None:
        return None
    meta["html"] = storage.read_blob(meta["file_path"])
    return meta


def get_latest(slug: str) -> dict | None:
    with db.docs_conn() as c:
        row = c.execute(
            f"SELECT latest_version FROM docs d WHERE d.slug=%s AND {_LIVE}",
            (slug,)).fetchone()
    if row is None:
        return None
    return get_version(slug, row[0])


def is_public(slug: str) -> bool:
    """True iff the doc exists and is flagged public (anon-readable via /d/)."""
    with db.docs_conn() as c:
        row = c.execute(
            f"SELECT public FROM docs d WHERE d.slug=%s AND {_LIVE}",
            (slug,)).fetchone()
    return bool(row and row[0])


def set_public(slug: str, public: bool) -> bool:
    """Set the slug's public flag. Returns False if the slug is unknown.
    The flag lives on the docs row, so re-publishing a version never
    touches it."""
    with db.docs_conn() as c:
        row = c.execute(
            f"UPDATE docs d SET public=%s WHERE d.slug=%s AND {_LIVE} "
            "RETURNING id", (public, slug)).fetchone()
        c.commit()
    return row is not None


def list_docs(project: str | None = None, agent: str | None = None) -> list[dict]:
    """Newest-updated first. `agent` filters by the poster of the latest version."""
    sql = (
        "SELECT d.slug, d.title, d.tags, d.project, d.updated_at, "
        "d.latest_version, v.posted_by, v.byte_size, d.public, d.expires_at "
        "FROM docs d JOIN doc_versions v "
        "ON v.doc_id=d.id AND v.version=d.latest_version"
    )
    clauses: list[LiteralString] = [_LIVE]
    params: list = []
    if project is not None:
        clauses.append("d.project=%s")
        params.append(project)
    if agent is not None:
        clauses.append("v.posted_by=%s")
        params.append(agent)
    sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY d.updated_at DESC"
    with db.docs_conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [
        {"slug": r[0], "title": r[1], "tags": r[2], "project": r[3],
         "updated_at": r[4], "latest_version": r[5], "posted_by": r[6],
         "byte_size": r[7], "public": r[8], "expires_at": r[9]}
        for r in rows
    ]


def find_docs(filters: dict) -> list[dict]:
    """Docs matching the AND-combined filter set. Returns
    [{slug, title, latest_version}], newest-updated first.

    Recognised filter keys (all optional): slug, project, author, tag, q,
    updated_before, updated_after.
    """
    sql = (
        "SELECT d.slug, d.title, d.latest_version "
        "FROM docs d JOIN doc_versions v "
        "ON v.doc_id=d.id AND v.version=d.latest_version"
    )
    clauses: list[LiteralString] = [_LIVE]
    params: list = []
    if filters.get("slug"):
        clauses.append("d.slug=%s")
        params.append(filters["slug"])
    if filters.get("project"):
        clauses.append("d.project=%s")
        params.append(filters["project"])
    if filters.get("author"):
        clauses.append("v.posted_by=%s")
        params.append(filters["author"])
    if filters.get("tag"):
        clauses.append("%s = ANY(d.tags)")
        params.append(filters["tag"])
    if filters.get("q"):
        clauses.append("(d.title ILIKE %s OR d.slug ILIKE %s)")
        like = f"%{filters['q']}%"
        params += [like, like]
    if filters.get("updated_before"):
        clauses.append("d.updated_at < %s")
        params.append(filters["updated_before"])
    if filters.get("updated_after"):
        clauses.append("d.updated_at > %s")
        params.append(filters["updated_after"])
    sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY d.updated_at DESC"
    with db.docs_conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [{"slug": r[0], "title": r[1], "latest_version": r[2]} for r in rows]


def delete_docs(slugs: list[str]) -> int:
    """Delete the named docs (versions cascade via the doc_versions FK) and
    their blob directories. Returns the count of doc rows actually deleted.
    The DB delete commits before blob removal, so a crash leaves orphan
    blobs (harmless) rather than orphan rows."""
    deleted = 0
    for slug in slugs:
        with db.docs_conn() as c:
            row = c.execute("DELETE FROM docs WHERE slug=%s RETURNING id",
                            (slug,)).fetchone()
            c.commit()
        if row is not None:
            deleted += 1
            storage.delete_doc(slug)
    return deleted


def purge_expired() -> int:
    """Delete every doc whose expiry has passed (versions cascade) and its
    blob directory. Returns the number of docs removed. Same crash ordering
    as delete_docs: rows go first, so a crash leaves orphan blobs, never
    orphan rows that a later publish would trip over."""
    with db.docs_conn() as c:
        rows = c.execute(
            "DELETE FROM docs WHERE expires_at IS NOT NULL "
            "AND expires_at <= now() RETURNING slug").fetchall()
        c.commit()
    for (slug,) in rows:
        storage.delete_doc(slug)
    return len(rows)


def list_versions(slug: str) -> list[dict]:
    """Version history for a slug, newest first."""
    with db.docs_conn() as c:
        rows = c.execute(
            "SELECT v.version, v.posted_by, v.created_at, v.byte_size, v.sha256 "
            "FROM doc_versions v JOIN docs d ON d.id=v.doc_id "
            f"WHERE d.slug=%s AND {_LIVE} ORDER BY v.version DESC",
            (slug,),
        ).fetchall()
    return [
        {"version": r[0], "posted_by": r[1], "created_at": r[2],
         "byte_size": r[3], "sha256": r[4]}
        for r in rows
    ]


def list_tags(project: str | None = None) -> list[dict]:
    """Every distinct tag in use with its usage count, most-used first.
    Lets agents pick from already-established tags rather than inventing
    new ones. `project` scopes to one project's docs."""
    sql = "SELECT unnest(d.tags) AS tag, COUNT(*) AS n FROM docs d"
    clauses: list[LiteralString] = [_LIVE]
    params: list = []
    if project is not None:
        clauses.append("d.project=%s")
        params.append(project)
    sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY tag ORDER BY n DESC, tag ASC"
    with db.docs_conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [{"tag": r[0], "count": r[1]} for r in rows]
