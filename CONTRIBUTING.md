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

Requires **Python 3.15+** and a local **PostgreSQL** you can create
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
  `backend/` (`pyrightconfig.json`) and `eslint` over `public/` and the
  JavaScript tests in `tests/` (`eslint.config.mjs`). Every disable in
  those files names the pattern it is there for; add one the same way
  rather than raising a threshold.

## CI

One `ci gate` workflow owns the gate set's `push` and `pull_request`
triggers, with no path filter — documentation-only pushes run it too;
only `size-baseline` also runs standalone, on pull requests and on pushes
to `main` or `master`. It folds ten gates: `tests`, `lint`, `types`, `eslint`, `audit`, `codeql`,
`size-baseline`, `actionlint`, `secrets`, and `speed`. Their local commands
are listed below; CodeQL runs on GitHub:

| Gate | What it runs | Locally |
|---|---|---|
| `tests` | the suite against Postgres 16 and 17, plus the tests marked `portable` (no database) on Windows, macOS and Linux with Python 3.14 and 3.15, plus a coverage job gated on the floor recorded in `.github/ci-thresholds.json` (below), plus `tests/app_js.test.mjs`, which loads the real `public/index.html` and `public/app.js` in jsdom and reports `app.js` line coverage with no threshold | `.venv/bin/pytest -v` / `.venv/bin/pytest -m portable -v` / `npm ci && node --test --experimental-test-coverage --test-coverage-include=public/app.js tests/app_js.test.mjs` |
| `lint` | pycodestyle, pylint | `git ls-files '*.py' \| xargs pycodestyle` / `... \| xargs pylint --rcfile=.pylintrc` |
| `types` | pyright over `backend/` | `pyright` |
| `eslint` | eslint over `public/` and the JavaScript tests in `tests/` | `npm ci && npx eslint public tests` |
| `audit` | pip-audit over each of the eight requirements manifests separately (a joint `--require-hashes` run fails), plus `npm audit`; also on a daily cron | `pip-audit -r requirements.txt` and the same again for each of the other seven: `requirements-dev.txt`, `requirements-test.txt`, `requirements-diff-cover.txt`, `requirements-audit.txt`, `requirements-zizmor.txt`, `requirements-build.txt`, `requirements-speed.txt` |
| `codeql` | security analysis of the Python, JavaScript and Actions (the workflows language), weekly cron | — (GitHub-hosted) |
| `size-baseline` | the tracked-file line-count ratchet over `.github/size-baseline.json` | — (GitHub-hosted action) |
| `actionlint` | actionlint + zizmor over the workflows themselves | `actionlint .github/workflows/*.yml && zizmor .github/workflows/` |
| `secrets` | gitleaks over the full history and working tree; also on a daily cron | `gitleaks git --no-banner --verbose --redact=100 . && gitleaks dir --no-banner --verbose --redact=100 .` |
| `speed` | inside the digest-pinned image `.github/speed/Dockerfile` names, this head's workload instruction counts under `valgrind --tool=cachegrind --cache-sim=no` against `.github/speed-budget.json`, with a recorded toolchain (Python, valgrind, that image, the `requirements-speed.txt` hash); fails on a missing declared workload, a measured-but-undeclared workload, or a count over `budget * (1 + tolerance_pct / 100)`; on a toolchain mismatch the same job also measures a base commit paired with the checked-out one (on a pull request, the base tip its merge commit was built on; on a push, the push's `before` when that is a full SHA of an ancestor, else, for an all-zero `before` (a new branch) or one that is not a fetched ancestor (a rewritten history), the checkout's first parent; on any other event, the checkout's first parent) with a venv built from that commit's own `requirements-speed.txt`, then fails on a declared workload missing from either measurement, a workload either measured but the budget does not declare, or a head count over `base * (1 + tolerance_pct / 100)`; the job also fails when the event name is missing, the history is shallow, a pull request's base is missing, unknown or not an ancestor of the checkout, a push's `before` is missing or not a full lowercase SHA, the rule falls back to the checkout's first parent and the checkout has none (a commit without a parent has nothing to pair with), the base measurement names no commit, or the base and head toolchains differ on any field but `lock_sha256` | `.venv/bin/python scripts/ci/speed.py measure --database docs_speed --out measurement.json && .venv/bin/python scripts/ci/speed.py check --measurements measurement.json --budget .github/speed-budget.json`, with valgrind and a Postgres you can `createdb` on; only inside the same image do the counts compare with CI's (the docker command is below the speed paragraphs); `docs_speed` is a disposable database, reset by `measure` |

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

The speed budget ratchets down in the same workflow: after a green
`master` push, `tighten` uses the `speed-measurement` artifact to lower
each workload whose count fell below its budget by more than half of
`tolerance_pct` (a percentage of that workload's budget) to its measured
count, only with a matching toolchain and
no failed workloads; a smaller drop keeps the recorded count, so
run-to-run spread under that margin commits nothing. It keeps the
toolchain and tolerance; when the budget changes and `master` has not
moved, the speed arm commits "Tighten the speed budget (automated)". A
raise is never automatic: each raised workload count or `tolerance_pct`
needs exactly one matching line in a commit message:

```
Budget-Raise: .github/speed-budget.json <key> <from> -> <to>
```

That commit touches nothing but the budget file; `<key>` is the leaf path
(e.g. `workloads["service.list"]` or `tolerance_pct`). The check refuses
malformed, duplicate, leftover or otherwise unconsumed declarations. A
rebase merge keeps the declaration lines; a squash may drop them, which
then refuses on `master`'s push.

A toolchain mismatch (any recorded `toolchain` field, `lock_sha256`
included, differing from the run's) is judged by that paired run: the
head's counts against the base commit's, measured in the same job by
this checkout's `scripts/ci/speed.py`, the budget still deciding the
workload set. A pull request that moves a toolchain field (the
`requirements-speed.txt` lock, the image, Python or valgrind) should
carry its declared rebaseline commit, taken from its own CI run's
measurement, whether its paired run passes or fails; on a Dependabot pull
request a maintainer adds it. After that commit the toolchains match and
the ordinary budget check applies. Until one lands on `master`, every run
whose toolchain differs from the budget's takes that paired path,
refusals included: its counts are compared only with its own base
commit's, the budget's counts are not consulted, and `tighten` lowers
nothing. To rebaseline, take `measurement.json` from that run's
`speed-measurement` artifact, run
`python3 scripts/ci/speed.py rebaseline --measurements measurement.json --budget .github/speed-budget.json`,
review the resulting counts, and commit only the budget. Every changed
`toolchain` leaf, including `lock_sha256`, needs its exact declaration like
counts do, for example `Budget-Raise: .github/speed-budget.json toolchain.image "old image" -> "new image"`.
Toolchain endpoints use JSON strings or `null`; `absent` declares an added or
removed key. Include the numeric declarations above for any raised counts or
tolerance in the same commit.

The speed job runs in a container from the image `.github/speed/Dockerfile`
pins by digest, and installs valgrind and `libc6-dbg` at exact versions from
a snapshot.debian.org source fixed to that image's build timestamp; the
counts depend on that image, so a count taken outside it does not compare
with CI's. To measure in that image, put the `run` lines of the speed job's
install, virtualenv and measure steps into `steps.sh` in an empty directory
`out/`, and run them in the image beside a Postgres reachable as `postgres`:

```bash
image="$(sed -n 's/\r$//; s/^FROM //p' .github/speed/Dockerfile)"
docker network create speed
docker run -d --name speed-pg --network speed --network-alias postgres \
  -e POSTGRES_PASSWORD=postgres "$(sed -n 's/\r$//; s/^FROM //p' .github/postgres/17/Dockerfile)"
# TCP, because the image's first-start init server listens on the socket only;
# 60 tries, so a container that exited ends the wait instead of spinning on it.
for try in $(seq 60); do
  docker exec speed-pg pg_isready -q -h 127.0.0.1 -U postgres && break
  [ "$try" -lt 60 ] || echo "speed-pg is not ready after 60 tries; see docker logs speed-pg" >&2
  sleep 1
done
docker run --rm --network speed --ulimit nofile=65536:65536 \
  -v "$PWD:/src:ro" -v "$PWD/out:/out" -w /src \
  -e RUNNER_TEMP=/out -e GITHUB_OUTPUT=/out/output -e GITHUB_WORKSPACE=/src \
  -e DOCS_HUB_SPEED_IMAGE="$image" -e PGHOST=postgres -e PGUSER=postgres \
  -e PGPASSWORD=postgres -e PYTHONDONTWRITEBYTECODE=1 "$image" bash -e /out/steps.sh
docker rm -f speed-pg && docker network rm speed
```

Run the last line, which removes the Postgres container and the network,
whether or not the measurement succeeded.
`out/measurement.json` is then a head measurement taken the way the job
takes it. Counts are expected to agree with a CI measurement of the same
commit only inside this same pinned image. The one spread measured is
between two same-code, same-toolchain GitHub-hosted runs, taken before the
job moved into this image (at most 0.06%); how closely a run on another
host agrees with CI is not measured. Valgrind refuses
to start under Docker's near-unlimited open-file limit, hence `--ulimit`. A
digest bump of that image is a toolchain change like any other, and it
needs the snapshot timestamp moved to the new image's own (its
`/etc/apt/sources.list.d/debian.sources` names it) and the valgrind and
`libc6-dbg` pins moved to that snapshot's versions. Dependabot's pull
requests for that image (the `/.github/speed` directory of the docker
entry in `.github/dependabot.yml`) move none of those. `libc6-dbg` depends
on the exact `libc6` version, so once the new image's `libc6` differs from
the pinned one, such a pull request's install step fails until a
maintainer commit moves the snapshot timestamp and both pins.

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
