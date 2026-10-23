"""CLI filter values round-trip through a real loopback HTTP server."""
# protected-access: pin the CLI's import-time URL to the local fixture.
# redefined-outer-name: pytest fixtures are requested by parameter name.
# pylint: disable=protected-access,redefined-outer-name
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from docs_hub_cli import cli


@pytest.fixture
def filter_server(monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # pylint: disable=invalid-name
            url = urlsplit(self.path)
            received.append((url.path, parse_qs(url.query)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"docs": [], "tags": []}')

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        monkeypatch.setattr(cli, "_DEFAULT_URL",
                            f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("DOCS_HUB_API_KEY", "test-api-key")
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield received
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("value", [
    "Ordinary Research", "R&D", "C++", "Žluťoučký東京", "a#b?c=d%20/e",
])
@pytest.mark.parametrize("command, filters", [
    ("list", ("project",)),
    ("list", ("agent",)),
    ("list", ("project", "agent")),
    ("tags", ("project",)),
])
def test_cli_filters_round_trip(filter_server, monkeypatch, command, filters, value):
    argv = ["docs-hub", command]
    expected = {}
    for name in filters:
        requested = f"{name}:{value}"
        argv.extend([f"--{name}", requested])
        expected[name] = [requested]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    assert filter_server == [(f"/api/{command}", expected)]
