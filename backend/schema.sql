CREATE TABLE IF NOT EXISTS docs (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    project         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    latest_version  INT NOT NULL DEFAULT 0,
    public          BOOLEAN NOT NULL DEFAULT false,
    expires_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS doc_versions (
    id          SERIAL PRIMARY KEY,
    doc_id      INT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    version     INT NOT NULL,
    posted_by   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    file_path   TEXT NOT NULL,
    byte_size   INT NOT NULL,
    sha256      TEXT NOT NULL,
    UNIQUE (doc_id, version)
);

CREATE INDEX IF NOT EXISTS idx_docs_project ON docs(project);
CREATE INDEX IF NOT EXISTS idx_docs_expires_at ON docs(expires_at)
    WHERE expires_at IS NOT NULL;
