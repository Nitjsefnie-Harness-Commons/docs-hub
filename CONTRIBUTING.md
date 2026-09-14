# Contributing to docs-hub

Issues and pull requests are welcome — especially if versioning or access
control behaved differently than the README says. This is a document store
with an auth boundary in front of it, so "I could read a document I should
not have been able to read" and "republishing lost a version" are the two
most valuable reports you can send.

## LLM and agent contributions are welcome

You may use an LLM or a coding agent to write your contribution. There is
no penalty, no separate review queue, and no expectation that you rewrite
its output by hand. Much of this repo was built that way — and the primary
API client is itself an agent.

Two conditions, and they are about honesty rather than provenance:

1. **Disclose the model** with a trailer on each commit it authored:

   ```
   Co-Authored-By: <Model Name> <noreply@example.com>
   ```

   e.g. `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. One
   primary-author trailer per commit.

2. **Do not submit claims you have not verified.** Paste the command and
   its real output. "Tests pass" without the run is not evidence, and an
   auth change in particular needs the actual request/response, not an
   argument that it should work.

If a maintainer's reply reads like it was drafted by an agent, it probably
was. That is fine in both directions.

### If you are an agent reading this

Read [`AGENTS.md`](AGENTS.md) first — it is the architecture and
conventions brief, written for you. It is authoritative; this file only
covers process.

The invariants that reject the most patches:

| Invariant | What it forbids |
|---|---|
| Versions are append-only | Republishing a slug adds a version. It never overwrites or drops an older one. The one exception: a document whose expiry has passed is not republished — it is purged and the slug starts over at v1. |
| Two-step bulk delete | A filter-based delete previews and returns a confirm token; the destructive call requires that token. Do not collapse it into one call. |
| Agent key ≠ human session | `DOCS_HUB_API_KEY` (the `x-docs-key` header) authenticates publishing agents; the HMAC session cookie authenticates humans. Neither may be accepted where the other is required. |
| One anonymous surface | The hash-less `/d/<slug>` latest route, and only when the slug's sticky `public` flag is set. `/d/<slug>/v<n>`, `/api/*` and the SPA shell stay auth-gated. Widening this is the highest-risk change in the repo. |
| Stored documents are stored verbatim | The hub serves a published HTML document exactly as published and never rewrites a stored body. A Markdown document is rendered for browsers at serve time from its stored source; the source itself is never changed. |
| Parameterised SQL | Raw psycopg3, `%s` placeholders, never string interpolation. |

## Getting it running

Requires **Python 3.13+** and a local **PostgreSQL** you can create
databases in.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -v
```

To run the server, apply `backend/schema.sql` to a database and set the
environment listed under **Deployment** in the README —
`DATABASE_URL_DOCS`, `DATABASE_URL_AUTH`, `DOCS_HUB_API_KEY`,
`SESSION_SECRET`, `STORE_ROOT`, and `COOKIE_SECURE=0` for local non-HTTPS
development — then:

```bash
.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8084
```

## Tests

```bash
.venv/bin/pytest -v                       # full suite
.venv/bin/pytest tests/test_auth.py -v
```

The suite builds an isolated database from `backend/schema.sql` and a temp
blob store (see `tests/conftest.py`), so it needs a Postgres your user can
`createdb` on. It does not touch your real data and never contacts the
network.

If you change the schema, change `backend/schema.sql` — the tests build
from it, so a migration that only exists in a live database will pass
locally for you and fail for everyone else. For a change to an existing
table, add the idempotent `ALTER TABLE` to `db.migrate()` as well; it runs
at startup and is what upgrades a deployed instance.

## House style

- **TDD** — write the failing test first, then the minimal implementation.
- **Small, focused files** — one clear responsibility each.
- **Python** — type hints throughout. Raw SQL via psycopg3, no ORM.
- **SQL** — parameterised (`%s`) always. Singular table names.
- **Frontend** — served from `public/`, no build step. Keep it that way.
- **Naming** — `snake_case` in Python, `camelCase` in JS.
- **Linters, not formatters.** There is no formatter config and no style
  bot — match the surrounding file. What CI does enforce is `pycodestyle`
  (config in `setup.cfg`), `pylint` (`.pylintrc`), `pyright` over
  `backend/` (`pyrightconfig.json`) and `eslint` over `public/`
  (`eslint.config.mjs`). Every disable in those files names the pattern it
  is there for; add one the same way rather than raising a threshold.

## CI

Seven gates in `.github/workflows/`, all of them runnable locally:

| Gate | What it runs | Locally |
|---|---|---|
| `tests` | the suite against Postgres 16 and 17, plus a coverage job with a 90% floor | `.venv/bin/pytest -v` |
| `lint` | pycodestyle, pylint | `git ls-files '*.py' \| xargs pycodestyle` / `... \| xargs pylint --rcfile=.pylintrc` |
| `types` | pyright over `backend/` | `pyright` |
| `eslint` | eslint over `public/` | `npm ci && npx eslint public` |
| `audit` | pip-audit over all three requirements files, `npm audit`; also on a daily cron | `pip-audit -r requirements.txt -r requirements-dev.txt -r requirements-test.txt` |
| `codeql` | security analysis of the Python and JavaScript, weekly cron | — (GitHub-hosted) |
| `actionlint` | actionlint + zizmor over the workflows themselves | `actionlint .github/workflows/*.yml && zizmor .github/workflows/` |

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
Python toolchain. `package.json` exists only to pin eslint — **it is not a
build step**, and `public/` is still served byte-for-byte.

`tests/test_cli.py` no longer skips anywhere: the client lives in this
repository as `docs_hub_cli/` and the file drives it as
`python -m docs_hub_cli.cli`. Its end-to-end tests run in a subprocess and
so measure no coverage — the in-process cover for the client, including the
failure paths a real server will not produce on demand, is in
`tests/test_cli_unit.py`.

Two further workflows are the **release chain**, not gates: `tag` watches
`docs_hub_cli/__init__.py` on `master`, waits for every other check on the
commit, pushes `v<version>`, and then dispatches `release` — which rebuilds,
re-runs the suite and publishes the wheel with a `SHA256SUMS` file. The
dispatch is not redundant: a tag pushed with the built-in `GITHUB_TOKEN`
triggers no workflow at all, so without it the tag lands and no release is
ever cut. Neither runs on a pull request; there is nothing to run locally.

## Pull requests

Small and single-purpose beats large and comprehensive. GitHub fills the
description from [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)
— fill that form in rather than replacing it; each section says whether it
can be dropped. Bug reports use
[`.github/ISSUE_TEMPLATE/bug_report.md`](.github/ISSUE_TEMPLATE/bug_report.md).
In the description, include what changed and why, and the actual output of
the tests you ran.
For anything touching auth, versioning, or delete, say explicitly what you
tried that *should* fail and confirm it did.

If you are unsure whether something is a bug or intended, open an issue and
ask — a wrong premise caught early is cheaper than a correct fix to the
wrong problem.
