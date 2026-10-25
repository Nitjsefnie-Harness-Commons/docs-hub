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
| One anonymous surface | The hash-less `/d/<slug>` latest route, and only when the slug's sticky `public` flag is set — plus `/privacy`, and only when the operator's privacy notice is configured (`DOCS_HUB_PRIVACY_NOTICE_FILE`). `/d/<slug>/v<n>`, `/api/*` and the SPA shell stay auth-gated. Widening this is the highest-risk change in the repo. |
| Stored documents are stored verbatim | The hub serves a published HTML document exactly as published and never rewrites a stored body. A Markdown document is rendered for browsers at serve time from its stored source; the source itself is never changed. The one exception: anonymously served public documents gain a privacy-notice link in the served bytes; the stored blob is untouched. |
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
`DOCS_HUB_DATABASE_URL_DOCS`, `DOCS_HUB_DATABASE_URL_AUTH`, `DOCS_HUB_API_KEY`,
`DOCS_HUB_SESSION_SECRET`, `DOCS_HUB_STORE_ROOT`, and
`DOCS_HUB_COOKIE_SECURE=0` for local non-HTTPS development — then:

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

One `ci gate` workflow owns the gate set's `push` and `pull_request`
triggers, with no path filter — documentation-only pushes run it too. It
folds eight gates: `tests`, `lint`, `types`, `eslint`, `audit`, `codeql`,
`actionlint`, and `secrets`. Their local commands are listed below; CodeQL
runs on GitHub:

| Gate | What it runs | Locally |
|---|---|---|
| `tests` | the suite against Postgres 16 and 17, plus a coverage job gated on the floor recorded in `.github/ci-thresholds.json` (below), plus `tests/app_js.test.mjs`, which loads the real `public/index.html` and `public/app.js` in jsdom and reports `app.js` line coverage with no threshold | `.venv/bin/pytest -v` / `npm ci && node --test --experimental-test-coverage --test-coverage-include=public/app.js tests/app_js.test.mjs` |
| `lint` | pycodestyle, pylint | `git ls-files '*.py' \| xargs pycodestyle` / `... \| xargs pylint --rcfile=.pylintrc` |
| `types` | pyright over `backend/` | `pyright` |
| `eslint` | eslint over `public/` | `npm ci && npx eslint public` |
| `audit` | pip-audit over each of the seven requirements manifests separately (a joint `--require-hashes` run fails), plus `npm audit`; also on a daily cron | `pip-audit -r requirements.txt` and the same again for each of the other six: `requirements-dev.txt`, `requirements-test.txt`, `requirements-diff-cover.txt`, `requirements-audit.txt`, `requirements-zizmor.txt`, `requirements-build.txt` |
| `codeql` | security analysis of the Python, JavaScript and Actions (the workflows language), weekly cron | — (GitHub-hosted) |
| `actionlint` | actionlint + zizmor over the workflows themselves | `actionlint .github/workflows/*.yml && zizmor .github/workflows/` |
| `secrets` | gitleaks over the full history and working tree; also on a daily cron | `gitleaks git --no-banner --verbose --redact=100 . && gitleaks dir --no-banner --verbose --redact=100 .` |

The `aggregate` job folds every leg and fails closed, with two local legs
of its own: `conflict-markers` on every push and PR, and `base-freshness`
on PRs, which fails when `master` holds a gate-input commit the head lacks,
naming the commit and file.

The coverage floor is a ratchet, not a fixed number.
`.github/ci-thresholds.json` records a measurement and a floor exactly 1.5
points below it; the `coverage` job reads the floor through
`python3 scripts/ci/thresholds.py --coverage-floor python` and fails under
it. After a green `ci gate` run for a push to `master`, the `ratchet push`
workflow (`.github/workflows/ratchet-push.yml`) commits a raise to
`master` when that run measured above the recorded measurement and
`master` has not moved since; nothing lowers the floor
automatically, and lowering it by hand to turn a red build green is not a
fix. On a pull request the coverage of changed lines (diff-cover against
`origin/master`) is reported in the job summary with no threshold of its
own.

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
Python toolchain. `package.json` exists only to pin eslint and jsdom (the
`app.js` test's DOM) — **it is not a build step**, and `public/` is still
served byte-for-byte.

`tests/test_cli.py` no longer skips anywhere: the client lives in this
repository as `docs_hub_cli/` and the file drives it as
`python -m docs_hub_cli.cli`. Its end-to-end tests run in a subprocess and
so measure no coverage — the in-process cover for the client, including the
failure paths a real server will not produce on demand, is in
`tests/test_cli_unit.py`.

Two further workflows are the **release chain**, not gates: `tag` watches
`docs_hub_cli/__init__.py` on `master`, requires `aggregate` to succeed and
judges every other check run produced for the commit by a `push` or
`pull_request` run before tagging, skipping checks from any other event
(issue-comment commands, scheduled crons); a check whose producing run
cannot be identified is still judged. It then pushes `v<version>` and
dispatches `release` — which rebuilds, re-runs the suite and publishes the
wheel with a `SHA256SUMS` file. The dispatch is not redundant: a tag pushed
with the built-in `GITHUB_TOKEN`
triggers no workflow at all, so without it the tag lands and no release is
ever cut. After the release ships, its PR's author moves `master` to the next
patch's `.dev0` version in a separate commit, right away. `tag.yml` skips
`.dev` versions with a message; nothing is tagged and `release.yml` is not
dispatched. Neither runs on a pull request; there is nothing to run locally.

The `claim` workflow is not a gate either: it answers `/claim`, `/unclaim`
and `/release` comments on issues and blocks nothing.

The `pr gate` workflow is not a gate leg either. On a non-draft pull
request whose author is not a bot, it checks the description against the
pull request template, the referenced issues against their assignees and
the commits' attribution; a pull request that fails is commented on and
closed unless the failing check is the reference one and its failure is
one of the comment-only shapes the pull requests section lists, and one
the gate closed is reopened once it passes. `aggregate` remains the only required check, and the workflow
always runs from `master`'s definition of it rather than the pull
request's. The `pull_request_target` trigger is load-bearing: a fork's
token on a `pull_request` run cannot close a pull request, so the base
repository's token does the closing. The template is read at the pull
request's base SHA, no pull-request code is run, and a pull request it
reopens has its CI runs held until its author pushes once more.

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

To work on an issue, claim it first: comment `/claim` on an open,
unassigned issue and the claim workflow assigns it to you; `/unclaim` or
`/release` hands it back.

Once open, a non-draft pull request is admitted by the `pr gate`
workflow: its description is checked against the pull request template,
it must reference an issue assigned to its author, and its commits must
be its author's, a push-access account's or web-flow's, or carry a
`Co-Authored-By` trailer for the author — model trailers at a noreply@
address are outside this check. A pull request that does not conform is
commented on and closed, and reopened once corrected; the reference
check is the only one with a comment-only outcome — with more than
twenty references, or with no checked reference but reference-like text,
it only comments and leaves the pull request open, and a pull request
that also fails the template check still closes. Assigning an
issue does not re-trigger the gate, so after an issue is assigned or
reassigned, edit the pull request body to have the pull request
rechecked.
