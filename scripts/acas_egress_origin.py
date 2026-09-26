"""Recording HTTP origin for the opt-in ACAS method-policy probe."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class Origin(BaseHTTPRequestHandler):
    """Accept arbitrary literal methods and retain only synthetic probe receipts."""

    events: list[dict[str, str]] = []
    lock = threading.Lock()

    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self.respond
        raise AttributeError(name)

    def log_message(self, format: str, *args: object) -> None:
        """Keep request URLs out of container logs."""

    def respond(self) -> None:
        """Serve receipts to the host and record probe requests before redirecting."""
        parsed = urlsplit(self.path)
        if parsed.path == "/receipts":
            if self.headers.get("Authorization") != "Bearer " + os.environ["PROBE_TOKEN"]:
                self.send_error(403)
                return
            with self.lock:
                payload = json.dumps(self.events).encode()
            self.send_response(200)
        else:
            query = parse_qs(parsed.query)
            with self.lock:
                self.events.append({"id": query.get("id", [""])[0], "method": self.command})
            payload = b"probe origin\n"
            if parsed.path.startswith("/redirect/"):
                self.send_response(int(parsed.path.rsplit("/", 1)[1]))
                self.send_header("Location", query["to"][0])
            else:
                self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Origin).serve_forever()
