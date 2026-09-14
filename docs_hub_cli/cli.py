#!/usr/bin/env python3
"""docs-hub CLI — publish HTML artifacts to docs.nitjsefni.eu and read them.

Auth: DOCS_HUB_API_KEY from the environment; if absent, falls back to
reading env.DOCS_HUB_API_KEY out of ~/.agent-bundle/settings.json directly.
Base URL: DOCS_HUB_URL or the production default.
Cross-platform: stdlib only (urllib), no third-party deps.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from html.parser import HTMLParser

from ._settings import setting

# The hub host is deployment identity, not a property of this CLI, so it lives
# in the `env` block of ~/.agent-bundle/settings.json. DOCS_HUB_URL was already the
# documented override; this makes the same key the SOURCE rather than an
# exception to a literal baked into a script that ships to every machine.
_DEFAULT_URL = setting("DOCS_HUB_URL", "")

# Tags whose text content is layout/markup noise, not document prose.
_SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template"}
# Tags that imply a line break in the rendered text.
_BLOCK_TAGS = {"p", "div", "br", "hr", "li", "tr", "section", "article",
               "header", "footer", "nav", "aside", "main", "ul", "ol",
               "table", "thead", "tbody", "blockquote", "pre", "figure",
               "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    """Collect rendered text from HTML, dropping script/style/head content
    and inserting newlines at block boundaries. convert_charrefs (default)
    unescapes entities for us."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _html_to_text(raw: bytes) -> str:
    """Strip HTML to readable plain text: drop markup-noise tags, break at
    block boundaries, collapse intra-line whitespace and blank-line runs."""
    parser = _TextExtractor()
    parser.feed(raw.decode("utf-8", "replace"))
    parser.close()
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip()
             for ln in parser.text().splitlines()]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out + "\n" if out else ""


def _base_url() -> str:
    url = _DEFAULT_URL
    if not url:
        print("ERROR: DOCS_HUB_URL not set (env or ~/.agent-bundle/settings.json)",
              file=sys.stderr)
        sys.exit(2)
    return url.rstrip("/")


def _api_key() -> str:
    # env -> settings.json, via the shared reader. This used to be a private
    # copy of that fallback living here; one reader means one precedence order
    # for every deployment value, and no second place to fix when it changes.
    key = setting("DOCS_HUB_API_KEY", "")
    if not key:
        print("ERROR: DOCS_HUB_API_KEY not set (env or ~/.agent-bundle/settings.json)",
              file=sys.stderr)
        sys.exit(2)
    return key


def _request(method: str, path: str, *, data: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(_base_url() + path, data=data, method=method)
    req.add_header("x-docs-key", _api_key())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        # Connection refused / DNS failure / timeout — clean message, not a
        # traceback (URLError is the non-HTTP failure superclass).
        print(f"ERROR: connection to {_base_url()} failed: {e.reason}",
              file=sys.stderr)
        sys.exit(1)


def _qs_escape(s: str) -> str:
    """RFC 6266 quoted-string escaping for Content-Disposition headers.
    Strips CR/LF (illegal in HTTP header values, classic header-injection
    vector) and backslash-escapes `\\` and `"`."""
    return (s.replace("\r", "").replace("\n", "")
             .replace("\\", "\\\\").replace('"', '\\"'))


def _multipart(fields: dict, file_field: str, filename: str,
               file_bytes: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{_qs_escape(k)}"\r\n\r\n{v}\r\n')
    ctype = mimetypes.guess_type(filename)[0] or "text/html"
    body = b"".join(p.encode() for p in parts)
    body += (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{_qs_escape(file_field)}"; '
             f'filename="{_qs_escape(filename)}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n").encode()
    body += file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def cmd_publish(args: argparse.Namespace) -> int:
    try:
        with open(args.file, "rb") as f:
            html = f.read()
    except OSError as e:
        # One line, like every other failure here — a traceback for a
        # mistyped path tells the operator nothing the message does not.
        print(f"ERROR: cannot read {args.file}: {e.strerror}",
              file=sys.stderr)
        return 1
    fields = {"slug": args.slug, "title": args.title,
              "tags": args.tags or "", "project": args.project or "",
              "from": getattr(args, "from")}
    # Sent only when given: the absence of the field is what asks for a
    # permanent document, and every publish restates the lifetime, so a
    # republish without --ttl is how an expiring document becomes permanent.
    # Stripped first: `--ttl "  "` is the same request as no --ttl at all,
    # and sending it blank would state a lifetime the wire format does not.
    ttl = args.ttl.strip()
    if ttl:
        fields["ttl"] = ttl
    body, ctype = _multipart(fields, "file", os.path.basename(args.file), html)
    status, raw = _request("POST", "/api/publish", data=body,
                           headers={"Content-Type": ctype})
    try:
        payload = json.loads(raw)
    except ValueError:
        # Non-JSON body (e.g. a Cloudflare 502 HTML page) — report status +
        # a short preview instead of a JSONDecodeError traceback.
        preview = raw[:200].decode("utf-8", "replace")
        print(f"ERROR: HTTP {status}: non-JSON response: {preview}",
              file=sys.stderr)
        return 1
    if status != 200 or not payload.get("ok"):
        print(f"ERROR: {payload.get('error', raw)}", file=sys.stderr)
        return 1
    if ttl and "expires_at" not in payload:
        # The key is absent, not null: a server older than the ttl field,
        # which accepted the publish and dropped the lifetime. Saying nothing
        # would leave the operator believing the document expires.
        print("WARNING: server did not confirm an expiry (it predates --ttl); "
              "the document is permanent", file=sys.stderr)
    suffix = f" (expires {payload['expires_at']})" if payload.get("expires_at") else ""
    print(f"published {payload['slug']} v{payload['version']} "
          f"-> {_base_url()}{payload['url']}{suffix}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    path = f"/d/{args.slug}/v{args.version}" if args.version else f"/api/doc/{args.slug}"
    status, raw = _request("GET", path)
    if status != 200:
        print(f"ERROR: HTTP {status}", file=sys.stderr)
        return 1
    out = _html_to_text(raw).encode("utf-8") if args.text_only else raw
    if args.output:
        with open(args.output, "wb") as f:
            f.write(out)
        print(f"wrote {args.output} ({len(out)} bytes)")
    else:
        sys.stdout.buffer.write(out)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    q = []
    if args.project:
        q.append(f"project={args.project}")
    if args.agent:
        q.append(f"agent={args.agent}")
    path = "/api/list" + ("?" + "&".join(q) if q else "")
    status, raw = _request("GET", path)
    if status != 200:
        print(f"ERROR: HTTP {status}", file=sys.stderr)
        return 1
    docs = json.loads(raw)["docs"]
    if args.untagged:
        docs = [d for d in docs if not d.get("tags")]
    for d in docs:
        tags = ",".join(d.get("tags") or []) or "-"
        expires = f" expires {d['expires_at']}" if d.get("expires_at") else ""
        print(f"{d['slug']:<40} v{d['latest_version']:<3} "
              f"{d['posted_by']:<14} [{tags}] {d['title']}{expires}")
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    status, raw = _request("GET", f"/api/versions/{args.slug}")
    if status != 200:
        print(f"ERROR: HTTP {status}", file=sys.stderr)
        return 1
    for v in json.loads(raw)["versions"]:
        print(f"v{v['version']:<3} {v['created_at']} {v['posted_by']:<12} "
              f"{v['byte_size']} bytes")
    return 0


def cmd_tags(args: argparse.Namespace) -> int:
    """List every tag in use with its count (most-used first). Agents call
    this before `publish` so they pick from the established tag set instead
    of inventing parallel ones."""
    path = "/api/tags" + (f"?project={args.project}" if args.project else "")
    status, raw = _request("GET", path)
    if status != 200:
        print(f"ERROR: HTTP {status}", file=sys.stderr)
        return 1
    for t in json.loads(raw)["tags"]:
        print(f"{t['count']:>5}  {t['tag']}")
    return 0


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        # getattr rather than a bare call inside try/except AttributeError:
        # both spellings behave identically at runtime, but typeshed types
        # sys.stdout as TextIO, which has no `reconfigure`, so only this one
        # type-checks. The sibling parsers extracted from the same bundle use
        # the same idiom.
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # not a reconfigurable stream (redirected/piped)
    ap = argparse.ArgumentParser(description="docs-hub CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish")
    p.add_argument("file")
    p.add_argument("--slug", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--tags", default="")
    p.add_argument("--project", default="")
    p.add_argument("--from", required=True, dest="from")
    p.add_argument("--ttl", default="",
                   help="lifetime after which the document vanishes, e.g. "
                        "30m, 2h, 7d, 2w (bare number = seconds, must be "
                        "≥ 1); omit = permanent")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("get")
    p.add_argument("slug")
    p.add_argument("--version", type=int, default=0)
    p.add_argument("-o", "--output", default="")
    p.add_argument("--text-only", action="store_true",
                   help="strip HTML tags and output plain text")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("list")
    p.add_argument("--project", default="")
    p.add_argument("--agent", default="")
    p.add_argument("--untagged", action="store_true",
                   help="only show docs with no tags")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("versions")
    p.add_argument("slug")
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("tags")
    p.add_argument("--project", default="")
    p.set_defaults(func=cmd_tags)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
