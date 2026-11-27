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
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from maf_sandbox_mxc._records import is_record

from . import freshness, remote_scratch
from . import remote_transfer as transfer
from .files.request import FileLimits
from .files.transport import file_result_limit
from .host_store import (
    CHUNK,
    MAX_FILES,
    _lock,  # pyright: ignore[reportPrivateUsage]
    _name,  # pyright: ignore[reportPrivateUsage]
    _relative,  # pyright: ignore[reportPrivateUsage]
)
from .host_store import (  # pyright: ignore[reportPrivateUsage]
    MAX_CHECKPOINT as MAX_CHECKPOINT,
)
from .host_store import (
    Refused as Refused,
)
from .private_root import check_file, prepare
from .remote_authority import Authority, Fence, Owner, Policy, Resource
from .shared_store import MAX_SHARED_RESULT, SharedStore
from .shared_store import Limits as Limits

MAX_FRAME = 8 * 1024**2
MAX_PAYLOAD = 4 * 1024**2
MAX_REQUEST = 64 * 1024
TIMEOUT = 10.0
TRANSFER_TIMEOUT = 300.0
OPERATIONS = frozenset({"acquire", "begin", "publish", "retry", "restore"})


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Refused("duplicate JSON field")
        result[key] = value
    return result


def _load(value: bytes) -> dict[str, Any]:
    try:
        result: dict[str, Any] = json.loads(value, object_pairs_hook=_object)
        if not is_record(result):
            raise Refused("API record must be an object")
        return result
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refused("invalid API record") from error


def _fields(value: dict[str, Any], names: set[str]) -> None:
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
        if (
            not self.sessions
            or not self.operations
            or not self.operations <= OPERATIONS | {"status", "retire"}
        ):
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
        ):
            raise Refused("session exceeds the bounded API profile")

    @classmethod
    def for_files(
        cls, profile: Mapping[str, str], limits: Limits, files: FileLimits = FileLimits()
    ) -> Session:
        """Size retained results for the file channel and bind its policy to ownership."""
        bound_profile = {**profile, "file_limits": _json(asdict(files)).decode("ascii")}
        return cls(bound_profile, replace(limits, result_bytes=file_result_limit(files)))

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
        initialize: bool = False,
        freshness_root: Path | None = None,
        scratch_policy: remote_scratch.Policy | None = None,
        fencing_policy: Policy = Policy(),
        boundary: Callable[[str], None] = lambda _: None,
    ):
        if enabled is not True:
            raise Refused("remote coordinator requires explicit opt-in")
        if type(initialize) is not bool:
            raise Refused("coordinator initialization requires an explicit boolean")
        if not initialize and not root.is_dir():
            raise Refused("existing coordinator root is unavailable")
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
        self.stores: dict[str, tuple[Owner | None, SharedStore]] = {}
        self.witness: freshness.Witness | None = None
        prepare(root)
        self.root = root.resolve()
        check_file(self.root / "service.lock")
        self.lock = (self.root / "service.lock").open("a+b")
        try:
            if self.lock.seek(0, 2) == 0:
                self.lock.write(b"0")
                self.lock.flush()
            _lock(self.lock, True)
            if initialize and any(p.name != "service.lock" for p in self.root.iterdir()):
                raise Refused("coordinator initialization requires an empty root")
            if freshness_root is not None:
                self.witness = freshness.Witness(freshness_root, self.root, initialize=initialize)
            with Authority(
                self.root / "ownership",
                enabled=True,
                scratch_policy=self.scratch_policy,
                policy=self.fencing_policy,
                existing_only=not initialize,
                witness=self.witness,
            ):
                pass
            for name, config in self.sessions.items():
                store = SharedStore(
                    self.root / "publication",
                    name,
                    dict(config.profile),
                    config.limits,
                    existing_only=not initialize,
                    witness=self.witness,
                )
                self.stores[name] = None, store
            if self.witness is not None:
                self.witness.ready()
            self._clear_transfer()
        except BaseException:
            self.close()
            raise

    def authority(self) -> Authority:
        """Open only the coordinator-local ownership journal."""
        return Authority(
            self.root / "ownership",
            enabled=True,
            scratch_policy=self.scratch_policy,
            policy=self.fencing_policy,
            existing_only=True,
            witness=self.witness,
        )

    def fence(
        self,
        owner: Owner,
        request_id: str,
        verify: Callable[[Fence], bool],
        *,
        automatic: bool = False,
    ) -> Fence:
        """Fence on the service owner thread using a trusted deployment verifier."""
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
            if self.witness is not None:
                self.witness.close()
            self.lock.close()

    def _clear_transfer(self) -> None:
        path = self.root / "transfer"
        if not path.exists() and not path.is_symlink():
            return
        prepare(path)
        transfer.clear(path)

    @contextmanager
    def _transfer(self) -> Generator[Path]:
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
        if cached is not None and cached[0] is None:
            self.stores[owner.session] = owner, cached[1]
            return cached[1]
        if cached is not None and cached[0] == owner:
            return cached[1]
        if cached is not None:
            cached[1].close()
            del self.stores[owner.session]
        config = self.sessions[owner.session]
        store = SharedStore(
            self.root / "publication",
            owner.session,
            dict(config.profile),
            config.limits,
            existing_only=True,
            witness=self.witness,
        )
        self.stores[owner.session] = owner, store
        return store

    @contextmanager
    def _publication_guard(
        self, authority: Authority, owner: Owner, call: str, code: bytes
    ) -> Generator[None]:
        with authority.guard(owner):
            if self.scratch_policy:
                remote_scratch.find(authority, owner, call, code)
            yield

    def pending_scratch(self) -> list[remote_scratch.Receipt]:
        """List bounded controller work; receipts do not authorize execution or cleanup."""
        if self.lock.closed or self.scratch_policy is None:
            raise Refused("remote scratch is not available")
        with self.authority() as authority, authority._transaction():  # pyright: ignore[reportPrivateUsage]
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

            with authority._transaction():  # pyright: ignore[reportPrivateUsage]
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
            owner = authority._decode_owner(receipt.owner)  # pyright: ignore[reportPrivateUsage]
            cached = self.stores.get(owner.session)
            store = cached[1] if cached is not None else self._store(owner)
            with authority._transaction():  # pyright: ignore[reportPrivateUsage]
                if row()["state"] != "cleaning":
                    raise Refused("remote scratch cleanup state changed")
                with store._transaction():  # pyright: ignore[reportPrivateUsage]
                    store._owner()  # pyright: ignore[reportPrivateUsage]
                    call = store.db.execute(
                        "SELECT request FROM calls WHERE session=? AND id=?",
                        (owner.session, receipt.call),
                    ).fetchone()
                    if call is not None and call["request"] != receipt.request:
                        raise Refused("remote cleanup request differs")
                    store.db.execute(
                        (
                            "UPDATE calls SET status='interrupted' WHERE session=? AND id=? "
                            "AND status='pending'"
                        ),
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
        self, fingerprint: str, request: dict[str, Any], connection: ssl.SSLSocket | None = None
    ) -> dict[str, Any]:
        """Authorize a verified TLS peer before opening any session store."""
        if self.lock.closed:
            raise Refused("coordinator is closed")
        _fields(request, {"version", "operation", "session", "arguments"})
        if type(request["version"]) is not int or request["version"] not in (1, 2, 3):
            raise Refused("unsupported API version")
        version = request["version"]
        streaming = version >= 2
        operation = _text(request["operation"])
        if version == 3 and (connection is None or operation not in {"publish", "begin", "retry"}):
            raise Refused("version 3 requires streamed result publication or replay")
        if version == 2 and (connection is None or operation not in {"publish", "restore"}):
            raise Refused("version 2 requires checkpoint streaming")
        deadline = time.monotonic() + TRANSFER_TIMEOUT
        session = _name(_text(request["session"]))
        grant = self.grants.get(fingerprint)
        if grant is None or session not in grant.sessions or operation not in grant.operations:
            raise Refused("certificate is not authorized")
        args = request["arguments"]
        if not is_record(args):
            raise Refused("API arguments must be an object")
        config = self.sessions[session]
        with self.authority() as authority:
            if operation == "acquire":
                _fields(
                    args, {"guest", "expected_profile"} if "expected_profile" in args else {"guest"}
                )
                if "expected_profile" in args and _text(args["expected_profile"]) != config.digest:
                    raise Refused("execution host profile differs from the configured session")
                resource = Resource(grant.host, grant.boot, _name(_text(args["guest"])))
                owner = authority.acquire(session, config.digest, resource)
                with authority.guard(owner):
                    self._store(owner)
                return {"owner": authority._encode(owner)}  # pyright: ignore[reportPrivateUsage]
            if (
                version < 3
                and operation in {"begin", "retry", "publish"}
                and config.limits.result_bytes > MAX_PAYLOAD
            ):
                raise Refused("session requires streaming result transfer")
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
            owner = authority._decode_owner(_text(args["owner"]))  # pyright: ignore[reportPrivateUsage]
            if (
                owner.session != session
                or owner.profile != config.digest
                or (owner.resource.host, owner.resource.boot) != (grant.host, grant.boot)
            ):
                raise Refused("owner does not belong to this certificate scope")
            with authority.guard(owner):
                store = self._store(owner)
            if operation in {"status", "retire"}:
                with authority.guard(owner):
                    state = store.db.execute(
                        "SELECT state,current_call FROM sessions WHERE id=?", (session,)
                    ).fetchone()
                    pending = (
                        self.scratch_policy is not None
                        and authority.db.execute(
                            "SELECT 1 FROM remote_scratch WHERE session=? AND state!='released'",
                            (session,),
                        ).fetchone()
                        is not None
                    )
                    if operation == "status":
                        return {
                            "active": state["state"] == "active",
                            "checkpoint": state["current_call"],
                            "pending": pending,
                            "scratch": asdict(self.scratch_policy) if self.scratch_policy else None,
                        }
                    if pending:
                        raise Refused("remote scratch requires trusted reconciliation")
                    store.retire()
                    store.collect_checkpoints(limit=128)
                    return {"retired": True}
            if operation in ("begin", "retry"):
                call = _name(_text(args["call"]))
                code = _bytes(args["request"], MAX_REQUEST)
                scratch = None
                existing = store.db.execute(
                    "SELECT 1 FROM calls WHERE session=? AND id=?", (session, call)
                ).fetchone()
                if existing is None and operation == "begin" and self.scratch_policy:
                    if (
                        store.db.execute(
                            "SELECT state FROM sessions WHERE id=?", (session,)
                        ).fetchone()[0]
                        != "active"
                    ):
                        raise Refused("session is retired")
                    scratch = remote_scratch.reserve(authority, owner, call, code)
                    self.boundary("after_remote_scratch_reserve")
                with authority.guard(owner):
                    if existing is None:
                        if operation == "retry":
                            raise Refused("unknown call identity")
                        store.collect_checkpoints()
                        store.expire()
                    result = store.begin(call, code)
                if scratch is not None:
                    self.boundary("after_remote_admission")
                    return {"result": None, "scratch": asdict(scratch)}
                if result is not None and version == 3:
                    assert connection is not None
                    _send(
                        connection,
                        {"ok": True, "value": {"result": _result_info(result)}},
                        deadline,
                    )
                    _send_result(connection, result, deadline)
                    with authority.guard(owner):
                        return {"complete": True}
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
                        (
                            "SELECT c.status,c.generation,r.generation FROM calls c JOIN "
                            "reservations r ON c.session=r.session AND c.id=r.call WHERE "
                            "c.session=? AND c.id=?"
                        ),
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
                    if version == 3:
                        descriptor = _result_descriptor(args["result"], config.limits.result_bytes)
                        result = b""
                    else:
                        descriptor = None
                        result = _bytes(args["result"], config.limits.result_bytes)
                    with self._transfer() as path:
                        destination = path / "checkpoint"
                        _send(connection, {"ok": True, "value": {"ready": True}}, deadline)
                        transfer.receive(
                            destination,
                            entries,
                            lambda count: _receive(connection, count, deadline),
                        )
                        if descriptor is not None:
                            result = _receive_result(connection, descriptor, deadline)
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
                if not is_record(files) or not files or len(files) > config.limits.files:
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


def _result_info(result: bytes) -> dict[str, Any]:
    return {"size": len(result), "sha256": hashlib.sha256(result).hexdigest()}


def _result_descriptor(value: object, limit: int) -> tuple[int, str]:
    if not is_record(value):
        raise Refused("invalid result descriptor")
    _fields(value, {"size", "sha256"})
    size, digest = value["size"], value["sha256"]
    if type(size) is not int or not 0 <= size <= limit:
        raise Refused("result exceeds transfer limit")
    if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise Refused("invalid result digest")
    return size, digest


def _receive_result(
    connection: ssl.SSLSocket, descriptor: tuple[int, str], deadline: float
) -> bytes:
    size, digest = descriptor
    result = _receive(connection, size, deadline)
    if hashlib.sha256(result).hexdigest() != digest:
        raise Refused("result digest differs")
    return result


def _send_result(connection: ssl.SSLSocket, result: bytes, deadline: float) -> None:
    for offset in range(0, len(result), CHUNK):
        _write(connection, result[offset : offset + CHUNK], deadline)


def _read(connection: ssl.SSLSocket, deadline: float | None = None) -> dict[str, Any]:
    if deadline is None:
        deadline = time.monotonic() + TIMEOUT
    size = struct.unpack("!I", _receive(connection, 4, deadline))[0]
    if not 0 < size <= MAX_FRAME:
        raise Refused("API frame exceeds limit")
    return _load(_receive(connection, size, deadline))


def _send(connection: ssl.SSLSocket, value: dict[str, Any], deadline: float | None = None) -> None:
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


def _reply(connection: ssl.SSLSocket, deadline: float) -> dict[str, Any]:
    response = _read(connection, deadline)
    if response.get("ok") is not True:
        raise Refused(_text(response.get("error")))
    _fields(response, {"ok", "value"})
    if not is_record(response["value"]):
        raise Refused("invalid coordinator response")
    return response["value"]


class Endpoint(Protocol):
    """Authenticated dispatch shared by separate worker and controller listeners."""

    @property
    def grants(self) -> Mapping[str, object]:
        raise NotImplementedError

    def dispatch(
        self, fingerprint: str, request: dict[str, Any], connection: ssl.SSLSocket | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError


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

    def call(self, operation: str, session: str, **arguments: object) -> dict[str, Any]:
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
            if not is_record(response["value"]):
                raise Refused("invalid coordinator response")
            return response["value"]
        except (OSError, ValueError) as error:
            raise Refused("coordinator unavailable; resolve call identity") from error

    @contextmanager
    def _stream(
        self, operation: str, session: str, arguments: dict[str, Any], *, version: int = 2
    ) -> Generator[tuple[ssl.SSLSocket, float]]:
        try:
            deadline = time.monotonic() + TRANSFER_TIMEOUT
            with socket.create_connection(self.address, timeout=TIMEOUT) as raw:
                with self.context.wrap_socket(raw, server_hostname=self.hostname) as connection:
                    _send(
                        connection,
                        dict(
                            version=version,
                            operation=operation,
                            session=session,
                            arguments=arguments,
                        ),
                        deadline,
                    )
                    yield connection, deadline
        except (OSError, ValueError) as error:
            raise Refused("coordinator unavailable; resolve call identity") from error

    def call_result(
        self,
        operation: str,
        session: str,
        owner: str,
        call: str,
        request: bytes,
        *,
        result_bytes: int,
    ) -> dict[str, Any]:
        """Begin or retry a call, returning verified result bytes or the admission receipt."""
        if operation not in {"begin", "retry"} or len(request) > MAX_REQUEST:
            raise Refused("invalid result request")
        if type(result_bytes) is not int or not 0 < result_bytes <= MAX_SHARED_RESULT:
            raise Refused("invalid result limit")
        arguments = dict(owner=owner, call=call, request=_encoded(request))
        if result_bytes <= MAX_PAYLOAD:
            response = self.call(operation, session, **arguments)
            if "result" not in response or (operation == "retry" and response["result"] is None):
                raise Refused("missing result")
            if response["result"] is not None:
                response["result"] = _bytes(response["result"], result_bytes)
            return response
        with self._stream(operation, session, arguments, version=3) as (connection, deadline):
            response = _reply(connection, deadline)
            if "result" not in response or (operation == "retry" and response["result"] is None):
                raise Refused("missing result")
            if response["result"] is None:
                return response
            _fields(response, {"result"})
            descriptor = _result_descriptor(response["result"], result_bytes)
            result = _receive_result(connection, descriptor, deadline)
            complete = _reply(connection, deadline)
            if complete != {"complete": True} or complete["complete"] is not True:
                raise Refused("incomplete result transfer")
            return {"result": result}

    def publish(
        self,
        session: str,
        owner: str,
        call: str,
        request: bytes,
        checkpoint: Path,
        result: bytes,
        *,
        result_bytes: int = MAX_PAYLOAD,
    ) -> None:
        """Stream a stable checkpoint after begin; do not retry ambiguous publication."""
        if type(result_bytes) is not int or not 0 < result_bytes <= MAX_SHARED_RESULT:
            raise Refused("invalid result limit")
        if len(request) > MAX_REQUEST or len(result) > result_bytes:
            raise Refused("payload exceeds limit")
        version = 3 if result_bytes > MAX_PAYLOAD else 2
        entries = transfer.describe(checkpoint, MAX_CHECKPOINT, MAX_FILES)
        arguments = dict(
            owner=owner,
            call=call,
            request=_encoded(request),
            files=entries,
            result=_result_info(result) if version == 3 else _encoded(result),
        )
        with self._stream("publish", session, arguments, version=version) as (connection, deadline):
            if _reply(connection, deadline) != {"ready": True}:
                raise Refused("invalid coordinator readiness")
            transfer.send(checkpoint, entries, lambda block: _write(connection, block, deadline))
            if version == 3:
                _send_result(connection, result, deadline)
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
