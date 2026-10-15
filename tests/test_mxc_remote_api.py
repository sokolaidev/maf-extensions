"""Real TLS authorization, publication fencing and coordinator restart controls."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import random
import socket
import ssl
import struct
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    api = importlib.import_module("scripts.experiments.mxc_session_patch.remote_api")
    remote = importlib.import_module("scripts.experiments.mxc_session_patch.remote_authority")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

LIMITS = api.Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=128, files=2)
PROFILE = {"runtime": "pinned", "policy": "closed"}
SESSION = api.Session(PROFILE, LIMITS)
FINGERPRINT = "a" * 64
GRANT = api.Grant("host-a", "boot-a", frozenset({"session"}))


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def request(operation, **arguments):
    return {"version": 1, "operation": operation, "session": "session", "arguments": arguments}


def direct(db, operation, **arguments):
    return db.dispatch(FINGERPRINT, request(operation, **arguments))


@contextmanager
def coordinator(root, **kwargs):
    db = api.Coordinator(root, {FINGERPRINT: GRANT}, {"session": SESSION}, enabled=True, **kwargs)
    try:
        yield db
    finally:
        db.close()


def publication(owner, call="call", data=b"checkpoint"):
    return dict(
        owner=owner,
        call=call,
        request=encoded(b"code"),
        files={"index.json": encoded(data)},
        result=encoded(b"result"),
    )


def start(db):
    owner = direct(db, "acquire", guest="guest-a")["owner"]
    assert direct(db, "begin", owner=owner, call="call", request=encoded(b"code")) == {
        "result": None
    }
    return owner


@pytest.mark.parametrize("boundary", ["checkpoint_stored", "before_commit"])
def test_revocation_during_preparation_rolls_back_real_publication(tmp_path, boundary):
    with coordinator(tmp_path / "db") as db:
        owner = start(db)
        direct(db, "publish", **publication(owner))
        direct(db, "begin", owner=owner, call="next", request=encoded(b"code"))

        def revoke(point):
            if point == boundary:
                with db.authority() as authority:
                    authority.request_fence(authority._decode_owner(owner), "stop")

        db.boundary = revoke
        with pytest.raises(api.Refused, match="revoked"):
            direct(db, "publish", **publication(owner, "next", b"uncommitted"))
        store = db.stores["session"][1]
        assert store.restore(tmp_path / "restored") == "call"
        assert (tmp_path / "restored/index.json").read_bytes() == b"checkpoint"
        assert store.begin("call", b"code") == b"result"
        assert (
            store.db.execute("SELECT status FROM calls WHERE id='next'").fetchone()[0] == "pending"
        )
        store.audit_usage()


def test_publication_guard_prevents_revocation_until_actual_commit(tmp_path, monkeypatch):
    original = remote.Authority.guard
    attempts = []

    @contextmanager
    def guard(authority, owner):
        with remote.Authority(authority.root, enabled=True) as contender:
            with original(authority, owner) as connection:
                yield connection
                with pytest.raises(api.Refused, match="unavailable"):
                    contender.request_fence(owner, "stop")
                attempts.append(True)

    with coordinator(tmp_path / "db") as db:
        owner = start(db)
        # Install at the final preparation boundary to isolate the commit guard.
        db.boundary = lambda point: (
            monkeypatch.setattr(remote.Authority, "guard", guard)
            if point == "before_commit"
            else None
        )
        direct(db, "publish", **publication(owner))
        assert attempts == [True]
        assert db.stores["session"][1].begin("call", b"code") == b"result"


def test_quota_and_retention_are_reused_without_reexecution(tmp_path):
    with coordinator(tmp_path / "db") as db:
        owner = start(db)
        direct(db, "publish", **publication(owner))
        assert direct(db, "retry", owner=owner, call="call", request=encoded(b"code")) == {
            "result": encoded(b"result")
        }
        store = db.stores["session"][1]
        store.db.execute("UPDATE calls SET expires=0,grace=0,remaining=0")
        with pytest.raises(api.Refused, match="result_expired"):
            direct(db, "retry", owner=owner, call="call", request=encoded(b"code"))
        assert direct(db, "restore", owner=owner)["files"] == {"index.json": encoded(b"checkpoint")}
        store.db.execute("UPDATE sessions SET quota=?", (store.usage("session"),))
        with pytest.raises(api.Refused, match="quota"):
            direct(db, "begin", owner=owner, call="new", request=encoded(b"code"))
        store.audit_usage()


@pytest.mark.parametrize(
    "field,value",
    [
        ("files", {"../escape": "eA=="}),
        ("files", {"sub/file": "eA=="}),
        ("files", {"index.json": encoded(b"x" * 1025)}),
        ("result", encoded(b"x" * 129)),
        ("files", {"index.json": "invalid!"}),
        ("request", encoded(b"different")),
    ],
)
def test_bad_publication_never_commits_or_leaks_transfer(tmp_path, field, value):
    with coordinator(tmp_path / "db") as db:
        owner = start(db)
        args = publication(owner) | {field: value}
        with pytest.raises(api.Refused):
            direct(db, "publish", **args)
        assert not (db.root / "transfer").exists()
        assert (
            db.stores["session"][1].db.execute("SELECT status FROM calls").fetchone()[0]
            == "pending"
        )


def test_service_is_explicit_and_exclusive_and_unknown_retry_does_not_admit(tmp_path):
    with pytest.raises(api.Refused, match="opt-in"):
        api.Coordinator(tmp_path / "db", {FINGERPRINT: GRANT}, {"session": SESSION})
    assert not (tmp_path / "db").exists()
    with coordinator(tmp_path / "db") as db:
        with pytest.raises((api.Refused, OSError)):
            with coordinator(tmp_path / "db"):
                pass
        owner = direct(db, "acquire", guest="guest")["owner"]
        with pytest.raises(api.Refused, match="unknown call"):
            direct(db, "retry", owner=owner, call="missing", request=encoded(b"code"))
        assert db.stores["session"][1].db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_permission_and_certificate_scope_before_store_access(tmp_path):
    with coordinator(tmp_path / "db") as db:
        with pytest.raises(api.Refused, match="authorized"):
            db.dispatch("b" * 64, request("acquire", guest="guest"))
        with pytest.raises(api.Refused, match="authorized"):
            db.dispatch(FINGERPRINT, request("acquire", guest="guest") | {"session": "other"})
        owner = direct(db, "acquire", guest="guest")["owner"]
        db.grants[FINGERPRINT] = replace(GRANT, operations=frozenset({"retry"}))
        with pytest.raises(api.Refused, match="authorized"):
            direct(db, "begin", owner=owner, call="call", request=encoded(b"code"))
        assert db.stores["session"][1].db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    root = tmp_path_factory.mktemp("mxc-tls")
    now = datetime.now(UTC)

    def make(name, issuer=None, *, server=False, expired=False):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        issuer_cert, issuer_key = (None, key) if issuer is None else issuer
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject if issuer_cert is None else issuer_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=-1 if expired else 2))
            .add_extension(
                x509.BasicConstraints(ca=issuer is None, path_length=0 if issuer is None else None),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=issuer is None,
                    crl_sign=issuer is None,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        )
        if issuer is not None:
            builder = builder.add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
            )
        cert = builder.sign(issuer_key, hashes.SHA256())
        (root / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root / f"{name}.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return cert, key

    ca = make("ca")
    make("server", ca, server=True)
    for name in ("a", "b", "unknown"):
        make(name, ca)
    make("expired", ca, expired=True)
    other = make("other-ca")
    make("untrusted", other)
    return root


SERVER = r"""
import json, os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.remote_api import Coordinator, Grant, Session, Limits, Server, server_context
from scripts.experiments.mxc_session_patch import remote_api, remote_authority
config = json.loads(Path(sys.argv[1]).read_text())
certs = Path(config['certificates'])
remote_api.TIMEOUT = config['timeout']
remote_api.TRANSFER_TIMEOUT = config['transfer_timeout']
original_send = remote_api.transfer.send
def send(root, entries, write):
    damage = config.get('download_damage')
    if damage == 'corrupt':
        original_send(root, entries, lambda block: write(bytes([block[0] ^ 1]) + block[1:]))
    elif damage == 'partial':
        write(b'part')
        raise remote_api.Refused('injected interrupted download')
    else:
        original_send(root, entries, write)
        if damage == 'revoked':
            with coordinator.authority() as authority:
                owner = authority.acquire('session', sessions['session'].digest,
                    remote_authority.Resource('host-a', 'boot-a', 'guest'))
                authority.request_fence(owner, 'stop')
remote_api.transfer.send = send
def boundary(point):
    if point == config.get('crash'):
        os._exit(76)
grants = {k: Grant(v['host'], v['boot'], frozenset(v['sessions']), frozenset(v['operations'])) for k,v in config['grants'].items()}
sessions = {'session': Session(config['profile'], Limits(**config['limits']))}
coordinator = Coordinator(Path(config['root']), grants, sessions, enabled=True, boundary=boundary)
try:
    with Server(('127.0.0.1', 0), server_context(certs/'server.pem', certs/'server.key', certs/'ca.pem'), coordinator) as server:
        Path(config['ready']).write_text(str(server.server_address[1]))
        server.serve_forever(poll_interval=0.05)
finally:
    coordinator.close()
"""


def fingerprint(certs, name):
    cert = x509.load_pem_x509_certificate((certs / f"{name}.pem").read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()


@contextmanager
def running(
    root,
    certs,
    *,
    crash=None,
    grants=None,
    timeout=api.TIMEOUT,
    limits=LIMITS,
    transfer_timeout=api.TRANSFER_TIMEOUT,
    download_damage=None,
):
    import time

    root.mkdir(exist_ok=True)
    ready = root / "ready"
    ready.unlink(missing_ok=True)
    config = root / "config.json"
    if grants is None:
        grants = {
            fingerprint(certs, n): {
                "host": f"host-{n}",
                "boot": f"boot-{n}",
                "sessions": ["session"],
                "operations": sorted(api.OPERATIONS),
            }
            for n in ("a", "b")
        }
    config.write_text(
        json.dumps(
            {
                "root": str(root / "service"),
                "certificates": str(certs),
                "ready": str(ready),
                "crash": crash,
                "timeout": timeout,
                "transfer_timeout": transfer_timeout,
                "download_damage": download_damage,
                "grants": grants,
                "profile": PROFILE,
                "limits": asdict(limits),
            }
        )
    )
    with subprocess.Popen(
        [sys.executable, "-c", SERVER, str(config)],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                if child.poll() is not None:
                    raise AssertionError(child.communicate()[1].decode())
                if time.monotonic() > deadline:
                    raise TimeoutError("coordinator did not become ready")
                time.sleep(0.01)
            yield child, ("127.0.0.1", int(ready.read_text()))
        finally:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=15)


def client(certs, address, name="a", hostname="localhost"):
    context = api.client_context(certs / f"{name}.pem", certs / f"{name}.key", certs / "ca.pem")
    return api.Client(address, context, hostname)


def test_tls_publish_restart_retry_and_verified_takeover(tmp_path, certificates):
    root = tmp_path / "runner"
    with running(root, certificates) as (_, address):
        a = client(certificates, address)
        owner = a.call("acquire", "session", guest="guest-a")["owner"]
        assert a.call("begin", "session", owner=owner, call="call", request=encoded(b"code")) == {
            "result": None
        }
        assert a.call("publish", "session", **publication(owner)) == {"committed": True}
        b = client(certificates, address, "b")
        with pytest.raises(api.Refused, match="scope"):
            b.call("retry", "session", owner=owner, call="call", request=encoded(b"code"))
    with running(root, certificates) as (_, address):
        a = client(certificates, address)
        assert a.call("acquire", "session", guest="guest-a")["owner"] == owner
        assert a.call("retry", "session", owner=owner, call="call", request=encoded(b"code"))[
            "result"
        ] == encoded(b"result")
        with remote.Authority(root / "service/ownership", enabled=True) as authority:
            authority.request_fence(authority._decode_owner(owner), "stop")
            b = client(certificates, address, "b")
            with pytest.raises(api.Refused, match="not confirmed"):
                b.call("acquire", "session", guest="guest-b")
            authority.confirm_fence("stop", lambda _: True)
        replacement = b.call("acquire", "session", guest="guest-b")["owner"]
        assert b.call("restore", "session", owner=replacement) == {
            "call": "call",
            "files": {"index.json": encoded(b"checkpoint")},
        }
        assert b.call("retry", "session", owner=replacement, call="call", request=encoded(b"code"))[
            "result"
        ] == encoded(b"result")
        with pytest.raises(api.Refused, match="revoked"):
            a.call("publish", "session", **publication(owner))
        b.call("begin", "session", owner=replacement, call="next", request=encoded(b"code"))
        b.call("publish", "session", **publication(replacement, "next", b"replacement"))


@pytest.mark.parametrize("name", ["unknown", "expired", "untrusted"])
def test_real_tls_rejects_unpermitted_certificates(tmp_path, certificates, name):
    with running(tmp_path / "runner", certificates) as (_, address):
        with pytest.raises(api.Refused):
            client(certificates, address, name).call("acquire", "session", guest="guest")
        assert client(certificates, address).call("acquire", "session", guest="guest")["owner"]


def test_tls_requires_client_cert_and_coordinator_hostname(tmp_path, certificates):
    with running(tmp_path / "runner", certificates) as (_, address):
        context = ssl.create_default_context(cafile=str(certificates / "ca.pem"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        anonymous = api.Client(address, context, "localhost")
        with pytest.raises(api.Refused):
            anonymous.call("acquire", "session", guest="guest")
        with pytest.raises(api.Refused):
            client(certificates, address, hostname="wrong.invalid").call(
                "acquire", "session", guest="guest"
            )


@pytest.mark.parametrize("boundary", ["checkpoint_stored", "before_commit", "after_commit"])
def test_server_death_resolves_original_call_without_execution(tmp_path, certificates, boundary):
    root = tmp_path / "runner"
    with running(root, certificates, crash=boundary) as (child, address):
        a = client(certificates, address)
        owner = a.call("acquire", "session", guest="guest")["owner"]
        a.call("begin", "session", owner=owner, call="call", request=encoded(b"code"))
        with pytest.raises(api.Refused):
            a.call("publish", "session", **publication(owner))
        assert child.wait(timeout=10) == 76
    with running(root, certificates) as (_, address):
        a = client(certificates, address)
        if boundary == "after_commit":
            assert a.call("retry", "session", owner=owner, call="call", request=encoded(b"code"))[
                "result"
            ] == encoded(b"result")
            assert a.call("restore", "session", owner=owner)["call"] == "call"
        else:
            with pytest.raises(api.Refused, match="explicit recovery"):
                a.call("retry", "session", owner=owner, call="call", request=encoded(b"code"))
            assert a.call("restore", "session", owner=owner) == {"call": None, "files": {}}
            with pytest.raises(api.Refused, match="reservation"):
                a.call("begin", "session", owner=owner, call="new", request=encoded(b"code"))
        assert not (root / "service/transfer").exists()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":1,"version":1}',
        b"[]",
        b'{"version":true}',
        b'{"version":1,"operation":"confirm_fence","session":"session","arguments":{}}',
    ],
)
def test_malformed_records_and_worker_confirmation_are_refused(tmp_path, certificates, payload):
    with running(tmp_path / "runner", certificates) as (_, address):
        context = api.client_context(
            certificates / "a.pem", certificates / "a.key", certificates / "ca.pem"
        )
        with socket.create_connection(address, timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(struct.pack("!I", len(payload)) + payload)
                assert api._read(connection)["ok"] is False
        assert client(certificates, address).call("acquire", "session", guest="guest")["owner"]


def test_oversized_frame_refused_without_reading_payload(tmp_path, certificates):
    with running(tmp_path / "runner", certificates) as (_, address):
        context = api.client_context(
            certificates / "a.pem", certificates / "a.key", certificates / "ca.pem"
        )
        with socket.create_connection(address, timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(struct.pack("!I", api.MAX_FRAME + 1))
                assert api._read(connection)["ok"] is False


def test_weak_tls_configuration_refused():
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(api.Refused, match="verified"):
        api.Client(("localhost", 1), insecure, "localhost")


def test_certificate_removal_revokes_access_after_restart(tmp_path, certificates):
    root = tmp_path / "runner"
    with running(root, certificates) as (_, address):
        owner = client(certificates, address).call("acquire", "session", guest="guest")["owner"]
    grants = {
        fingerprint(certificates, "b"): {
            "host": "host-b",
            "boot": "boot-b",
            "sessions": ["session"],
            "operations": sorted(api.OPERATIONS),
        }
    }
    with running(root, certificates, grants=grants) as (_, address):
        with pytest.raises(api.Refused, match="authorized"):
            client(certificates, address).call("restore", "session", owner=owner)
        with pytest.raises(api.Refused, match="already has an owner"):
            client(certificates, address, "b").call("acquire", "session", guest="guest-b")


@pytest.mark.parametrize("field", ["host", "boot", "profile"])
def test_acquisition_does_not_accept_worker_identity_claims(tmp_path, field):
    with coordinator(tmp_path / "db") as db:
        with pytest.raises(api.Refused, match="fields"):
            direct(db, "acquire", **{"guest": "guest", field: "worker-supplied"})
        assert not db.stores


def test_request_limit_refuses_before_admission(tmp_path):
    with coordinator(tmp_path / "db") as db:
        owner = direct(db, "acquire", guest="guest")["owner"]
        with pytest.raises(api.Refused, match="limit"):
            direct(
                db, "begin", owner=owner, call="call", request=encoded(b"x" * (api.MAX_REQUEST + 1))
            )
        assert db.stores["session"][1].db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["directory", "hardlink", "oversize"])
def test_transfer_recovery_refuses_unowned_or_over_budget_entries(tmp_path, damage):
    root = tmp_path / "db"
    with coordinator(root):
        pass
    transfer = root / "transfer"
    api.prepare(transfer)
    file = transfer / "index.json"
    if damage == "directory":
        file = transfer.joinpath(*(["d"] * (api.transfer.MAX_DEPTH + 2)))
        file.mkdir(parents=True)
    elif damage == "hardlink":
        import os

        other = tmp_path / "preserve"
        other.write_bytes(b"preserve")
        os.link(other, file)
    else:
        with file.open("wb") as stream:
            stream.truncate(api.MAX_CHECKPOINT + 1)
    with pytest.raises(api.Refused):
        with coordinator(root):
            pass
    assert file.exists()


def test_changed_profile_and_boot_refuse_saved_owner(tmp_path):
    root = tmp_path / "db"
    with coordinator(root) as db:
        owner = direct(db, "acquire", guest="guest")["owner"]
        db.grants[FINGERPRINT] = replace(GRANT, boot="new-boot")
        with pytest.raises(api.Refused, match="scope"):
            direct(db, "restore", owner=owner)
        db.sessions["session"] = api.Session({"runtime": "different"}, LIMITS)
        with pytest.raises(api.Refused, match="compatibility"):
            direct(db, "acquire", guest="replacement")


def test_unavailable_coordinator_does_not_fall_back(tmp_path, certificates):
    with running(tmp_path / "runner", certificates) as (child, address):
        a = client(certificates, address)
        owner = a.call("acquire", "session", guest="guest")["owner"]
        child.terminate()
        child.wait(timeout=10)
        with pytest.raises(api.Refused, match="unavailable"):
            a.call("begin", "session", owner=owner, call="call", request=encoded(b"code"))


def test_partial_frame_deadline_releases_serial_server(tmp_path, certificates):
    with running(tmp_path / "runner", certificates, timeout=0.25) as (_, address):
        context = api.client_context(
            certificates / "a.pem", certificates / "a.key", certificates / "ca.pem"
        )
        with socket.create_connection(address, timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(struct.pack("!I", 100) + b"{")
                assert api._read(connection)["ok"] is False
        assert client(certificates, address).call("acquire", "session", guest="guest")["owner"]


def test_competing_tls_clients_cannot_both_acquire(tmp_path, certificates):
    from concurrent.futures import ThreadPoolExecutor

    with running(tmp_path / "runner", certificates) as (_, address):

        def acquire(name):
            try:
                return client(certificates, address, name).call(
                    "acquire", "session", guest=f"guest-{name}"
                )
            except api.Refused:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, ["a", "b"]))
        assert sum(result is not None for result in results) == 1


def test_restore_destination_stays_below_private_transfer_root(tmp_path, monkeypatch):
    original = api.SharedStore.restore
    observed = []

    def restore(store, destination):
        assert destination.name == "checkpoint"
        api.prepare(destination.parent)
        observed.append(destination)
        return original(store, destination)

    monkeypatch.setattr(api.SharedStore, "restore", restore)
    with coordinator(tmp_path / "db") as db:
        owner = start(db)
        direct(db, "publish", **publication(owner))
        assert direct(db, "restore", owner=owner)["call"] == "call"
        assert not (db.root / "transfer").exists()
        assert len(observed) == 1


@contextmanager
def streaming_request(a, operation, **arguments):
    with a._stream(operation, "session", arguments) as (connection, deadline):
        yield connection, deadline


def entry(name="blobs/sha256/data", data=b"checkpoint"):
    return dict(path=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())


def tls_start(a):
    owner = a.call("acquire", "session", guest="guest")["owner"]
    a.call("begin", "session", owner=owner, call="call", request=encoded(b"code"))
    return owner


def stream_args(owner, **changes):
    return (
        dict(
            owner=owner,
            call="call",
            request=encoded(b"code"),
            files=[entry()],
            result=encoded(b"result"),
        )
        | changes
    )


def test_streaming_nested_checkpoint_restart_and_native_size(
    tmp_path, certificates, record_property, monkeypatch
):
    size = int(os.environ.get("MXC_TRANSFER_BYTES", 8 * 1024**2))
    record_property("checkpoint_payload_bytes", size + 2)
    limits = api.Limits(
        size * 4 + 200_000, size * 8 + 500_000, checkpoint_bytes=size + 2, result_bytes=128, files=3
    )
    source = tmp_path / "source"
    blob = source / "blobs/sha256/data"
    blob.parent.mkdir(parents=True)
    block = random.Random(1671).randbytes(1024**2)
    digest = hashlib.sha256()
    with blob.open("wb") as stream:
        remaining = size
        while remaining:
            part = block[: min(len(block), remaining)]
            stream.write(part)
            digest.update(part)
            remaining -= len(part)
    (source / "index.json").write_bytes(b"{}")
    (source / "empty").write_bytes(b"")
    original = Path.read_bytes

    def read_bytes(path):
        assert path.name != "data", "checkpoint must not be buffered as a whole file"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    root = tmp_path / "runner"
    with running(root, certificates, limits=limits) as (_, address):
        a = client(certificates, address)
        owner = tls_start(a)
        a.publish("session", owner, "call", b"code", source, b"result")
        assert not (root / "service/transfer").exists()
        with pytest.raises(api.Refused, match="streaming"):
            a.call("restore", "session", owner=owner)
    with running(root, certificates, limits=limits) as (_, address):
        a = client(certificates, address)
        destination = tmp_path / "restored"
        assert (
            a.restore("session", owner, destination, checkpoint_bytes=size + 2, files=3) == "call"
        )
        restored = destination / "blobs/sha256/data"
        with restored.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == digest.hexdigest()
        assert restored.stat().st_size == size
        assert (destination / "index.json").read_bytes() == b"{}"
        assert (destination / "empty").read_bytes() == b""
        assert a.call("retry", "session", owner=owner, call="call", request=encoded(b"code"))[
            "result"
        ] == encoded(b"result")
        assert not list(tmp_path.glob("mxc-transfer-*"))


@pytest.mark.parametrize(
    "files",
    [
        [],
        [entry("../escape")],
        [entry("CON")],
        [entry("a\\b")],
        [entry("/absolute")],
        [entry("a/" * 16 + "b")],
        [entry("a" * 513)],
        [entry("A"), entry("a")],
        [entry("A/x"), entry("a/y")],
        [entry("a"), entry("a/b")],
        [entry("a/b"), entry("a")],
        [entry(), entry()],
        [entry() | {"size": True}],
        [entry() | {"size": -1}],
        [entry() | {"size": 1025}],
        [entry() | {"sha256": "bad"}],
        [entry() | {"extra": 1}],
        [entry("a"), entry("b"), entry("c")],
    ],
)
def test_stream_manifest_refused_before_ready(tmp_path, certificates, files):
    with running(tmp_path / "runner", certificates) as (_, address):
        a = client(certificates, address)
        owner = tls_start(a)
        with streaming_request(a, "publish", **stream_args(owner, files=files)) as (
            connection,
            deadline,
        ):
            with pytest.raises(api.Refused):
                api._reply(connection, deadline)
        assert not (tmp_path / "runner/service/transfer").exists()


@pytest.mark.parametrize("failure", ["hash", "partial", "completion", "timeout"])
def test_stream_failure_preserves_previous_checkpoint_and_reservation(
    tmp_path, certificates, failure
):
    root = tmp_path / "runner"
    with running(root, certificates, transfer_timeout=0.5) as (_, address):
        a = client(certificates, address)
        owner = tls_start(a)
        a.call("publish", "session", **publication(owner))
        a.call("begin", "session", owner=owner, call="next", request=encoded(b"code"))
        with streaming_request(a, "publish", **stream_args(owner, call="next")) as (
            connection,
            deadline,
        ):
            assert api._reply(connection, deadline) == {"ready": True}
            if failure == "partial":
                connection.sendall(b"short")
            else:
                if failure != "timeout":
                    connection.sendall(b"corruption" if failure == "hash" else b"checkpoint")
                    if failure == "completion":
                        api._send(connection, {"complete": False})
                with pytest.raises(api.Refused):
                    api._reply(connection, deadline)
        destination = tmp_path / "restored"
        assert a.restore("session", owner, destination) == "call"
        assert (destination / "index.json").read_bytes() == b"checkpoint"
        assert not (root / "service/transfer").exists()
        with pytest.raises(api.Refused, match="explicit recovery"):
            a.call("retry", "session", owner=owner, call="next", request=encoded(b"code"))
    with api.SharedStore(root / "service/publication", "session", PROFILE, LIMITS) as store:
        assert (
            store.db.execute("SELECT COUNT(*) FROM reservations WHERE call='next'").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("boundary", ["checkpoint_stored", "before_commit", "after_commit"])
def test_stream_crash_resolves_original_identity(tmp_path, certificates, boundary):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.json").write_bytes(b"checkpoint")
    root = tmp_path / "runner"
    with running(root, certificates, crash=boundary) as (child, address):
        a = client(certificates, address)
        owner = tls_start(a)
        with pytest.raises(api.Refused):
            a.publish("session", owner, "call", b"code", source, b"result")
        assert child.wait(timeout=10) == 76
    with running(root, certificates) as (_, address):
        a = client(certificates, address)
        if boundary == "after_commit":
            assert a.call("retry", "session", owner=owner, call="call", request=encoded(b"code"))[
                "result"
            ] == encoded(b"result")
            assert a.restore("session", owner, tmp_path / "restored") == "call"
        else:
            with pytest.raises(api.Refused, match="explicit recovery"):
                a.call("retry", "session", owner=owner, call="call", request=encoded(b"code"))
            assert a.restore("session", owner, tmp_path / "absent") is None
            assert not (tmp_path / "absent").exists()
        assert not (root / "service/transfer").exists()


def test_stream_rejects_revocation_during_upload(tmp_path, certificates):
    root = tmp_path / "runner"
    with running(root, certificates) as (_, address):
        a = client(certificates, address)
        owner = tls_start(a)
        with streaming_request(a, "publish", **stream_args(owner)) as (connection, deadline):
            assert api._reply(connection, deadline) == {"ready": True}
            with remote.Authority(root / "service/ownership", enabled=True) as authority:
                authority.request_fence(authority._decode_owner(owner), "stop")
            connection.sendall(b"checkpoint")
            api._send(connection, {"complete": True})
            with pytest.raises(api.Refused, match="revoked"):
                api._reply(connection, deadline)
    with api.SharedStore(root / "service/publication", "session", PROFILE, LIMITS) as store:
        assert (
            store.db.execute("SELECT COUNT(*) FROM calls WHERE status='committed'").fetchone()[0]
            == 0
        )
        assert store.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


def test_stream_download_limits_and_existing_destination(tmp_path, certificates):
    with running(tmp_path / "runner", certificates) as (_, address):
        a = client(certificates, address)
        owner = tls_start(a)
        a.call("publish", "session", **publication(owner))
        destination = tmp_path / "restored"
        with pytest.raises(api.Refused, match="byte limit"):
            a.restore("session", owner, destination, checkpoint_bytes=1)
        assert not destination.exists()
        assert not list(tmp_path.glob("mxc-transfer-*"))
        destination.mkdir()
        (destination / "preserve").write_bytes(b"keep")
        with pytest.raises(api.Refused, match="already exists"):
            a.restore("session", owner, destination)
        assert (destination / "preserve").read_bytes() == b"keep"


def test_stream_requires_admitted_call_and_certificate_scope(tmp_path, certificates):
    with running(tmp_path / "runner", certificates) as (_, address):
        a = client(certificates, address)
        owner = tls_start(a)
        for peer, arguments, message in [
            (a, stream_args(owner, call="unknown"), "identity"),
            (a, stream_args(owner, request=encoded(b"changed")), "identity"),
            (client(certificates, address, "b"), stream_args(owner), "scope"),
        ]:
            with streaming_request(peer, "publish", **arguments) as (connection, deadline):
                with pytest.raises(api.Refused, match=message):
                    api._reply(connection, deadline)


def test_stream_source_mutation_refused_before_completion(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "data"
    path.write_bytes(b"first")
    entries = api.transfer.describe(source, 100, 1)
    path.write_bytes(b"other")
    with pytest.raises(api.Refused, match="changed"):
        api.transfer.send(source, entries, lambda _: None)


def test_stream_nested_staging_recovered_after_process_death(tmp_path, certificates):
    root = tmp_path / "runner"
    with running(root, certificates) as (child, address):
        a = client(certificates, address)
        owner = tls_start(a)
        with streaming_request(a, "publish", **stream_args(owner)) as (connection, deadline):
            assert api._reply(connection, deadline) == {"ready": True}
            connection.sendall(b"part")
            child.terminate()
            child.wait(timeout=10)
    with running(root, certificates) as (_, address):
        assert not (root / "service/transfer").exists()
        with pytest.raises(api.Refused, match="explicit recovery"):
            client(certificates, address).call(
                "retry", "session", owner=owner, call="call", request=encoded(b"code")
            )


@pytest.mark.parametrize("damage", ["corrupt", "partial", "revoked"])
def test_stream_download_failure_never_exposes_destination(tmp_path, certificates, damage):
    with running(tmp_path / "runner", certificates, download_damage=damage) as (_, address):
        a = client(certificates, address)
        owner = tls_start(a)
        a.call("publish", "session", **publication(owner))
        destination = tmp_path / "restored"
        with pytest.raises(api.Refused):
            a.restore("session", owner, destination)
        assert not destination.exists()
        assert not list(tmp_path.glob("mxc-transfer-*"))


def test_stream_storage_failure_cleans_partial_bytes_without_refunding(
    tmp_path, certificates, monkeypatch
):
    import io

    class Connection:
        def __init__(self):
            self.output = io.BytesIO()

        def settimeout(self, value):
            pass

        def sendall(self, value):
            self.output.write(value)

    def fail(root, entries, read):
        root.mkdir()
        (root / "partial").write_bytes(b"part")
        raise OSError("injected storage failure")

    monkeypatch.setattr(api.transfer, "receive", fail)
    with coordinator(tmp_path / "db") as db:
        owner = start(db)
        wire = request("publish", **stream_args(owner)) | {"version": 2}
        with pytest.raises(OSError, match="storage failure"):
            db.dispatch(FINGERPRINT, wire, Connection())
        assert not (db.root / "transfer").exists()
        store = db.stores["session"][1]
        assert store.db.execute("SELECT status FROM calls").fetchone()[0] == "pending"
        assert store.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
