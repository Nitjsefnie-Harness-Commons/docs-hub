# docs-hub

[![tests](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/tests.yml/badge.svg)](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/tests.yml)
[![lint](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/lint.yml/badge.svg)](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/lint.yml)
[![types](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/types.yml/badge.svg)](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/types.yml)
[![eslint](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/eslint.yml/badge.svg)](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/eslint.yml)
[![audit](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/audit.yml/badge.svg)](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/audit.yml)
[![codeql](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/codeql.yml/badge.svg)](https://github.com/Nitjsefnie-Harness-Commons/docs-hub/actions/workflows/codeql.yml)

A password-protected, versioned HTML-artifact hub. Agents publish HTML
documents (analyses, plans, reports, reviews) over an API; humans read
them in a browser. Re-publishing the same slug keeps prior versions
browsable.

This repo holds **both halves**: the server in `backend/` and the
agent-facing client in `docs_hub_cli/`, which ships as the `docs-hub-cli`
wheel. Keeping them together is what lets one test run prove an endpoint
change and its client still agree.

## What it does

- Agents `POST /api/publish` an HTML file under a slug, with title,
  tags, project, and an author handle. Each publish becomes a new
  version; old versions remain accessible.
- Humans log in with a user ID and password, then browse, search by
  project / agent / tag, and read documents in an iframe.
- `GET /api/tags` returns every tag in use with its count (most-used
  first), so agents pick from the established tag set instead of
  inventing parallel ones.
- Filter-based bulk delete is a two-step API: a preview call returns a
  confirm token; a follow-up call with the token executes the delete.
- A publish may carry a time-to-live (`ttl`, e.g. `30m`, `2h`, `7d`).
  When it elapses the document and all its versions vanish: reads and
  listings hide it immediately, and a background reaper deletes it. Each
  publish restates the lifetime; republishing without `ttl` makes the
  document permanent.
- Documents may be Markdown (`.md`): the source is stored verbatim,
  rendered to a readable page for browsers, and returned raw
  (`text/markdown`) to agents.

## CLI

`docs-hub` is a stdlib-only client — no third-party dependencies, so it
installs anywhere Python 3.11+ runs.

| Command | Does |
|---|---|
| `docs-hub publish FILE --slug S --title T --from AGENT [--tags a,b] [--project P] [--ttl DURATION] [--format html\|markdown]` | publish a file as a new version of slug `S` |
| `docs-hub get SLUG [--version N] [-o FILE] [--text-only]` | read a document; `--text-only` strips HTML to text with `#`–`######` heading prefixes and `**bold**` markers for `<strong>` and `<b>`. A Markdown document passes through unstripped only without `--version` — a `--version N` fetch takes the browser route, which returns the rendered page. |
| `docs-hub list [--project P] [--agent A] [--untagged]` | list documents, newest version per slug |
| `docs-hub versions SLUG` | every version of one slug, newest first |
| `docs-hub tags [--project P]` | every tag in use with its count, most-used first |

Two environment values, each falling back to the `env` block of
`~/.agent-bundle/settings.json`: `DOCS_HUB_URL` (the hub to talk to) and
`DOCS_HUB_API_KEY` (sent as `x-docs-key`). Both are read at import, so
export them before invoking.

Install from a release — verify the checksum before installing, which is
the whole reason `SHA256SUMS` is published beside the wheel:

```bash
gh release download --repo Nitjsefnie-Harness-Commons/docs-hub \
  --pattern '*.whl' --pattern 'SHA256SUMS'
sha256sum -c SHA256SUMS
pip install ./docs_hub_cli-*.whl
```

## Architecture

- Python + FastAPI + uvicorn; psycopg3 against Postgres.
- Metadata and the version index live in a Postgres database; HTML
  blobs are stored on local disk, one file per version.
- Human auth verifies user ID + password against a shared `users`
  table, then issues an HMAC-signed session cookie.
- Agent auth is a shared API key presented in an `x-docs-key` header.

## Layout

| Path | Role |
|------|------|
| `backend/` | FastAPI app — `db`, `schema`, `storage`, `auth`, `session`, `docs_repo`, `login`, `api`, `views`, `app` |
| `docs_hub_cli/` | client CLI — the `docs-hub-cli` wheel |
| `public/` | SPA (`index.html`, `app.js`, `styles.css`) served at `/` |
| `store/` | HTML blobs on disk, one file per version (git-ignored runtime data) |
| `tests/` | pytest suite |
| `deploy/` | systemd unit + nginx vhost template |
| `docs/superpowers/` | design spec and implementation plan |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -v
```

Tests build an isolated database from `backend/schema.sql` and a temp
blob store — see `tests/conftest.py`. No live network is touched.

## Deployment

- `deploy/` contains a systemd unit running `uvicorn backend.app:app`
  on a local port, plus an nginx vhost template to put in front of it.

Required environment for the running service:

- `DOCS_HUB_DATABASE_URL_DOCS` — Postgres connection string for the docs database.
- `DOCS_HUB_DATABASE_URL_AUTH` — Postgres connection string for the shared auth
  database (the `users` table used for human login).
- `DOCS_HUB_API_KEY` — shared key agents present in the `x-docs-key`
  header.
- `DOCS_HUB_SESSION_SECRET` — HMAC secret used to sign session cookies.
- `DOCS_HUB_STORE_ROOT` — path to the blob store directory.
- `DOCS_HUB_COOKIE_SECURE` — `1` (default) to mark the session cookie `Secure`;
  set to `0` for local non-HTTPS development.

Optional environment:

- `DOCS_HUB_PURGE_INTERVAL_SECONDS` — how often the reaper deletes expired
  documents (default `60`).
- `DOCS_HUB_PRIVACY_NOTICE_FILE` — path to a Markdown file with the
  operator's privacy notice. When set and readable, `/privacy` serves it
  rendered as Markdown and a link to it appears on the sign-in form and on
  anonymously served public documents; unset, empty or unreadable means
  the link and the page are absent.

The old unprefixed names keep working as a fallback for the settings read
through `backend/settings.py` (the prefixed name wins when both are set).
`DOCS_HUB_API_KEY` and `DOCS_HUB_PRIVACY_NOTICE_FILE` are read
prefixed-only and have no unprefixed alias. Empty values count as unset
everywhere.

After pulling a version that changes `requirements.txt`, run
`.venv/bin/pip install -r requirements.txt` before restarting the unit.

### Backup and restore

One deployment's documents live in two places a backup must copy together:
the docs database named by `DOCS_HUB_DATABASE_URL_DOCS` (the `docs` and
`doc_versions` tables of `backend/schema.sql`), and the blob directory named
by `DOCS_HUB_STORE_ROOT` (one file per version — `backend/storage.py`). The
shared auth database (`DOCS_HUB_DATABASE_URL_AUTH`, the `users` table) is
out of scope: docs-hub reads it for login but does not own it, so it is
backed up under whatever procedure its owner provides.

This repository provides the procedure below; the operator supplies the
backup schedule, retention, off-host storage, and who runs them.

#### Backup

Stop the unit (`deploy/docs-hub.service`) first: publishes write a version's
file before its row commits, and deletes and the expiry reaper unlink files
before their commit, so with the unit running no ordering of dump and copy
can guarantee the two agree.

```bash
systemctl stop docs-hub.service
pg_dump -Fc "$DOCS_HUB_DATABASE_URL_DOCS" > docs-hub-<date>.dump
cp -a "$DOCS_HUB_STORE_ROOT" /backup/docs-hub-store-<date>
```

With the unit stopped, nothing can write either side, so the dump and the
blob copy agree with each other.

#### Restore

Restore the dump into a fresh database — the name the unit's
`DOCS_HUB_DATABASE_URL_DOCS` already points at, or point that variable at
the new name — and copy the blobs into `DOCS_HUB_STORE_ROOT`:

```bash
createdb <db>
pg_restore -d <db> docs-hub-<date>.dump
mkdir -p "$DOCS_HUB_STORE_ROOT"
cp -a /backup/docs-hub-store-<date>/. "$DOCS_HUB_STORE_ROOT"/
```

Each stored `file_path` is the realpath of
`<DOCS_HUB_STORE_ROOT>/<slug>/v<N>.html|.md` taken at publish time
(`backend/storage.py`), so when the new `DOCS_HUB_STORE_ROOT` differs from
the original, rewrite the prefix — reads refuse any stored path outside the
current store, so an unrewritten restore fails loudly rather than silently
missing; the one quiet case is a new root that contains the old one, where
unrewritten rows keep reading from the old subtree until it is removed.
Spell both roots as realpaths with a
trailing slash, and rewrite the anchored prefix — a bare `replace()` also
rewrites a second occurrence of the old spelling inside a slug segment:

```sql
UPDATE doc_versions
SET file_path = '<new root>/' || substr(file_path, length('<old root>/') + 1)
WHERE starts_with(file_path, '<old root>/');
```

The rewrite covers only rows carrying the old prefix; this query must
return zero rows afterwards, and every row it returns needs a hand fix
before the unit starts:

```sql
SELECT file_path FROM doc_versions
WHERE NOT starts_with(file_path, '<new root>/');
```

#### Verify the restore

Run this check before starting the unit — the reaper purges once at
startup, deleting documents whose expiry has passed, so the check is the
last look at the restored state as restored. It stops on any row whose file
is missing, or whose size or SHA-256 disagrees with the stored `byte_size` /
`sha256`. Slugs are `[a-z0-9_-/]`, so the `|` separator cannot occur in the
store-relative part of a stored path; a `|` in the store root itself, which
no charset constrains, would only misdirect the split into a missing-file or
mismatch exit — still loud, never a silent pass:

```bash
rows=$(psql "$DOCS_HUB_DATABASE_URL_DOCS" -At -F '|' \
    -c "SELECT file_path, byte_size, sha256 FROM doc_versions") || exit
n=0
while IFS='|' read -r path size want; do
    if [ -z "$path" ]; then continue; fi
    if [ ! -f "$path" ]; then
        printf 'missing file: %s\n' "$path" >&2
        exit 1
    fi
    got=$(stat -c %s -- "$path")
    hash=$(sha256sum -- "$path")
    hash=${hash%% *}
    if [ "$got" -ne "$size" ] || [ "$hash" != "$want" ]; then
        printf 'mismatch: %s\n' "$path" >&2
        exit 1
    fi
    n=$((n + 1))
done <<<"$rows"
echo "checked $n version file(s), all match"
```

Then start the unit:

```bash
systemctl start docs-hub.service
```

## Conventions

- TDD: write the failing test first, then the minimal implementation.
- Small, focused files — one clear responsibility each.
