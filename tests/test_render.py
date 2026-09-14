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


def test_javascript_urls_are_dropped_from_links_and_images():
    # markdown-it's default validateLink refuses the javascript: scheme, so
    # neither an inline link nor an image is built at all: the source stays
    # literal paragraph text and no attribute carries the URL.
    out = render_markdown(b"[x](javascript:alert(1))\n\n"
                          b"![i](javascript:alert(1))\n", "T")
    body = out.split('<main class="md">')[1]
    assert "<a " not in body and "<img" not in body
    assert 'href="javascript:' not in out and 'src="javascript:' not in out
    assert "<p>[x](javascript:alert(1))</p>" in body


def test_raw_img_onerror_in_source_is_escaped():
    out = render_markdown(b"<img src=x onerror=alert(1)>\n", "T")
    assert "<img" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
