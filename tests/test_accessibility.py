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
