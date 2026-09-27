"""Exercise redirect headers through the recording origin's HTTP server."""

from __future__ import annotations

import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from acas_egress_origin import Origin  # noqa: E402


@pytest.fixture
def origin():
    class Handler(Origin):
        events: list[dict[str, str]] = []
        lock = threading.Lock()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


@pytest.mark.parametrize("separator", ["\r", "\n", "\r\n", "\r\n\r\n"])
def test_redirect_rejects_decoded_header_line_breaks(origin, separator):
    target = "https://b.probe.example/probe" + separator + "X-Injected: yes"
    connection = HTTPConnection("127.0.0.1", origin, timeout=5)
    try:
        connection.request("GET", "/redirect/302?" + urlencode({"to": target}))
        response = connection.getresponse()
        assert response.status == 400
        assert response.getheader("Location") is None
        assert response.getheader("X-Injected") is None
        assert b"X-Injected" not in response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("status", [302, 303, 307, 308])
def test_redirect_preserves_valid_target_and_status(origin, status):
    target = "https://b.probe.example/probe?id=next&value=%2F#fragment"
    connection = HTTPConnection("127.0.0.1", origin, timeout=5)
    try:
        connection.request("POST", f"/redirect/{status}?" + urlencode({"to": target}))
        response = connection.getresponse()
        assert response.status == status
        assert response.getheader("Location") == target
        assert response.read() == b"probe origin\n"
    finally:
        connection.close()
