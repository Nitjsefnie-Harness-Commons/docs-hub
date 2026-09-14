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

_STYLE = """
:root { color-scheme: light dark; --bg: #ffffff; --fg: #1a1a1a;
        --muted: #5a5a5a; --rule: #d8d8d8; --code-bg: #f4f4f4; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font-family: system-ui, -apple-system, "Segoe UI", Roboto,
                    "Helvetica Neue", Arial, sans-serif;
       line-height: 1.55; }
main.md { padding: 2rem 1.25rem 4rem; }
main.md > :first-child { margin-top: 0; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 2rem 0 0.75rem; }
h1 { font-size: 1.9rem; }
h2 { font-size: 1.45rem; }
h3 { font-size: 1.2rem; }
a { color: #0b57d0; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2rem 0; }
code, pre, kbd, samp { font-family: ui-monospace, SFMono-Regular, Menlo,
                       Consolas, "Liberation Mono", monospace;
                       font-size: 0.9em; }
code { background: var(--code-bg); padding: 0.15em 0.35em;
       border-radius: 3px; }
pre { background: var(--code-bg); padding: 0.85rem 1rem; border-radius: 5px;
      overflow-x: auto; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; display: block; overflow-x: auto;
        max-width: 100%; margin: 1rem 0; }
th, td { border: 1px solid var(--rule); padding: 0.4rem 0.7rem;
         text-align: left; }
th { background: var(--code-bg); }
blockquote { margin: 1rem 0; padding: 0.1rem 1rem;
             border-left: 4px solid var(--rule); color: var(--muted); }
img { max-width: 100%; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #16181c; --fg: #e6e6e6; --muted: #a6a6a6;
          --rule: #3a3d42; --code-bg: #24262b; }
  a { color: #8ab4f8; }
}
"""


def render_markdown(source: bytes, title: str) -> str:
    """A whole HTML page showing `source` rendered, titled `title`.

    `source` is the stored Markdown blob; undecodable bytes become U+FFFD
    rather than raising, so a mis-encoded upload still reads.
    """
    body = _MD.render(source.decode("utf-8", "replace"))
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n<main class=\"md\">\n"
        f"{body}</main>\n</body>\n</html>\n"
    )
