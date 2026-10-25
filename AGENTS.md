# docs-hub

`docs.nitjsefni.eu` — a password-protected, versioned HTML-artifact hub.
Fleet agents publish HTML documents (analyses, plans, reports, reviews);
a human reads them in a browser. Re-publishing a slug keeps prior
versions browsable.

## Status

Built and deployed — the server is live at `https://docs.nitjsefni.eu`.

- Spec: `docs/superpowers/specs/2026-05-21-docs-hub-design.md`
- Plan: `docs/superpowers/plans/2026-05-21-docs-hub.md` — 13 TDD tasks

Build the project by executing the plan task-by-task (superpowers
`subagent-driven-development` or `executing-plans`).

## Layout

| Path | Role |
|------|------|
| `backend/` | FastAPI app — db, schema, storage, auth, session, docs_repo, login, api, views, app |
| `docs_hub_cli/` | the agent-facing client CLI, shipped as the `docs-hub-cli` wheel |
| `store/` | HTML blobs on disk, one file per version (git-ignored runtime data) |
| `tests/` | pytest suite, plus `app_js.test.mjs` for `public/app.js` |
| `deploy/` | systemd unit + nginx vhost |
| `docs/superpowers/` | spec + plan |

The agent-facing **CLI lives here**, in `docs_hub_cli/` — client and server
are one contract, so a change to an endpoint on one side is caught by the
same test run as the other. It builds into the `docs-hub-cli` wheel
(`pyproject.toml`), which `tag.yml` and `release.yml` publish with a
`SHA256SUMS` file on every `__version__` bump. The fleet setup bundle
installs that wheel and keeps a thin wrapper at
`~/.agent-bundle/scripts/docs_hub.py`, so the documented absolute-path
invocation keeps working unchanged.

## Architecture

- Python + FastAPI + uvicorn; psycopg3 against Postgres.
- Metadata + the version index in the local `docs` database; HTML blobs
  on local disk under `store/`.
- Human auth: user ID + password verified against the shared `authdb`
  Postgres `users` table (same credentials as the other `*.nitjsefni.eu`
  services), then an HMAC session cookie.
- Agent auth: a shared API key (`DOCS_HUB_API_KEY`, from the environment).
- Public docs: a sticky per-slug `public` flag on the `docs` row (default
  false, survives re-publishes). When set, the hash-less `/d/<slug>` latest
  route serves to anyone without auth; that and the operator-configured
  `/privacy` notice route (`DOCS_HUB_PRIVACY_NOTICE_FILE`) are the two
  anonymous surfaces, and the only ones. `/d/<slug>/v<n>`, `/api/*` and
  the SPA shell stay auth-gated. Toggle via
  `POST /api/doc/<slug>/public` (auth required) or the SPA doc-view button.
  `db.migrate()` applies the idempotent `ALTER TABLE` at startup.
- Ephemeral docs: a nullable `expires_at` on the `docs` row, set per publish
  from the `ttl` form field (`backend/ttl.py` parses `30m`/`2h`/`7d`/seconds).
  NOT sticky — every publish restates it, and omitting it makes the doc
  permanent. `docs_repo` filters every read to live rows (`_LIVE`), so an
  expired doc is 404/unlisted the moment its time passes; `backend/reaper.py`
  runs `purge_expired()` at startup and every `DOCS_HUB_PURGE_INTERVAL_SECONDS`
  (default 60) to delete the rows and blobs. Republishing an expired slug
  purges it first and starts again at v1.
- Markdown docs: `doc_versions.format` (`html` | `markdown`), inferred from
  the upload filename unless the `format` field says otherwise; stored
  verbatim as `v<N>.md`; `backend/render.py` (markdown-it-py, raw HTML
  escaped) renders it on `/d/…` at serve time; `/api/doc/<slug>` returns the
  raw source as `text/markdown`.
- Served on local port 8084, behind nginx, Cloudflare-proxied.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest -v
```

Tests build an isolated `docs_test` database from `backend/schema.sql`
and a temp blob store — see `tests/conftest.py`. No live network.

One `ci gate` workflow owns the gate set's `push` and `pull_request`
triggers without a path filter, so documentation-only pushes run it too.
It folds ten gates: `tests` (Postgres 16 and 17, plus the recorded
coverage floor, plus the `public/app.js` test in jsdom),
`lint` (pycodestyle, pylint), `types` (pyright over `backend/`), `eslint`
(over `public/` and the JavaScript tests in `tests/`), `audit`
(pip-audit, npm audit), `codeql`, `size-baseline` (the tracked-file
line-count ratchet), `actionlint` (actionlint + zizmor),
`secrets` (gitleaks), and `speed` (the last release against this commit
on one runner, `scripts/ci/speed.py`). Its `aggregate` job folds
every leg and fails closed, with `conflict-markers` on every push and PR
and PR-only `base-freshness`, which fails when `master` holds a gate-input
commit the head lacks, naming the commit and file. CONTRIBUTING.md's table
gives the local commands; CodeQL runs on GitHub. Run the applicable local
checks before pushing rather than using CI as the first check.

The coverage floor is recorded, not fixed: `.github/ci-thresholds.json`
holds a measurement and a floor exactly 1.5 points below it, and the
`coverage` job fails under that floor. After a green `ci gate` run for a
push to `master`, `ratchet push` commits a raise when that run measured
above the recorded measurement and `master` has not moved since; nothing
lowers it automatically. On a pull request the
coverage of changed lines is reported in the job summary with no
threshold of its own.

Two more workflows are the release chain rather than gates: `tag` watches
`docs_hub_cli/__init__.py` on `master`, requires `aggregate` to succeed and
waits for all other reported checks to finish without failure, pushes
`v<version>` and then dispatches `release`, which rebuilds,
re-runs the suite and publishes the wheel plus `SHA256SUMS`. The dispatch is
load-bearing — a tag pushed with `GITHUB_TOKEN` triggers no workflow at all.

## Conventions

- TDD: write the failing test first, then the minimal implementation.
- Small, focused files — one clear responsibility each.
- Every commit carries a `Co-Authored-By` trailer for the model/agent
  that authored it.
