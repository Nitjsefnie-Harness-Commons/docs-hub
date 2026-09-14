# Markdown Documents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A document may be published as Markdown. The hub stores the Markdown source verbatim, renders it to a readable HTML page for browsers at serve time, and hands the raw source back to agents.

**Architecture:** A `format` column on `doc_versions` (`html` | `markdown`, default `html`) recorded at publish; Markdown blobs are stored as `v<N>.md` next to the existing `v<N>.html` layout. A small `backend/render.py` turns Markdown into a complete HTML document with `markdown-it-py` (CommonMark + tables + strikethrough, raw HTML escaped, so a Markdown doc can never carry a script). The browser routes `/d/<slug>` and `/d/<slug>/v<n>` render Markdown; the agent route `/api/doc/<slug>` returns the stored bytes verbatim with `text/markdown`. The API infers the format from the uploaded filename when not stated; the CLI infers it from the file extension. List/version responses carry `format`; the SPA shows an `md` chip.

**Tech Stack:** Python 3.13, FastAPI, psycopg3, Postgres, `markdown-it-py` (new server dependency); stdlib-only CLI; vanilla-JS SPA; pytest.

**Spec:** No separate spec file. The binding requirements are the Global Constraints and task texts below.

## Global Constraints

- **Formats:** exactly two, spelled `html` and `markdown`. Anything else is invalid.
- **Column:** `doc_versions.format TEXT NOT NULL DEFAULT 'html'`, in `backend/schema.sql` AND applied idempotently by `db.migrate()` (`ALTER TABLE doc_versions ADD COLUMN IF NOT EXISTS format text NOT NULL DEFAULT 'html'`). No CHECK constraint (Postgres has no `ADD CONSTRAINT IF NOT EXISTS`); validation is in code.
- **Storage:** `storage.blob_path(slug, version, ext="html")` / `storage.store_blob(slug, version, data, ext="html")`; a Markdown version is stored at `<STORE_ROOT>/<slug>/v<N>.md`, byte-for-byte as uploaded. Stored source is never rewritten (CONTRIBUTING invariant, reworded in Task 3).
- **Rendering:** `backend/render.py` exposes `render_markdown(source: bytes, title: str) -> str` returning a complete HTML document (`<!doctype html>`, `<html lang="en">`, `<meta charset="utf-8">`, viewport meta, `<title>` with the title HTML-escaped, an inline `<style>`, `<body><main class="md">…rendered…</main></body>`). Parser: `MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])`. `html: False` means raw HTML in the source is escaped — a `<script>` in a Markdown doc renders as text. Decode the source as UTF-8 with `errors="replace"`. The style: system font stack, `max-width: 80ch`, centred, `line-height: 1.55`, monospace `code`/`pre` with a light background and horizontal scroll on `pre`, bordered tables with cell padding, a left-bordered `blockquote`, responsive images (`img { max-width: 100% }`), and a `prefers-color-scheme: dark` block that flips background/foreground/code background.
- **Repository:** `docs_repo.publish(..., fmt: str = "html")` stores with the matching extension and records `format`; `get_version`/`get_latest` dicts gain `"format"` and keep the key `"html"` for the raw bytes (renamed keys would ripple through views/api/tests — keep `"html"` as the bytes key, document it in the docstring). `list_docs()` dicts gain `"format"` (of the latest version, via the existing join). `list_versions()` entries gain `"format"`.
- **API:** `POST /api/publish` form field `format: str = Form("")`. Empty → infer from `file.filename`: a name ending in `.md` or `.markdown` (case-insensitive) → `markdown`, else `html`. Non-empty → lower-cased must be `html` or `markdown`, else HTTP 400 `{"ok": false, "error": "invalid format: <value!r>"}`. Success JSON gains `"format"`. `GET /api/doc/<slug>` returns the stored bytes verbatim: `media_type="text/markdown; charset=utf-8"` for markdown, unchanged `HTMLResponse` for html. `GET /api/list` docs gain `"format"`; `GET /api/versions/<slug>` entries gain `"format"`.
- **Views:** `/d/<slug>` and `/d/<slug>/v<n>`: markdown → `HTMLResponse(render_markdown(doc["html"], doc["title"]))`; html → unchanged. The anonymous public path is untouched (it only gates; a public markdown doc renders the same way).
- **CLI:** `docs-hub publish FILE ... [--format html|markdown]`; when `--format` is omitted nothing is sent and the server infers from the filename (the multipart part already carries the basename). When given, it is sent as the `format` field verbatim (the server validates). Publish output appends ` [markdown]` when the response's `format` is `markdown`. `docs-hub get`: `_request` returns `(status, body, content_type)`; `--text-only` skips the HTML-to-text pass and writes the body unchanged when `content_type` starts with `text/markdown`. `docs-hub list` appends ` [markdown]` after the title (before any ` expires …` suffix) when a doc's `format` is `markdown`; `docs-hub versions` appends ` markdown` to a row when its `format` is `markdown`. Old servers omit the key → no suffix. Bump `docs_hub_cli.__version__` to `0.3.0`.
- **Dependency:** add `markdown-it-py` to `requirements.txt` (server only; the CLI stays stdlib-only). Deployment note in README: re-run `pip install -r requirements.txt` before restarting.
- **Invariants from CONTRIBUTING.md hold:** one anonymous surface unchanged; parameterised SQL; versions append-only; agent key ≠ human session.
- **Gates:** `.venv/bin/pytest -q` green (coverage floor 90%); `git ls-files '*.py' | xargs .venv/bin/pycodestyle`; `git ls-files '*.py' | xargs .venv/bin/pylint --rcfile=.pylintrc`; `.venv/bin/pyright`; `npx eslint public` (Task 4); coverage as CI: `.venv/bin/python -m coverage run --source=backend,docs_hub_cli -m pytest -q && .venv/bin/python -m coverage report --fail-under=90`. Run from the worktree root `/tmp/docs-hub-markdown`.
- **Commits:** one logical commit per task, explicit paths (never `git add -A`), trailer exactly `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Tests:** pytest, existing style (`_client()`, `KEY`, `_Transport`). Only one pytest run at a time (the suite creates/drops `docs_test` and binds port 8099).

---

### Task 1: Format column, Markdown storage, and the renderer

**Files:**
- Modify: `backend/schema.sql`, `backend/db.py` (`migrate()`), `backend/storage.py`, `backend/docs_repo.py`
- Create: `backend/render.py`
- Modify: `requirements.txt`
- Test: `tests/test_render.py` (create), `tests/test_storage.py`, `tests/test_docs_repo.py`, `tests/test_db.py` (extend)

**Interfaces:**
- Produces: `storage.blob_path(slug, version, ext="html")`, `storage.store_blob(slug, version, data, ext="html")`.
- Produces: `docs_repo.publish(..., fmt="html")`; version dicts carry `"format"`; `list_docs`/`list_versions` carry `"format"`.
- Produces: `render.render_markdown(source: bytes, title: str) -> str`.

- [ ] **Step 1: Schema + migration** — add the column to `doc_versions` in `backend/schema.sql`:
```sql
    format      TEXT NOT NULL DEFAULT 'html',
```
and in `db.migrate()`:
```python
        c.execute(
            "ALTER TABLE doc_versions ADD COLUMN IF NOT EXISTS "
            "format text NOT NULL DEFAULT 'html'"
        )
```
Extend the migrate test in `tests/test_db.py` (mirror the `expires_at` one) to assert `doc_versions.format` exists after two runs.

- [ ] **Step 2: Failing storage tests** — in `tests/test_storage.py` add: `blob_path("a/b", 3, ext="md")` ends with `a/b/v3.md`; default ext still `.html`; `store_blob(..., ext="md")` writes the `.md` file and returns its path.

- [ ] **Step 3: Storage** — `blob_path(slug, version, ext="html")` builds `f"v{version}.{ext}"`; `store_blob(slug, version, data, ext="html")` passes it through. Update the module docstring layout line to `v<N>.html` or `v<N>.md`.

- [ ] **Step 4: Failing renderer tests** — `tests/test_render.py`:
```python
from backend.render import render_markdown


def test_renders_headings_paragraphs_and_lists():
    out = render_markdown(b"# Title\n\nSome *text*.\n\n- a\n- b\n", "T")
    assert out.startswith("<!doctype html>")
    assert "<h1>Title</h1>" in out
    assert "<em>text</em>" in out
    assert "<li>a</li>" in out


def test_raw_html_in_source_is_escaped_not_executed():
    out = render_markdown(b"hi <script>alert(1)</script>", "T")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_title_is_escaped():
    out = render_markdown(b"x", "<b>T</b> & co")
    assert "<title>&lt;b&gt;T&lt;/b&gt; &amp; co</title>" in out


def test_tables_and_strikethrough_are_enabled():
    out = render_markdown(b"| a | b |\n|---|---|\n| 1 | 2 |\n\n~~gone~~\n", "T")
    assert "<table>" in out and "<td>1</td>" in out
    assert "<s>gone</s>" in out


def test_fenced_code_is_kept_verbatim_and_escaped():
    out = render_markdown(b"```py\nif a < b: pass\n```\n", "T")
    assert "<pre><code class=\"language-py\">if a &lt; b: pass\n</code></pre>" in out


def test_invalid_utf8_does_not_raise():
    out = render_markdown(b"ok \xff\xfe bytes", "T")
    assert "ok" in out


def test_document_shell_has_charset_viewport_and_style():
    out = render_markdown(b"x", "T")
    assert '<meta charset="utf-8">' in out
    assert 'name="viewport"' in out
    assert "<style>" in out and "prefers-color-scheme: dark" in out
    assert '<main class="md">' in out
```

- [ ] **Step 5: Renderer** — `backend/render.py`:
```python
"""Render a stored Markdown source into a complete, self-contained HTML page.

The hub stores what was published and never rewrites it; this module is the
serve-time view of a Markdown version for browsers. `html=False` makes
markdown-it escape any raw HTML in the source, so a Markdown document can
never carry a script — unlike a published HTML document, which is trusted
agent output and served verbatim.
"""
from __future__ import annotations

import html

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False,
                                "typographer": False}).enable(
    ["table", "strikethrough"])

_STYLE = """..."""  # the style described in Global Constraints


def render_markdown(source: bytes, title: str) -> str:
    body = _MD.render(source.decode("utf-8", "replace"))
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n<main class=\"md\">\n"
        f"{body}</main>\n</body>\n</html>\n"
    )
```
Add `markdown-it-py` to `requirements.txt`.

- [ ] **Step 6: Failing repository tests** — in `tests/test_docs_repo.py`:
```python
def test_publish_markdown_stores_md_blob_and_format():
    res = docs_repo.publish("m/one", "M", [], None, "analyst", b"# hi\n",
                            fmt="markdown")
    assert res["format"] == "markdown"
    v = docs_repo.get_version("m/one", 1)
    assert v["format"] == "markdown"
    assert v["html"] == b"# hi\n"
    assert v["file_path"].endswith("m/one/v1.md")
    assert os.path.exists(v["file_path"])


def test_publish_default_format_is_html():
    docs_repo.publish("m/h", "H", [], None, "analyst", b"<h1>h</h1>")
    v = docs_repo.get_latest("m/h")
    assert v["format"] == "html"
    assert v["file_path"].endswith("m/h/v1.html")


def test_versions_of_one_slug_may_differ_in_format():
    docs_repo.publish("m/mix", "X", [], None, "analyst", b"<h1>1</h1>")
    docs_repo.publish("m/mix", "X", [], None, "analyst", b"# 2\n", fmt="markdown")
    vs = docs_repo.list_versions("m/mix")
    assert [(v["version"], v["format"]) for v in vs] == [(2, "markdown"), (1, "html")]
    d = next(x for x in docs_repo.list_docs() if x["slug"] == "m/mix")
    assert d["format"] == "markdown"


def test_publish_rejects_unknown_format():
    with pytest.raises(ValueError, match="invalid format"):
        docs_repo.publish("m/bad", "B", [], None, "analyst", b"x", fmt="rtf")
```

- [ ] **Step 7: Repository** — in `docs_repo.py`: `FORMATS = ("html", "markdown")`; `publish(..., fmt: str = "html")` raises `ValueError(f"invalid format: {fmt!r}")` when not in `FORMATS`; `ext = "md" if fmt == "markdown" else "html"`; `storage.store_blob(slug, version, html, ext=ext)`; INSERT includes `format`; return dict gains `"format": fmt`. `_version_row` selects `v.format` and returns `"format"`. `list_docs` selects `v.format` and returns `"format"`. `list_versions` selects and returns `"format"`. Update the `publish` docstring: the `html` parameter is the raw bytes of either format.

- [ ] **Step 8: Gates, then commit**
```bash
git add backend/schema.sql backend/db.py backend/storage.py backend/docs_repo.py backend/render.py requirements.txt tests/test_render.py tests/test_storage.py tests/test_docs_repo.py tests/test_db.py
git commit -m "Store Markdown versions verbatim and render them for browsers"
```

---

### Task 2: API and view routes

**Files:**
- Modify: `backend/api.py`, `backend/views.py`, `README.md` (Deployment)
- Test: `tests/test_app.py`, `tests/test_public.py` (extend)

**Interfaces:**
- Consumes: Task 1's `publish(fmt=)`, `"format"` keys, `render_markdown`.

- [ ] **Step 1: Failing tests** — `tests/test_app.py`:
```python
def _publish_md(c, slug, body=b"# hello\n\n<script>x</script>\n", **extra):
    return c.post("/api/publish",
                  data={"slug": slug, "title": slug, "from": "analyst", **extra},
                  files={"file": ("d.md", body, "text/markdown")}, headers=KEY)


def test_publish_infers_markdown_from_filename():
    c = _client()
    r = _publish_md(c, "md/infer")
    assert r.status_code == 200, r.text
    assert r.json()["format"] == "markdown"
    raw = c.get("/api/doc/md/infer", headers=KEY)
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/markdown")
    assert raw.content == b"# hello\n\n<script>x</script>\n"


def test_explicit_format_overrides_filename():
    c = _client()
    r = c.post("/api/publish",
               data={"slug": "md/explicit", "title": "E", "from": "analyst",
                     "format": "markdown"},
               files={"file": ("d.html", b"# md in html name\n", "text/html")},
               headers=KEY)
    assert r.json()["format"] == "markdown"


def test_publish_rejects_unknown_format():
    c = _client()
    r = _publish_md(c, "md/bad", format="rtf")
    assert r.status_code == 400
    assert r.json()["error"].startswith("invalid format")


def test_browser_routes_render_markdown_and_escape_html():
    c = _client()
    _publish_md(c, "md/render")
    for path in ("/d/md/render", "/d/md/render/v1"):
        r = c.get(path, headers=KEY)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "<h1>hello</h1>" in r.text
        assert "<script>" not in r.text
        assert "&lt;script&gt;" in r.text
        assert "<title>md/render</title>" in r.text


def test_html_docs_are_still_served_verbatim():
    c = _client()
    _publish(c, "md/html", body=b"<h1>raw</h1><script>ok()</script>")
    r = c.get("/d/md/html", headers=KEY)
    assert r.content == b"<h1>raw</h1><script>ok()</script>"
    assert c.get("/api/doc/md/html", headers=KEY).headers["content-type"].startswith("text/html")


def test_list_and_versions_carry_format():
    c = _client()
    _publish(c, "md/fmt", body=b"<h1>1</h1>")
    _publish_md(c, "md/fmt")
    docs = c.get("/api/list", headers=KEY).json()["docs"]
    assert next(d for d in docs if d["slug"] == "md/fmt")["format"] == "markdown"
    vs = c.get("/api/versions/md/fmt", headers=KEY).json()["versions"]
    assert [(v["version"], v["format"]) for v in vs] == [(2, "markdown"), (1, "html")]
```
`tests/test_public.py`:
```python
def test_public_markdown_doc_renders_anonymously():
    c = _client()
    c.post("/api/publish",
           data={"slug": "pub/md", "title": "Pub MD", "from": "analyst"},
           files={"file": ("d.md", b"# open\n", "text/markdown")}, headers=KEY)
    _set_public(c, "pub/md", True)
    r = _client().get("/d/pub/md")
    assert r.status_code == 200
    assert "<h1>open</h1>" in r.text
```

- [ ] **Step 2: API** — in `publish`: parameter `format: str = Form("", alias="format")` — name the Python parameter `fmt` with `alias="format"` (the builtin name is worth avoiding); resolve:
```python
    name = (file.filename or "").lower()
    if fmt.strip():
        fmt = fmt.strip().lower()
        if fmt not in docs_repo.FORMATS:
            return JSONResponse({"ok": False, "error": f"invalid format: {fmt!r}"},
                                status_code=400)
    else:
        fmt = "markdown" if name.endswith((".md", ".markdown")) else "html"
```
pass `fmt=fmt` to `docs_repo.publish`; success JSON gains `"format": res["format"]`. `get_doc`: if `doc["format"] == "markdown"` return `Response(doc["html"], media_type="text/markdown; charset=utf-8")`, else the existing `HTMLResponse`. `api_list`: nothing to convert (`format` is a string) — it flows through. `api_versions`: same.

- [ ] **Step 3: Views** — `render_version` and `render_latest`: extract a helper `_serve(doc)` that returns `HTMLResponse(render.render_markdown(doc["html"], doc["title"]))` for markdown and `HTMLResponse(doc["html"])` otherwise.

- [ ] **Step 4: README Deployment** — under the deployment section add: "After pulling a version that changes `requirements.txt`, run `.venv/bin/pip install -r requirements.txt` before restarting the unit."

- [ ] **Step 5: Gates, then commit**
```bash
git add backend/api.py backend/views.py README.md tests/test_app.py tests/test_public.py
git commit -m "Accept Markdown on publish and render it on the browser routes"
```

---

### Task 3: CLI, docs, and the version bump

**Files:**
- Modify: `docs_hub_cli/cli.py`, `docs_hub_cli/__init__.py` (`0.3.0`), `README.md` ("What it does", CLI table), `AGENTS.md` (Architecture), `CONTRIBUTING.md` (invariants row)
- Test: `tests/test_cli_unit.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `format` form field; `format` in publish/list/versions JSON; `text/markdown` content type on `/api/doc`.

- [ ] **Step 1: Enumerate `_request` call sites** — `grep -n "_request" docs_hub_cli/cli.py tests/test_cli_unit.py` and read every hit before changing its return shape: `cmd_publish`, `cmd_get`, `cmd_list`, `cmd_versions`, `cmd_tags`, and the `_Transport` fake plus the direct `_request` tests.

- [ ] **Step 2: Failing unit tests** (same style as the file): `publish --format markdown` sends a `name="format"` part with `markdown`; `publish` without `--format` sends no `format` part; publish output ends with ` [markdown]` when the response says so and is unchanged otherwise; `get --text-only` writes the body unchanged when the transport reports `text/markdown; charset=utf-8` and still strips HTML for `text/html`; `list` appends ` [markdown]` after the title for a markdown doc (and before ` expires …` when both apply), nothing for html or a missing key; `versions` appends ` markdown` for a markdown version; `main` accepts `--format`. Update `_Transport` to return `(status, body, content_type)` with a default content type of `text/html; charset=utf-8`, and every existing test that unpacks two values.

- [ ] **Step 3: Implement** — `_request` returns `(status, body, content_type)` where `content_type = resp.headers.get("Content-Type", "")` (and `e.headers.get(...)` on `HTTPError`); update all five call sites. `cmd_publish`: `if args.format: fields["format"] = args.format`; suffix ` [markdown]` when `payload.get("format") == "markdown"`. `cmd_get`: `text_only and not content_type.startswith("text/markdown")` gates the strip. `cmd_list`/`cmd_versions` suffixes per Global Constraints. Parser: `--format`, `choices=["html", "markdown"]`, `default=""`, help `document format; omit to infer from the file extension (.md/.markdown → markdown)`. `__version__ = "0.3.0"`.

- [ ] **Step 4: End-to-end** — in `tests/test_cli.py`: publish a `.md` file through the subprocess CLI, assert the output ends with ` [markdown]`, and `get --text-only` returns the Markdown source unchanged.

- [ ] **Step 5: Docs** — `README.md` "What it does": add `- Documents may be Markdown (`.md`): the source is stored verbatim, rendered to a readable page for browsers, and returned raw (`text/markdown`) to agents.`; CLI table row gains `[--format html|markdown]`. `AGENTS.md` Architecture: add a bullet: `- Markdown docs: `doc_versions.format` (`html` | `markdown`), inferred from the upload filename unless the `format` field says otherwise; stored verbatim as `v<N>.md`; `backend/render.py` (markdown-it-py, raw HTML escaped) renders it on `/d/…` at serve time; `/api/doc/<slug>` returns the raw source as `text/markdown`.` `CONTRIBUTING.md` invariants row "Stored HTML is stored verbatim" → "Stored documents are stored verbatim" with the second column: "The hub serves a published HTML document exactly as published and never rewrites a stored body. A Markdown document is rendered for browsers at serve time from its stored source; the source itself is never changed."

- [ ] **Step 6: Gates, then commit**
```bash
git add docs_hub_cli/cli.py docs_hub_cli/__init__.py README.md AGENTS.md CONTRIBUTING.md tests/test_cli_unit.py tests/test_cli.py
git commit -m "Publish Markdown from the CLI and document the format"
```

---

### Task 4: Format chip in the SPA

**Files:**
- Modify: `public/app.js` (`renderIndex` row, `renderVersions` rows), `public/styles.css`

- [ ] **Step 1**: read `renderIndex`, `renderVersions`, the existing `.ttl`/`.tag` shared rule.
- [ ] **Step 2**: index row — when `d.format === 'markdown'`, render `<span class="fmt" title="Markdown source, rendered by the hub">md</span>` in the `.slug` line before any `.ttl` chip (hoist into a `const fmt = …` like `size`/`ttl`). Versions view — the same chip per version when `v.format === 'markdown'`. Add `.fmt` to the shared `.tag, .ttl` rule (`.tag, .ttl, .fmt { … }`) with no extra rule unless spacing needs one. No signature changes (format only changes with a new version, which already bumps the signatures).
- [ ] **Step 3**: `npx eslint public` clean.
- [ ] **Step 4**:
```bash
git add public/app.js public/styles.css
git commit -m "Show a Markdown chip in the SPA"
```
