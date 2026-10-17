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
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast

from . import remote_scratch
from . import remote_transfer as transfer
from .host_store import MAX_CHECKPOINT, MAX_FILES, Refused, _lock, _name, _relative
from .private_root import check_file, prepare
from .remote_authority import Authority, Fence, Owner, Policy, Resource
from .shared_store import Limits, SharedStore

MAX_FRAME = 8 * 1024**2
MAX_PAYLOAD = 4 * 1024**2
MAX_REQUEST = 64 * 1024
TIMEOUT = 10.0
TRANSFER_TIMEOUT = 300.0
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
            or self.limits.result_bytes > MAX_PAYLOAD
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
        scratch_policy: remote_scratch.Policy | None = None,
        fencing_policy: Policy = Policy(),
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
        self.scratch_policy = scratch_policy
        self.fencing_policy = fencing_policy
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
        return Authority(
            self.root / "ownership",
            enabled=True,
            scratch_policy=self.scratch_policy,
            policy=self.fencing_policy,
        )

    def fence(
        self,
        owner: Owner,
        request_id: str,
        verify: Callable[[Fence], bool],
        *,
        automatic: bool = False,
    ) -> Fence:
        """Fence on the service owner thread using a trusted deployment verifier, never worker input."""
        if self.lock.closed:
            raise Refused("coordinator is closed")
        with self.authority() as authority:
            request = authority.request_fence(owner, request_id, automatic=automatic)
            authority.confirm_fence(request_id, verify)
            return request

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
        transfer.clear(path)

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

    @contextmanager
    def _publication_guard(
        self, authority: Authority, owner: Owner, call: str, code: bytes
    ) -> Iterator[None]:
        with authority.guard(owner):
            if self.scratch_policy:
                remote_scratch.find(authority, owner, call, code)
            yield

    def pending_scratch(self) -> list[remote_scratch.Receipt]:
        """List bounded controller work; receipts do not authorize execution or cleanup."""
        if self.lock.closed or self.scratch_policy is None:
            raise Refused("remote scratch is not available")
        with self.authority() as authority, authority._transaction():
            return [
                remote_scratch.receipt(authority, row)
                for row in authority.db.execute(
                    "SELECT * FROM remote_scratch WHERE state!='released' ORDER BY token"
                )
            ]

    def reconcile_scratch(
        self, receipt: remote_scratch.Receipt, verify: Callable[[remote_scratch.Receipt], bool]
    ) -> None:
        """Release only after trusted proof of helper termination, scratch removal and no reuse.

        The deployment verifier binds evidence to the entire receipt and runs outside SQLite.
        Like dispatch, controllers must call this on the serialized service owner thread.
        """
        if self.lock.closed or self.scratch_policy is None:
            raise Refused("remote scratch is not available")
        with self.authority() as authority:

            def row():
                found = authority.db.execute(
                    "SELECT * FROM remote_scratch WHERE token=?", (receipt.token,)
                ).fetchone()
                if remote_scratch.receipt(authority, found) != receipt:
                    raise Refused("remote scratch receipt differs")
                return found

            with authority._transaction():
                current = row()
                if current["state"] == "released":
                    return
                authority.db.execute(
                    "UPDATE remote_scratch SET state='cleaning' WHERE token=?", (receipt.token,)
                )
            self.boundary("after_remote_cleanup_intent")
            if verify(receipt) is not True:
                raise Refused("remote termination and scratch removal are not confirmed")
            self.boundary("after_remote_cleanup_verification")
            owner = authority._decode_owner(receipt.owner)
            cached = self.stores.get(owner.session)
            store = cached[1] if cached is not None else self._store(owner)
            with authority._transaction():
                if row()["state"] != "cleaning":
                    raise Refused("remote scratch cleanup state changed")
                with store._transaction():
                    store._owner()
                    call = store.db.execute(
                        "SELECT request FROM calls WHERE session=? AND id=?",
                        (owner.session, receipt.call),
                    ).fetchone()
                    if call is not None and call["request"] != receipt.request:
                        raise Refused("remote cleanup request differs")
                    store.db.execute(
                        "UPDATE calls SET status='interrupted' WHERE session=? AND id=? AND status='pending'",
                        (owner.session, receipt.call),
                    )
                    store.db.execute(
                        "DELETE FROM reservations WHERE session=? AND call=?",
                        (owner.session, receipt.call),
                    )
                    self.boundary("before_remote_reservation_release")
                self.boundary("after_remote_reservation_release")
                authority.db.execute(
                    "UPDATE remote_scratch SET state='released' WHERE token=?", (receipt.token,)
                )
                self.boundary("before_remote_scratch_release")
            self.boundary("after_remote_scratch_release")

    def dispatch(
        self, fingerprint: str, request: dict, connection: ssl.SSLSocket | None = None
    ) -> dict:
        """Authorize a verified TLS peer before opening any session store."""
        if self.lock.closed:
            raise Refused("coordinator is closed")
        _fields(request, {"version", "operation", "session", "arguments"})
        if type(request["version"]) is not int or request["version"] not in (1, 2):
            raise Refused("unsupported API version")
        streaming = request["version"] == 2
        operation = _text(request["operation"])
        if streaming and (connection is None or operation not in {"publish", "restore"}):
            raise Refused("version 2 requires checkpoint streaming")
        deadline = time.monotonic() + TRANSFER_TIMEOUT
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
            if (
                not streaming
                and operation in {"publish", "restore"}
                and (config.limits.checkpoint_bytes + config.limits.result_bytes > MAX_PAYLOAD)
            ):
                raise Refused("session requires streaming checkpoint transfer")
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
                scratch = None
                if operation == "begin" and self.scratch_policy:
                    existing = store.db.execute(
                        "SELECT status FROM calls WHERE session=? AND id=?", (session, call)
                    ).fetchone()
                    if existing is None:
                        scratch = remote_scratch.reserve(authority, owner, call, code)
                        self.boundary("after_remote_scratch_reserve")
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
                if scratch is not None:
                    self.boundary("after_remote_admission")
                    return {"result": None, "scratch": asdict(scratch)}
                return {"result": None if result is None else _encoded(result)}
            if operation == "publish":
                call = _name(_text(args["call"]))
                code = _bytes(args["request"], MAX_REQUEST)
                row = store.db.execute(
                    "SELECT request FROM calls WHERE session=? AND id=?", (session, call)
                ).fetchone()
                if row is None or row[0] != hashlib.sha256(code).hexdigest():
                    raise Refused("call request identity differs")
                with self._publication_guard(authority, owner, call, code):
                    pass
                files = args["files"]
                if streaming:
                    assert connection is not None
                    reserved = store.db.execute(
                        "SELECT c.status,c.generation,r.generation FROM calls c JOIN reservations r ON c.session=r.session AND c.id=r.call WHERE c.session=? AND c.id=?",
                        (session, call),
                    ).fetchone()
                    if reserved is None or tuple(reserved) != (
                        "pending",
                        store.generation,
                        store.generation,
                    ):
                        raise Refused("call is not reserved for this owner")
                    entries = transfer.inventory(
                        files, config.limits.checkpoint_bytes, config.limits.files
                    )
                    result = _bytes(args["result"], config.limits.result_bytes)
                    with self._transfer() as path:
                        destination = path / "checkpoint"
                        _send(connection, {"ok": True, "value": {"ready": True}}, deadline)
                        transfer.receive(
                            destination,
                            entries,
                            lambda count: _receive(connection, count, deadline),
                        )
                        complete = _read(connection, deadline)
                        if complete != {"complete": True} or type(complete["complete"]) is not bool:
                            raise Refused("incomplete checkpoint transfer")
                        store.commit(
                            call,
                            destination,
                            result,
                            self.boundary,
                            commit_guard=lambda: self._publication_guard(
                                authority, owner, call, code
                            ),
                        )
                    return {"committed": True}
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
                        commit_guard=lambda: self._publication_guard(authority, owner, call, code),
                    )
                return {"committed": True}
            if operation == "restore":
                with self._transfer() as path:
                    destination = path / "checkpoint"
                    call = store.restore(destination)
                    if streaming:
                        assert connection is not None
                        entries = (
                            transfer.describe(
                                destination, config.limits.checkpoint_bytes, config.limits.files
                            )
                            if call
                            else []
                        )
                        with authority.guard(owner):
                            pass
                        _send(
                            connection,
                            {"ok": True, "value": {"call": call, "files": entries}},
                            deadline,
                        )
                        if call:
                            transfer.send(
                                destination,
                                entries,
                                lambda block: _write(connection, block, deadline),
                            )
                        with authority.guard(owner):
                            return {"complete": True}
                    files = (
                        {p.name: _encoded(p.read_bytes()) for p in destination.iterdir()}
                        if call
                        else {}
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


def _read(connection: ssl.SSLSocket, deadline: float | None = None) -> dict:
    if deadline is None:
        deadline = time.monotonic() + TIMEOUT
    size = struct.unpack("!I", _receive(connection, 4, deadline))[0]
    if not 0 < size <= MAX_FRAME:
        raise Refused("API frame exceeds limit")
    return _load(_receive(connection, size, deadline))


def _send(connection: ssl.SSLSocket, value: dict, deadline: float | None = None) -> None:
    payload = _json(value)
    if len(payload) > MAX_FRAME:
        raise Refused("API frame exceeds limit")
    _write(
        connection,
        struct.pack("!I", len(payload)) + payload,
        deadline or time.monotonic() + TIMEOUT,
    )


def _write(connection: ssl.SSLSocket, block: bytes, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("API transfer deadline elapsed")
    connection.settimeout(remaining)
    connection.sendall(block)


def _reply(connection: ssl.SSLSocket, deadline: float) -> dict:
    response = _read(connection, deadline)
    if response.get("ok") is not True:
        raise Refused(_text(response.get("error")))
    _fields(response, {"ok", "value"})
    if not isinstance(response["value"], dict):
        raise Refused("invalid coordinator response")
    return response["value"]


class Endpoint(Protocol):
    """Authenticated dispatch shared by separate worker and controller listeners."""

    @property
    def grants(self) -> Mapping[str, object]: ...

    def dispatch(
        self, fingerprint: str, request: dict, connection: ssl.SSLSocket | None = None
    ) -> dict: ...


class Server(socketserver.TCPServer):
    """Single-request TLS connections; serialized, bounded experimental service."""

    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: ssl.SSLContext, coordinator: Endpoint):
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
            response = server.coordinator.dispatch(fingerprint, _read(connection), connection)
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

    @contextmanager
    def _stream(
        self, operation: str, session: str, arguments: dict
    ) -> Iterator[tuple[ssl.SSLSocket, float]]:
        try:
            deadline = time.monotonic() + TRANSFER_TIMEOUT
            with socket.create_connection(self.address, timeout=TIMEOUT) as raw:
                with self.context.wrap_socket(raw, server_hostname=self.hostname) as connection:
                    _send(
                        connection,
                        dict(version=2, operation=operation, session=session, arguments=arguments),
                        deadline,
                    )
                    yield connection, deadline
        except (OSError, ValueError) as error:
            raise Refused("coordinator unavailable; resolve call identity") from error

    def publish(
        self, session: str, owner: str, call: str, request: bytes, checkpoint: Path, result: bytes
    ) -> None:
        """Stream a stable checkpoint after begin; never retry an ambiguous publication automatically."""
        if len(request) > MAX_REQUEST or len(result) > MAX_PAYLOAD:
            raise Refused("payload exceeds limit")
        entries = transfer.describe(checkpoint, MAX_CHECKPOINT, MAX_FILES)
        arguments = dict(
            owner=owner,
            call=call,
            request=_encoded(request),
            files=entries,
            result=_encoded(result),
        )
        with self._stream("publish", session, arguments) as (connection, deadline):
            if _reply(connection, deadline) != {"ready": True}:
                raise Refused("invalid coordinator readiness")
            transfer.send(checkpoint, entries, lambda block: _write(connection, block, deadline))
            _send(connection, {"complete": True}, deadline)
            if _reply(connection, deadline) != {"committed": True}:
                raise Refused("invalid publication acknowledgment")

    def restore(
        self,
        session: str,
        owner: str,
        destination: Path,
        *,
        checkpoint_bytes: int = MAX_CHECKPOINT,
        files: int = MAX_FILES,
    ) -> str | None:
        """Expose a verified checkpoint only after the coordinator's final ownership check."""
        if (
            type(checkpoint_bytes) is not int
            or not 0 < checkpoint_bytes <= MAX_CHECKPOINT
            or type(files) is not int
            or not 0 < files <= MAX_FILES
        ):
            raise Refused("invalid download limits")
        if destination.exists() or destination.is_symlink():
            raise Refused("restore destination already exists")
        with tempfile.TemporaryDirectory(
            prefix="mxc-transfer-", dir=destination.parent
        ) as temporary:
            root = Path(temporary)
            prepare(root)
            staged = root / "checkpoint"
            with self._stream("restore", session, {"owner": owner}) as (connection, deadline):
                response = _reply(connection, deadline)
                _fields(response, {"call", "files"})
                call = response["call"]
                if call is None:
                    if response["files"] != []:
                        raise Refused("unexpected checkpoint without call")
                else:
                    call = _name(_text(call))
                    entries = transfer.inventory(response["files"], checkpoint_bytes, files)
                    transfer.receive(
                        staged, entries, lambda count: _receive(connection, count, deadline)
                    )
                if _reply(connection, deadline) != {"complete": True}:
                    raise Refused("incomplete checkpoint transfer")
            if call is not None:
                if destination.exists() or destination.is_symlink():
                    raise Refused("restore destination already exists")
                staged.rename(destination)
            return call
