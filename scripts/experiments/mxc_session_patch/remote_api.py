"""Bounded experimental coordinator API over mutually authenticated TLS."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import socket
import socketserver
import sqlite3
import ssl
import struct
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .host_store import Refused, _lock, _name, _relative
from .private_root import check_file, prepare
from .remote_authority import Authority, Owner, Resource
from .shared_store import Limits, SharedStore

MAX_FRAME = 8 * 1024**2
MAX_PAYLOAD = 4 * 1024**2
MAX_REQUEST = 64 * 1024
TIMEOUT = 10.0
OPERATIONS = frozenset({"acquire", "begin", "publish", "retry", "restore"})


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("duplicate JSON field")
        result[key] = value
    return result


def _load(value: bytes) -> dict:
    try:
        result = json.loads(value, object_pairs_hook=_object)
        if not isinstance(result, dict):
            raise Refused("API record must be an object")
        return result
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refused("invalid API record") from error


def _fields(value: dict, names: set[str]) -> None:
    if set(value) != names:
        raise Refused("unexpected API fields")


def _text(value: object) -> str:
    if type(value) is not str:
        raise Refused("API field must be text")
    return value


def _bytes(value: object, limit: int) -> bytes:
    text = _text(value)
    if len(text) > 4 * ((limit + 2) // 3):
        raise Refused("payload exceeds limit")
    try:
        result = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as error:
        raise Refused("invalid payload encoding") from error
    if len(result) > limit:
        raise Refused("payload exceeds limit")
    return result


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


@dataclass(frozen=True)
class Grant:
    """Trusted certificate mapping; boot identity is configuration, not worker input."""

    host: str
    boot: str
    sessions: frozenset[str]
    operations: frozenset[str] = OPERATIONS

    def __post_init__(self) -> None:
        _name(self.host)
        _name(self.boot)
        if not self.sessions or not self.operations or not self.operations <= OPERATIONS:
            raise Refused("invalid certificate permissions")
        for session in self.sessions:
            _name(session)


@dataclass(frozen=True)
class Session:
    """Coordinator-configured profile and limits for the bounded transfer experiment."""

    profile: Mapping[str, str]
    limits: Limits

    def __post_init__(self) -> None:
        if (
            not self.profile
            or any(type(k) is not str or type(v) is not str for k, v in self.profile.items())
            or len(_json(dict(self.profile))) > 4096
            or self.limits.checkpoint_bytes + self.limits.result_bytes > MAX_PAYLOAD
        ):
            raise Refused("session exceeds the bounded API profile")

    @property
    def digest(self) -> str:
        """Bind ownership to the coordinator's runtime and policy profile."""
        return hashlib.sha256(_json(dict(self.profile))).hexdigest()


class Coordinator:
    """Own local publication handles; callers must supply a verified certificate fingerprint."""

    def __init__(
        self,
        root: Path,
        grants: Mapping[str, Grant],
        sessions: Mapping[str, Session],
        *,
        enabled: bool = False,
        boundary: Callable[[str], None] = lambda _: None,
    ):
        if enabled is not True:
            raise Refused("remote coordinator requires explicit opt-in")
        if not grants or not sessions or len(grants) > 128 or len(sessions) > 128:
            raise Refused("invalid coordinator configuration size")
        if any(not re.fullmatch(r"[0-9a-f]{64}", key) for key in grants):
            raise Refused("certificate fingerprint must be SHA-256")
        for name in sessions:
            _name(name)
        if any(not grant.sessions <= sessions.keys() for grant in grants.values()):
            raise Refused("certificate names an unconfigured session")
        self.grants = {
            k: Grant(v.host, v.boot, frozenset(v.sessions), frozenset(v.operations))
            for k, v in grants.items()
        }
        self.sessions = {k: Session(dict(v.profile), v.limits) for k, v in sessions.items()}
        self.boundary = boundary
        self.stores: dict[str, tuple[Owner, SharedStore]] = {}
        prepare(root)
        self.root = root.resolve()
        check_file(self.root / "service.lock")
        self.lock = (self.root / "service.lock").open("a+b")
        try:
            if self.lock.seek(0, 2) == 0:
                self.lock.write(b"0")
                self.lock.flush()
            _lock(self.lock, True)
            with self.authority():
                pass
            self._clear_transfer()
        except BaseException:
            self.lock.close()
            raise

    def authority(self) -> Authority:
        """Open only the coordinator-local ownership journal."""
        return Authority(self.root / "ownership", enabled=True)

    def close(self) -> None:
        """Release local handles without refunding interrupted reservations."""
        try:
            for _, store in self.stores.values():
                store.close()
            self.stores.clear()
        finally:
            self.lock.close()

    def _clear_transfer(self) -> None:
        path = self.root / "transfer"
        if not path.exists() and not path.is_symlink():
            return
        prepare(path)
        entries = []
        total = 0
        for entry in path.iterdir():
            if len(_relative(entry.name).parts) != 1:
                raise Refused("invalid transfer path")
            check_file(entry)
            total += entry.stat().st_size
            entries.append(entry)
            if total > MAX_PAYLOAD or len(entries) > 128:
                raise Refused("transfer cleanup exceeds bounds")
        for entry in entries:
            entry.unlink()
        path.rmdir()

    @contextmanager
    def _transfer(self) -> Iterator[Path]:
        path = self.root / "transfer"
        if path.exists():
            raise Refused("transfer cleanup is pending")
        prepare(path)
        try:
            yield path
        finally:
            self._clear_transfer()

    def _store(self, owner: Owner) -> SharedStore:
        cached = self.stores.get(owner.session)
        if cached is not None and cached[0] == owner:
            return cached[1]
        if cached is not None:
            cached[1].close()
            del self.stores[owner.session]
        config = self.sessions[owner.session]
        store = SharedStore(
            self.root / "publication", owner.session, dict(config.profile), config.limits
        )
        self.stores[owner.session] = owner, store
        return store

    def dispatch(self, fingerprint: str, request: dict) -> dict:
        """Authorize a verified TLS peer before opening any session store."""
        if self.lock.closed:
            raise Refused("coordinator is closed")
        _fields(request, {"version", "operation", "session", "arguments"})
        if type(request["version"]) is not int or request["version"] != 1:
            raise Refused("unsupported API version")
        operation = _text(request["operation"])
        session = _name(_text(request["session"]))
        grant = self.grants.get(fingerprint)
        if grant is None or session not in grant.sessions or operation not in grant.operations:
            raise Refused("certificate is not authorized")
        args = request["arguments"]
        if not isinstance(args, dict):
            raise Refused("API arguments must be an object")
        config = self.sessions[session]
        with self.authority() as authority:
            if operation == "acquire":
                _fields(args, {"guest"})
                resource = Resource(grant.host, grant.boot, _name(_text(args["guest"])))
                owner = authority.acquire(session, config.digest, resource)
                with authority.guard(owner):
                    self._store(owner)
                return {"owner": authority._encode(owner)}
            expected = {"owner"}
            if operation in ("begin", "retry", "publish"):
                expected |= {"call", "request"}
            if operation == "publish":
                expected |= {"files", "result"}
            _fields(args, expected)
            owner = authority._decode_owner(_text(args["owner"]))
            if (
                owner.session != session
                or owner.profile != config.digest
                or (owner.resource.host, owner.resource.boot) != (grant.host, grant.boot)
            ):
                raise Refused("owner does not belong to this certificate scope")
            with authority.guard(owner):
                store = self._store(owner)
            if operation in ("begin", "retry"):
                call = _name(_text(args["call"]))
                code = _bytes(args["request"], MAX_REQUEST)
                with authority.guard(owner):
                    if (
                        operation == "retry"
                        and store.db.execute(
                            "SELECT 1 FROM calls WHERE session=? AND id=?", (session, call)
                        ).fetchone()
                        is None
                    ):
                        raise Refused("unknown call identity")
                    result = store.begin(call, code)
                return {"result": None if result is None else _encoded(result)}
            if operation == "publish":
                call = _name(_text(args["call"]))
                code = _bytes(args["request"], MAX_REQUEST)
                row = store.db.execute(
                    "SELECT request FROM calls WHERE session=? AND id=?", (session, call)
                ).fetchone()
                if row is None or row[0] != hashlib.sha256(code).hexdigest():
                    raise Refused("call request identity differs")
                files = args["files"]
                if not isinstance(files, dict) or not files or len(files) > config.limits.files:
                    raise Refused("invalid checkpoint inventory")
                result = _bytes(args["result"], config.limits.result_bytes)
                with self._transfer() as path:
                    total = 0
                    for name, value in files.items():
                        relative = _relative(_text(name))
                        if len(relative.parts) != 1:
                            raise Refused("bounded API requires flat checkpoint files")
                        data = _bytes(value, config.limits.checkpoint_bytes - total)
                        total += len(data)
                        with (path / relative.name).open("xb") as stream:
                            stream.write(data)
                    store.commit(
                        call,
                        path,
                        result,
                        self.boundary,
                        commit_guard=lambda: authority.guard(owner),
                    )
                return {"committed": True}
            if operation == "restore":
                with self._transfer() as path:
                    # SharedStore creates its destination; the exclusive service owns this root.
                    path.rmdir()
                    call = store.restore(path)
                    files = (
                        {p.name: _encoded(p.read_bytes()) for p in path.iterdir()} if call else {}
                    )
                    with authority.guard(owner):
                        return {"call": call, "files": files}
            raise Refused("unsupported API operation")


def server_context(certificate: Path, key: Path, ca: Path) -> ssl.SSLContext:
    """Require a verified client certificate; fingerprints further restrict authorization."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(ca))
    context.load_cert_chain(str(certificate), str(key))
    return context


def client_context(certificate: Path, key: Path, ca: Path) -> ssl.SSLContext:
    """Verify coordinator chain and hostname and present the execution-host certificate."""
    context = ssl.create_default_context(cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate), str(key))
    return context


def _receive(connection: ssl.SSLSocket, count: int, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("API receive deadline elapsed")
        connection.settimeout(remaining)
        block = connection.recv(min(65536, count - len(result)))
        if not block:
            raise Refused("incomplete API frame")
        result.extend(block)
    return bytes(result)


def _read(connection: ssl.SSLSocket) -> dict:
    deadline = time.monotonic() + TIMEOUT
    size = struct.unpack("!I", _receive(connection, 4, deadline))[0]
    if not 0 < size <= MAX_FRAME:
        raise Refused("API frame exceeds limit")
    return _load(_receive(connection, size, deadline))


def _send(connection: ssl.SSLSocket, value: dict) -> None:
    payload = _json(value)
    if len(payload) > MAX_FRAME:
        raise Refused("API frame exceeds limit")
    connection.settimeout(TIMEOUT)
    connection.sendall(struct.pack("!I", len(payload)) + payload)


class Server(socketserver.TCPServer):
    """Single-request TLS connections; serialized, bounded experimental service."""

    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: ssl.SSLContext, coordinator: Coordinator):
        if (
            context.verify_mode != ssl.CERT_REQUIRED
            or context.minimum_version < ssl.TLSVersion.TLSv1_2
        ):
            raise Refused("server requires verified mutual TLS")
        self.context = context
        self.coordinator = coordinator
        super().__init__(address, _Handler)

    def get_request(self) -> tuple[ssl.SSLSocket, object]:
        connection, address = self.socket.accept()
        try:
            connection.settimeout(TIMEOUT)
            return self.context.wrap_socket(connection, server_side=True), address
        except BaseException:
            connection.close()
            raise

    def handle_error(self, request: object, client_address: object) -> None:
        """Keep worker-supplied records and coordinator internals out of transport logs."""


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection: ssl.SSLSocket = self.request
        server = cast(Server, self.server)
        try:
            certificate = connection.getpeercert(binary_form=True)
            if not certificate:
                raise Refused("client certificate required")
            fingerprint = hashlib.sha256(certificate).hexdigest()
            if fingerprint not in server.coordinator.grants:
                raise Refused("certificate is not authorized")
            response = server.coordinator.dispatch(fingerprint, _read(connection))
            _send(connection, {"ok": True, "value": response})
        except Refused as error:
            _send(connection, {"ok": False, "error": str(error)})
        except (OSError, sqlite3.Error):
            _send(
                connection, {"ok": False, "error": "coordinator unavailable; resolve call identity"}
            )


class Client:
    """No automatic retries: a transport failure may follow a committed publication."""

    def __init__(self, address: tuple[str, int], context: ssl.SSLContext, hostname: str):
        if (
            not context.check_hostname
            or context.verify_mode != ssl.CERT_REQUIRED
            or context.minimum_version < ssl.TLSVersion.TLSv1_2
        ):
            raise Refused("client requires verified coordinator TLS")
        self.address = address
        self.context = context
        self.hostname = hostname

    def call(self, operation: str, session: str, **arguments: object) -> dict:
        """Send one bounded request; resolve ambiguous writes with the original call identity."""
        try:
            with socket.create_connection(self.address, timeout=TIMEOUT) as raw:
                with self.context.wrap_socket(raw, server_hostname=self.hostname) as connection:
                    _send(
                        connection,
                        {
                            "version": 1,
                            "operation": operation,
                            "session": session,
                            "arguments": arguments,
                        },
                    )
                    response = _read(connection)
            if response.get("ok") is not True:
                raise Refused(_text(response.get("error")))
            _fields(response, {"ok", "value"})
            if not isinstance(response["value"], dict):
                raise Refused("invalid coordinator response")
            return response["value"]
        except (OSError, ValueError) as error:
            raise Refused("coordinator unavailable; resolve call identity") from error
