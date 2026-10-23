import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import app


@pytest.mark.parametrize("label, name", [("User ID", "user_id"), ("Password", "password")])
def test_login_labels_name_their_inputs(label: str, name: str) -> None:
    response = TestClient(app).get("/login")
    assert response.status_code == 200
    forms = re.findall(r"<form\b[^>]*>(.*?)</form>", response.text, re.S)
    assert len(forms) == 1
    labels = re.findall(rf"<label\b([^>]*)>\s*{label}\s*</label>", forms[0])
    assert len(labels) == 1
    target = re.search(r'\sfor="([^"]+)"', labels[0])
    assert target is not None, f"{label} label needs a non-empty for attribute"
    inputs = re.findall(rf'<input\b[^>]*\sname="{name}"[^>]*>', forms[0])
    assert len(inputs) == 1
    input_id = re.search(r'\sid="([^"]+)"', inputs[0])
    assert input_id is not None, f"{label} input needs a non-empty id"
    assert target[1] == input_id[1]


@pytest.mark.parametrize("name", ["project", "agent", "tag"])
def test_index_labels_name_their_selects(name: str) -> None:
    script = (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(
        encoding="utf-8")
    labels = re.findall(rf'<label\b([^>]*)>{name}</label>', script)
    assert len(labels) == 1, f"{name} needs a label element"
    assert ' class="label"' in labels[0]
    target = re.search(r'\sfor="([^"]+)"', labels[0])
    assert target is not None, f"{name} label needs a non-empty for attribute"
    select_ids = re.findall(rf'<select\b[^>]*\sid="(f-{name})"[^>]*>', script)
    assert select_ids == [f"f-{name}"]
    assert target[1] == select_ids[0]
    assert f'<span class="label">{name}</span>' not in script
    assert '<span class="label">search</span>' in script


@pytest.mark.parametrize("key, label", [
    ("title", "Title / Slug"), ("project", "Project"), ("tags", "Tags"),
    ("agent", "Agent"), ("updated", "Updated"), ("version", "Ver"), ("size", "Size"),
])
def test_index_sort_heading_has_a_native_button(key: str, label: str) -> None:
    script = (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(
        encoding="utf-8")
    headers = script.split("const headers = [", 1)[1].split("];", 1)[0]
    assert re.search(rf"key: '{key}',\s+label: '{label}'", headers)
    template = script.split("const headHtml = headers", 1)[1].split("const rowHtml", 1)[0]
    headings = re.findall(r"<th\b([^>]*)>(.*?)</th>", template, re.S)
    assert len(headings) == 1
    attributes, content = headings[0]
    button = re.fullmatch(r'<button\b([^>]*)>(.*?)</button>', content, re.S)
    assert button is not None, f"{label} needs a button inside its th"
    assert 'data-sort="${h.key}"' in button[1]
    assert 'type="button"' in button[1]
    assert 'data-sort' not in attributes
    assert button[2] == '${esc(h.label)}<span class="arrow">${glyph}</span>'


def test_index_sort_listener_uses_only_the_buttons() -> None:
    script = (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(
        encoding="utf-8")
    wire = script.split("function wireIndex(", 1)[1].split("function renderDocViewer", 1)[0]
    selectors = re.findall(r"\$\$\('([^']*data-sort[^']*)'\)", wire)
    assert selectors == ["button[data-sort]"]
    assert re.search(
        r"\$\$\('button\[data-sort\]'\)\.forEach\(\(button\) =>\s*"
        r"button\.addEventListener\('click', \(\) => \{\s*"
        r"setSort\(button\.dataset\.sort\);\s*reroute\(\);\s*\}\),\s*\);", wire)
    assert "th.addEventListener" not in wire


def test_index_sort_focus_is_captured_forwarded_and_restored() -> None:
    script = (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(
        encoding="utf-8")
    render = script.split("async function render() {", 1)[1].split("// ─", 1)[0]
    capture, after_replace = render.split("root.innerHTML = html;", 1)
    assert re.search(
        r"const sortWasFocused = document\.activeElement\?\.matches\('button\[data-sort\]'\)"
        r"\s*\? document\.activeElement\.dataset\.sort : null;", capture)
    assert "wireIndex(searchWasFocused, sortWasFocused)" in after_replace
    wire = script.split("function wireIndex(", 1)[1].split("function renderDocViewer", 1)[0]
    assert wire.startswith("searchWasFocused, sortWasFocused)")
    assert re.search(
        r'if \(sortWasFocused\) \{\s*'
        r'\$\(`button\[data-sort="\$\{sortWasFocused\}"\]`\)\?\.focus\(\);\s*\}', wire)
    assert "document.activeElement.id === 'f-q'" in capture
    assert re.search(
        r"if \(searchWasFocused\) \{\s*const inp = \$\('#f-q'\);\s*if \(inp\) \{\s*"
        r"inp\.focus\(\);\s*inp\.setSelectionRange\(inp\.value\.length, inp\.value\.length\);",
        wire)


def test_index_sort_buttons_keep_the_arrow_and_empty_row_span() -> None:
    script = (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(
        encoding="utf-8")
    headers = script.split("const headers = [", 1)[1].split("];", 1)[0]
    assert len(re.findall(r"key: '([^']+)'", headers)) == 7
    template = script.split("const headHtml = headers", 1)[1].split("const rowHtml", 1)[0]
    assert re.search(r'<button\b[^>]*>.*<span class="arrow">\$\{glyph\}</span></button>',
                     template)
    assert "FILTER.sort === h.key" in template
    assert "FILTER.sortDir === 'asc' ? '▲' : '▼'" in template
    assert '<td colspan="${headers.length}">' in script


def test_index_sort_buttons_inherit_the_heading_style() -> None:
    styles = (Path(__file__).resolve().parent.parent / "public" / "styles.css").read_text(
        encoding="utf-8")
    rules = re.findall(r"^\.docs-table thead th button\s*\{([^}]+)\}", styles, re.M)
    assert len(rules) == 1
    declarations = dict(re.findall(r"([\w-]+):\s*([^;]+);", rules[0]))
    expected = {
        "background": "transparent", "border": "0", "padding": "0",
        "font": "inherit", "color": "inherit", "width": "100%",
        "text-align": "inherit", "cursor": "pointer",
    }
    assert {name: declarations.get(name) for name in expected} == expected
    heading = re.search(r"^\.docs-table thead th\s*\{([^}]+)\}", styles, re.M)
    assert heading is not None
    assert "padding: 10px var(--row-pad-x);" in heading[1]


def test_index_sort_buttons_have_an_accent_focus_outline() -> None:
    styles = (Path(__file__).resolve().parent.parent / "public" / "styles.css").read_text(
        encoding="utf-8")
    rules = re.findall(r"^\.docs-table thead th button:focus-visible\s*\{([^}]+)\}",
                       styles, re.M)
    assert len(rules) == 1, "sort buttons need a visible keyboard focus rule"
    declarations = dict(re.findall(r"([\w-]+):\s*([^;]+);", rules[0]))
    assert declarations.get("outline") == "2px solid var(--accent)"
