"""Observe an owned HTTPS response and one same-socket follow-up across a profile description edit."""

from __future__ import annotations

import asyncio
import http.client
import json
import re
import ssl
import time
import uuid
from urllib.parse import urlsplit

from openshell_egress_probe import origin_get


class GuestHTTPS:
    """Keep the primed TLS socket for one follow-up from the same guest."""

    def __init__(self):
        self.idle_connection: http.client.HTTPSConnection | None = None
        self.idle_socket = None
        self.idle_target = None

    def get(self, operation, url, headers):
        target = urlsplit(url)
        if target.scheme != "https" or target.hostname is None or target.port is None:
            raise ValueError("expected the owned HTTPS origin")
        if operation == "reuse":
            connection = self.idle_connection
            if connection is None or self.idle_target != (target.netloc, target.path):
                raise ValueError("transport target changed")
            if connection.sock is None:
                return {"closed_before_request": True}
            if connection.sock is not self.idle_socket:
                raise RuntimeError("guest TLS socket replaced")
        elif operation in ("prime", "hold"):
            connection = http.client.HTTPSConnection(target.hostname, target.port, timeout=30)
            connection.connect()
            connection.auto_open = 0
            if operation == "prime":
                self.idle_connection, self.idle_socket = connection, connection.sock
                self.idle_target = (target.netloc, target.path)
        else:
            raise ValueError("unknown transport operation")
        try:
            connection.request("GET", target.path + "?" + target.query, headers=headers)
            with connection.getresponse() as response:
                expected = response.length
                body = response.read(4097)
                if len(body) > 4096:
                    connection.close()
                    raise ValueError("transport response exceeds 4096 bytes")
                if expected is not None and len(body) < expected:
                    raise http.client.IncompleteRead(body, expected - len(body))
                result = {"status": response.status, "body": body.decode()}
            if operation == "prime":
                result["kept_open"] = (
                    connection.sock is self.idle_socket and self.idle_socket is not None
                )
            return result
        except (
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            ConnectionResetError,
            BrokenPipeError,
            ssl.SSLEOFError,
        ) as exc:
            connection.close()
            return {"closed_during_request": type(exc).__name__}
        finally:
            if operation != "prime":
                connection.close()


def attributed_event(events, expected):
    """Match one HTTPS receipt including the connection assigned at the origin."""
    if len(events) != 1:
        raise RuntimeError("expected one TLS transport origin event")
    event = events[0]
    connection = event.get("origin_connection")
    if not isinstance(connection, str) or not re.fullmatch("[0-9a-f]{32}", connection):
        raise RuntimeError("missing origin TLS connection identity")
    if event != {**expected, "origin_connection": connection}:
        raise RuntimeError("TLS transport origin attribution mismatch")
    return event


def transport_outcome(observed, event, *, followup):
    """Distinguish an attributed response, an already closed socket and an interrupted request."""
    if (
        observed.get("status") == 200
        and event is not None
        and json.loads(observed["body"]) == event
    ):
        return "response_received"
    if observed == {"closed_before_request": True} and followup and event is None:
        return "socket_closed_before_request"
    if observed.get("closed_during_request") in (
        "RemoteDisconnected",
        "IncompleteRead",
        "ConnectionResetError",
        "BrokenPipeError",
        "SSLEOFError",
    ) and set(observed) == {"closed_during_request"}:
        return (
            "closed_after_origin_receipt" if event is not None else "closed_without_origin_receipt"
        )
    raise RuntimeError("unattributed TLS transport response")


async def measure_transport(guest, profile, client, mint, ip, host, reconcile, record):
    """Bracket a stored profile description change with a held origin response."""
    marker = uuid.uuid4().hex
    path = profile.endpoint_path + "?case=" + marker + "&transport="
    base = {
        "method": "GET",
        "host": host + ":8443",
        "port": 8443,
        "authorization_sha256": mint["authorization_sha256"],
    }
    url = "https://" + host + ":8443" + path
    row = {"case": marker, "path": profile.endpoint_path, "complete": False}
    record("transport", row)
    await reconcile(expected=[])
    await guest.command(operation="prime", url=url + "prime", source="edited")
    primed = await guest.request_result("edited")
    idle = attributed_event(await reconcile(), {**base, "path": path + "prime"})
    if (
        transport_outcome(primed["guest"], idle, followup=False) != "response_received"
        or type(primed["guest"].get("kept_open")) is not bool
    ):
        raise RuntimeError("idle HTTPS connection did not return its control response")
    row.update(idle_guest=primed, idle_origin=idle)
    try:
        await guest.command(operation="hold", url=url + "held", source="edited")
        deadline = time.monotonic() + 10
        while True:
            arrivals = await reconcile()
            if arrivals:
                held = attributed_event(arrivals, {**base, "path": path + "held"})
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("TLS origin did not receive the held request")
            await asyncio.sleep(0.1)
        row["held_origin"] = held
        row["receipt_observed_ns"] = time.monotonic_ns()
        await asyncio.to_thread(profile.update_description, client, record)
        row["exact_readback_ns"] = time.monotonic_ns()
    finally:
        release = await asyncio.to_thread(
            origin_get, ip, "/__release_tls?case=" + marker, method="POST"
        )
        row["origin_released"] = release == {"released": True}
    if not row["origin_released"]:
        raise RuntimeError("TLS response was no longer held at profile readback")
    first = await guest.request_result("edited")
    row["held_guest"] = first
    row["in_flight"] = transport_outcome(first["guest"], held, followup=False)
    await reconcile(expected=[])
    await guest.command(operation="reuse", url=url + "reuse", source="edited")
    second = await guest.request_result("edited")
    row["followup_guest"] = second
    arrivals = await reconcile()
    event = attributed_event(arrivals, {**base, "path": path + "reuse"}) if arrivals else None
    row["followup_origin"] = event
    row["followup"] = transport_outcome(second["guest"], event, followup=True)
    row["same_origin_connection"] = (
        event["origin_connection"] == idle["origin_connection"] if event is not None else None
    )
    if not primed["guest"]["kept_open"] and row["followup"] != "socket_closed_before_request":
        raise RuntimeError("closed idle TLS connection was replaced")
    row["complete"] = True
