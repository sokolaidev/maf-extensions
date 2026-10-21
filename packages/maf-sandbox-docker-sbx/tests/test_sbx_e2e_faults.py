"""Exercise fault injection and socket restoration without sbx or credentials."""

from __future__ import annotations

import contextlib
import http.server
import json
import socket
import socketserver
import threading

import pytest
from _sbx_e2e_faults import _UnixConnection, engine_fault_responses

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix sockets")


@pytest.fixture
def daemon(tmp_path):
    path = tmp_path / "daemon.sock"
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = json.dumps({"real_daemon": True, "path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(socketserver.TCPServer):
        address_family = getattr(socket, "AF_UNIX")

    with Server(str(path), Handler) as server:
        path.chmod(0o600)
        identity = path.stat().st_ino
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield path, requests, identity
        finally:
            server.shutdown()
            thread.join(timeout=5)


def request(path, method, route):
    with contextlib.closing(_UnixConnection(path)) as client:
        client.request(method, route)
        response = client.getresponse()
        return response.status, json.loads(response.read())


@pytest.mark.parametrize("fail_body", [False, True])
def test_faults_cross_http_and_restore_the_original_socket(daemon, fail_body):
    path, requests, identity = daemon
    expected = (
        pytest.raises(RuntimeError, match="body failed") if fail_body else contextlib.nullcontext()
    )
    with expected, engine_fault_responses(path) as observed:
        assert path.stat().st_mode & 0o777 == 0o600
        assert request(path, "GET", "/daemon/settings?key=forwarding") == (
            200,
            {"real_daemon": True, "path": "/daemon/settings?key=forwarding"},
        )
        assert request(path, "GET", "/sandbox?all=true") == (200, [])
        for method, route in (("POST", "/sandbox/existing/exec"), ("DELETE", "/sandbox/existing")):
            assert request(path, method, route) == (500, {"message": "backend unavailable"})
        assert request(path, "POST", "/sandbox") == (500, {"message": "backend unavailable"})
        assert observed == [
            "listing",
            "POST /sandbox/existing/exec",
            "DELETE /sandbox/existing",
            "POST /sandbox",
        ]
        assert requests == ["/daemon/settings?key=forwarding"]
        if fail_body:
            raise RuntimeError("body failed")
    assert path.stat().st_ino == identity
    assert request(path, "GET", "/sandbox")[1]["real_daemon"] is True
    assert not path.with_name(path.name + ".maf-hidden").exists()


def test_bind_failure_restores_the_daemon_socket(daemon, monkeypatch):
    path, requests, identity = daemon

    def fail_bind(self):
        raise OSError("bind failed")

    monkeypatch.setattr(socketserver.TCPServer, "server_bind", fail_bind)
    with pytest.raises(OSError, match="bind failed"), engine_fault_responses(path):
        pytest.fail("must not enter the fault context")
    assert path.stat().st_ino == identity
    assert request(path, "GET", "/sandbox")[1]["real_daemon"] is True


@pytest.mark.parametrize("symlink", [False, True])
def test_a_stale_backup_is_never_overwritten(daemon, symlink):
    path, requests, identity = daemon
    hidden = path.with_name(path.name + ".maf-hidden")
    if symlink:
        hidden.symlink_to(path.with_name("missing"))
    else:
        hidden.write_text("keep")
    with pytest.raises(AssertionError, match="refusing to replace"), engine_fault_responses(path):
        pytest.fail("must not enter the fault context")
    if symlink:
        assert hidden.is_symlink()
    else:
        assert hidden.read_text() == "keep"
    assert path.stat().st_ino == identity
