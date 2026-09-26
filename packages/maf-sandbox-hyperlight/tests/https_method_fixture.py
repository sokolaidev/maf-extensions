"""A bounded recording origin and temporary public TLS relay for live method probes."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

CLOUDFLARED_VERSION = "2026.9.3"
CLOUDFLARED_SHA256 = {
    "win32": "f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2",
    "linux": "77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2",
}
MAX_BODY = 64 * 1024


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    body: bytes
    transfer_encoding: str | None
    content_length: str | None


def read_body(handler: BaseHTTPRequestHandler) -> bytes:
    """Decode content-length and chunked request bodies with bounded size and framing."""
    encodings = handler.headers.get_all("Transfer-Encoding", [])
    lengths = handler.headers.get_all("Content-Length", [])
    if len(encodings) > 1 or len(lengths) > 1 or (encodings and lengths):
        raise ValueError("ambiguous framing")
    if encodings:
        if encodings[0].lower() != "chunked":
            raise ValueError("unsupported transfer encoding")
        result = bytearray()
        while True:
            line = handler.rfile.readline(1025)
            if len(line) > 1024 or not line.endswith(b"\r\n"):
                raise ValueError("invalid chunk line")
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size < 0 or size > MAX_BODY - len(result):
                raise ValueError("body exceeds limit")
            if not size:
                # Trailer size is bounded independently of the body.
                remaining = 4096
                while remaining > 0:
                    trailer = handler.rfile.readline(remaining + 1)
                    remaining -= len(trailer)
                    if trailer == b"\r\n":
                        return bytes(result)
                    if not trailer.endswith(b"\r\n"):
                        break
                raise ValueError("invalid trailers")
            chunk = handler.rfile.read(size)
            if len(chunk) != size or handler.rfile.read(2) != b"\r\n":
                raise ValueError("incomplete chunk")
            result.extend(chunk)
    size = int(lengths[0]) if lengths else 0
    if not 0 <= size <= MAX_BODY:
        raise ValueError("body exceeds limit")
    result = handler.rfile.read(size)
    if len(result) != size:
        raise ValueError("incomplete body")
    return result


class RecordingOrigin:
    def __init__(self) -> None:
        self.prefix = "/" + uuid.uuid4().hex + "/"
        self.proof = uuid.uuid4().hex.encode("ascii")
        self.records: list[RecordedRequest] = []
        self.lock = threading.Lock()
        origin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(5)

            def do_GET(self) -> None:
                self.close_connection = True
                if not self.path.startswith(origin.prefix):
                    self.send_error(404)
                    return
                try:
                    body = read_body(self)
                except (ValueError, OSError):
                    self.send_error(400)
                    return
                if self.path != origin.prefix + "ready":
                    with origin.lock:
                        if len(origin.records) >= 256:
                            self.send_error(429)
                            return
                        origin.records.append(
                            RecordedRequest(
                                self.command,
                                self.path,
                                body,
                                self.headers.get("Transfer-Encoding"),
                                self.headers.get("Content-Length"),
                            )
                        )
                self.send_response(200)
                self.send_header("Content-Length", str(len(origin.proof)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(origin.proof)

            do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def matching(self, path: str) -> list[RecordedRequest]:
        with self.lock:
            return [record for record in self.records if record.path == path]

    def __enter__(self) -> RecordingOrigin:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=6)
        assert not self.thread.is_alive(), "recording origin did not stop"


@contextmanager
def public_origin(binary: Path, directory: Path) -> Iterator[tuple[RecordingOrigin, str]]:
    """Expose only the random-path fixture through a pinned, temporary HTTPS tunnel."""
    with binary.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != CLOUDFLARED_SHA256[sys.platform]:
        raise ValueError("cloudflared must match the pinned release digest")
    config = directory / "cloudflared.yml"
    config.write_text("{}\n", encoding="ascii")
    log = directory / "cloudflared.log"
    with RecordingOrigin() as origin, log.open("wb") as output:
        process = subprocess.Popen(
            [
                str(binary),
                "tunnel",
                "--config",
                str(config),
                "--no-autoupdate",
                "--protocol",
                "http2",
                "--url",
                origin.local_url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            deadline = time.monotonic() + 120
            last_error = "no public endpoint announced"
            opener = build_opener(ProxyHandler({}))
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        "HTTPS tunnel exited: " + log.read_text(errors="replace")[-4000:]
                    )
                match = re.search(
                    r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text(errors="replace")
                )
                if match:
                    url = match.group()
                    try:
                        with opener.open(url + origin.prefix + "ready", timeout=3) as response:
                            ready = response.status == 200 and response.read(128) == origin.proof
                    except (URLError, OSError) as error:
                        last_error = str(error)
                        ready = False
                    if ready:
                        break
                time.sleep(0.2)
            else:
                raise TimeoutError(f"HTTPS recording origin did not become reachable: {last_error}")
            yield origin, url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.poll() is not None, "HTTPS tunnel was not reaped"
