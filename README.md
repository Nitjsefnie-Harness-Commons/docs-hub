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

## CLI

`docs-hub` is a stdlib-only client — no third-party dependencies, so it
installs anywhere Python 3.11+ runs.

| Command | Does |
|---|---|
| `docs-hub publish FILE --slug S --title T --from AGENT [--tags a,b] [--project P] [--ttl DURATION]` | publish an HTML file as a new version of slug `S` |
| `docs-hub get SLUG [--version N] [-o FILE] [--text-only]` | read a document; `--text-only` strips it to plain text |
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

- `DATABASE_URL_DOCS` — Postgres connection string for the docs database.
- `DATABASE_URL_AUTH` — Postgres connection string for the shared auth
  database (the `users` table used for human login).
- `DOCS_HUB_API_KEY` — shared key agents present in the `x-docs-key`
  header.
- `SESSION_SECRET` — HMAC secret used to sign session cookies.
- `STORE_ROOT` — path to the blob store directory.
- `COOKIE_SECURE` — `1` (default) to mark the session cookie `Secure`;
  set to `0` for local non-HTTPS development.

Optional environment:

- `PURGE_INTERVAL_SECONDS` — how often the reaper deletes expired
  documents (default `60`).

After pulling a version that changes `requirements.txt`, run
`.venv/bin/pip install -r requirements.txt` before restarting the unit.

## Conventions

- TDD: write the failing test first, then the minimal implementation.
- Small, focused files — one clear responsibility each.
