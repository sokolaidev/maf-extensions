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
    for name in ("a", "b", "unknown", "controller"):
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
from scripts.experiments.mxc_session_patch.remote_scratch import Policy
scratch = Policy(**config['scratch']) if config.get('scratch') else None
coordinator = Coordinator(Path(config['root']), grants, sessions, enabled=True, boundary=boundary, scratch_policy=scratch)
try:
    with Server(('127.0.0.1', 0), server_context(certs/'server.pem', certs/'server.key', certs/'ca.pem'), coordinator) as server:
        Path(config['ready'] + '.tmp').write_text(str(server.server_address[1]))
        Path(config['ready'] + '.tmp').replace(config['ready'])
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
    scratch=None,
    profile=PROFILE,
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
                "scratch": asdict(scratch) if scratch else None,
                "grants": grants,
                "profile": profile,
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


def test_ready_file_is_not_visible_until_port_is_complete(tmp_path, certificates, monkeypatch):
    root = tmp_path / "runner"
    observed = []
    original_exists = Path.exists

    def exists(path):
        if (
            path == root / "ready"
            and (root / "publishing").is_file()
            and not (root / "observed").is_file()
        ):
            observed.append(original_exists(path))
            (root / "observed").touch()
        return original_exists(path)

    delayed = r"""
from pathlib import Path
import time
write_text = Path.write_text
def delayed_write(path, data, *args, **kwargs):
    if path.name == 'ready' or path.name.startswith('ready.'):
        write_text(path, '')
        write_text(path.parent / 'publishing', '')
        deadline = time.monotonic() + 10
        while not (path.parent / 'observed').is_file():
            if time.monotonic() > deadline:
                raise TimeoutError('readiness observer did not run')
            time.sleep(.01)
    return write_text(path, data, *args, **kwargs)
Path.write_text = delayed_write
"""
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(sys.modules[__name__], "SERVER", delayed + SERVER)
    with running(root, certificates) as (_, address):
        assert address[1] > 0
    assert observed == [False]


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


@pytest.mark.parametrize("phase", ["inventory", "payload"])
@pytest.mark.parametrize("target", ["session", "other"])
def test_stream_socket_io_does_not_block_revocation(tmp_path, phase, target):
    from concurrent.futures import ThreadPoolExecutor

    with coordinator(tmp_path / "db") as db:
        owner = start(db)
        direct(db, "publish", **publication(owner))
        with db.authority() as authority:
            victim = (
                authority._decode_owner(owner)
                if target == "session"
                else authority.acquire(
                    "other", SESSION.digest, remote.Resource("host-b", "boot-b", "guest-b")
                )
            )

        def revoke():
            with db.authority() as authority:
                authority.request_fence(victim, "stop")

        fenced = []

        class PausedConnection:
            writes = 0

            def settimeout(self, value):
                pass

            def sendall(self, block):
                self.writes += 1
                if self.writes == (1 if phase == "inventory" else 2):
                    # Sending cannot resume until a separate connection commits revocation.
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(revoke).result(timeout=10)
                    fenced.append(True)

        connection = PausedConnection()
        wire = request("restore", owner=owner) | {"version": 2}
        if target == "session":
            with pytest.raises(api.Refused, match="revoked"):
                db.dispatch(FINGERPRINT, wire, connection)
        else:
            assert db.dispatch(FINGERPRINT, wire, connection) == {"complete": True}
        assert fenced == [True]
        assert connection.writes == 2
        assert not (db.root / "transfer").exists()
        with db.authority() as authority:
            with pytest.raises(api.Refused, match="revoked"):
                with authority.guard(victim):
                    pass


SCRATCH = api.remote_scratch.Policy(bytes=4096, entries=8, total_bytes=4096)


def scratch_begin(db, call="call", guest="guest-a"):
    owner = direct(db, "acquire", guest=guest)["owner"]
    result = direct(db, "begin", owner=owner, call=call, request=encoded(b"code"))
    assert result["result"] is None
    receipt = api.remote_scratch.Receipt(**result["scratch"])
    assert receipt.owner == owner and receipt.call == call
    assert receipt.request == hashlib.sha256(b"code").hexdigest()
    return owner, receipt


def test_remote_scratch_publication_keeps_charge_until_verified_cleanup(tmp_path):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        owner, receipt = scratch_begin(db)
        direct(db, "publish", **publication(owner))
        assert db.pending_scratch() == [receipt]
        assert direct(db, "retry", owner=owner, call="call", request=encoded(b"code"))[
            "result"
        ] == encoded(b"result")
        with pytest.raises(api.Refused, match="reconciliation"):
            direct(db, "begin", owner=owner, call="next", request=encoded(b"code"))
        verified = []
        db.reconcile_scratch(receipt, lambda value: verified.append(value) or True)
        assert verified == [receipt] and db.pending_scratch() == []
        db.reconcile_scratch(receipt, lambda _: pytest.fail("released receipt must be idempotent"))
        assert direct(db, "restore", owner=owner)["call"] == "call"
        assert direct(db, "retry", owner=owner, call="call", request=encoded(b"code"))[
            "result"
        ] == encoded(b"result")
        _, second = scratch_begin(db, "next")
        assert second.token != receipt.token
        assert db.pending_scratch() == [second]
        db.stores["session"][1].audit_usage()


@pytest.mark.parametrize("proof", [False, None, 1, "yes"])
def test_remote_cleanup_requires_exact_trusted_confirmation(tmp_path, proof):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        owner, receipt = scratch_begin(db)
        with pytest.raises(api.Refused, match="not confirmed"):
            db.reconcile_scratch(receipt, lambda _: proof)
        assert db.pending_scratch() == [receipt]
        assert (
            db.stores["session"][1].db.execute("SELECT count(*) FROM reservations").fetchone()[0]
            == 1
        )
        with pytest.raises(api.Refused, match="no longer authorized"):
            direct(db, "publish", **publication(owner))
        db.reconcile_scratch(receipt, lambda _: True)
        assert db.pending_scratch() == []
        with pytest.raises(api.Refused, match="explicit recovery"):
            direct(db, "begin", owner=owner, call="call", request=encoded(b"code"))
        scratch_begin(db, "next")


@pytest.mark.parametrize(
    "field,value",
    [
        ("token", "b" * 32),
        ("call", "other"),
        ("owner", "wrong"),
        ("request", "b" * 64),
        ("bytes", 1),
        ("entries", 1),
    ],
)
def test_remote_cleanup_rejects_changed_receipt_before_verifier(tmp_path, field, value):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        _, receipt = scratch_begin(db)
        with pytest.raises(api.Refused):
            db.reconcile_scratch(
                replace(receipt, **{field: value}), lambda _: pytest.fail("unbound proof")
            )
        assert db.pending_scratch() == [receipt]


def test_remote_cleanup_verifier_does_not_hold_authority_lock(tmp_path):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        owner, receipt = scratch_begin(db)

        def verify(value):
            assert value == receipt
            with db.authority() as authority:
                authority.request_fence(authority._decode_owner(owner), "stop")
            return True

        db.reconcile_scratch(receipt, verify)
        assert db.pending_scratch() == []
        with pytest.raises(api.Refused, match="revoked"):
            direct(db, "publish", **publication(owner))


def test_remote_scratch_survives_host_loss_and_replacement(tmp_path):
    root = tmp_path / "db"
    with coordinator(root, scratch_policy=SCRATCH) as db:
        owner, receipt = scratch_begin(db)
    with coordinator(root, scratch_policy=SCRATCH) as db:
        assert db.pending_scratch() == [receipt]
        with db.authority() as authority:
            authority.request_fence(authority._decode_owner(owner), "stop")
            authority.confirm_fence("stop", lambda _: True)
        replacement = direct(db, "acquire", guest="replacement")["owner"]
        with pytest.raises(api.Refused, match="reconciliation"):
            direct(db, "begin", owner=replacement, call="new", request=encoded(b"code"))
        db.reconcile_scratch(receipt, lambda value: value == receipt)
        scratch_begin(db, "new", "replacement")
        with pytest.raises(api.Refused, match="revoked"):
            direct(db, "publish", **publication(owner))


def test_remote_scratch_budget_is_shared_across_sessions(tmp_path):
    grant = replace(GRANT, sessions=frozenset({"session", "other"}))
    db = api.Coordinator(
        tmp_path / "db",
        {FINGERPRINT: grant},
        {"session": SESSION, "other": SESSION},
        enabled=True,
        scratch_policy=SCRATCH,
    )
    try:
        _, receipt = scratch_begin(db)

        def other(op, **args):
            return db.dispatch(FINGERPRINT, request(op, **args) | {"session": "other"})

        owner = other("acquire", guest="other")["owner"]
        with pytest.raises(api.Refused, match="capacity"):
            other("begin", owner=owner, call="call", request=encoded(b"code"))
        db.reconcile_scratch(receipt, lambda _: True)
        assert (
            other("begin", owner=owner, call="call", request=encoded(b"code"))["scratch"]["bytes"]
            == 4096
        )
    finally:
        db.close()


def test_remote_admission_failure_retains_charge_for_reconciliation(tmp_path):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        owner = direct(db, "acquire", guest="guest-a")["owner"]
        store = db.stores["session"][1]
        store.db.execute("UPDATE sessions SET quota=?", (store.usage("session"),))
        with pytest.raises(api.Refused, match="quota"):
            direct(db, "begin", owner=owner, call="call", request=encoded(b"code"))
        (receipt,) = db.pending_scratch()
        assert store.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
        db.reconcile_scratch(receipt, lambda _: True)
        assert db.pending_scratch() == []
        with pytest.raises(api.Refused, match="reconciliation"):
            direct(db, "begin", owner=owner, call="call", request=encoded(b"code"))


@pytest.mark.parametrize("mode", ["disable", "policy", "enable"])
def test_remote_scratch_mode_and_policy_cannot_change_on_existing_root(tmp_path, mode):
    root = tmp_path / "db"
    with coordinator(root, scratch_policy=None if mode == "enable" else SCRATCH):
        pass
    policy = {"disable": None, "enable": SCRATCH, "policy": replace(SCRATCH, entries=9)}[mode]
    with pytest.raises(api.Refused, match="format|policy"):
        with coordinator(root, scratch_policy=policy):
            pass


@pytest.mark.parametrize("boundary", ["after_remote_scratch_reserve", "after_remote_admission"])
def test_tls_remote_admission_death_keeps_capacity(tmp_path, certificates, boundary):
    root = tmp_path / "runner"
    with running(root, certificates, crash=boundary, scratch=SCRATCH) as (child, address):
        a = client(certificates, address)
        owner = a.call("acquire", "session", guest="guest")["owner"]
        with pytest.raises(api.Refused):
            a.call("begin", "session", owner=owner, call="call", request=encoded(b"code"))
        assert child.wait(timeout=10) == 76
    grants = {fingerprint(certificates, "a"): api.Grant("host-a", "boot-a", frozenset({"session"}))}
    db = api.Coordinator(
        root / "service", grants, {"session": SESSION}, enabled=True, scratch_policy=SCRATCH
    )
    try:
        (receipt,) = db.pending_scratch()
        assert receipt.owner == owner
        with pytest.raises(api.Refused, match="reconciliation"):
            db.dispatch(
                fingerprint(certificates, "a"),
                request("begin", owner=owner, call="new", request=encoded(b"code")),
            )
        db.reconcile_scratch(receipt, lambda _: True)
        assert db.pending_scratch() == []
    finally:
        db.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "after_remote_cleanup_intent",
        "after_remote_cleanup_verification",
        "before_remote_reservation_release",
        "after_remote_reservation_release",
        "before_remote_scratch_release",
        "after_remote_scratch_release",
    ],
)
def test_remote_cleanup_process_death_is_conservative_and_repeatable(tmp_path, boundary):
    root = tmp_path / "db"
    with coordinator(root, scratch_policy=SCRATCH) as db:
        _, receipt = scratch_begin(db)
    script = """
import os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.remote_api import Coordinator, Grant, Session, Limits
from scripts.experiments.mxc_session_patch.remote_scratch import Policy

def boundary(point):
    if point == sys.argv[2]:
        os._exit(76)
with_root = Path(sys.argv[1])
db = Coordinator(with_root, {'a'*64: Grant('host-a','boot-a',frozenset({'session'}))}, {'session': Session({'runtime':'pinned','policy':'closed'}, Limits(200_000,500_000,checkpoint_bytes=1024,result_bytes=128,files=2))}, enabled=True, scratch_policy=Policy(4096,8,4096), boundary=boundary)
db.reconcile_scratch(db.pending_scratch()[0], lambda _: True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), boundary],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 76, result.stderr
    with coordinator(root, scratch_policy=SCRATCH) as db:
        assert db.pending_scratch() == (
            [] if boundary == "after_remote_scratch_release" else [receipt]
        )
        db.reconcile_scratch(receipt, lambda value: value == receipt)
        assert db.pending_scratch() == []
        store = db.stores.get("session")
        if store:
            assert store[1].db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
            store[1].audit_usage()
        scratch_begin(db, "next")


def test_tls_remote_scratch_receipt_and_cleanup_are_not_worker_authority(tmp_path, certificates):
    with running(tmp_path / "runner", certificates, scratch=SCRATCH) as (_, address):
        a = client(certificates, address)
        owner = a.call("acquire", "session", guest="guest")["owner"]
        admitted = a.call("begin", "session", owner=owner, call="call", request=encoded(b"code"))
        assert admitted["scratch"]["owner"] == owner
        assert a.call("publish", "session", **publication(owner)) == {"committed": True}
        with pytest.raises(api.Refused, match="not authorized"):
            a.call("reconcile_scratch", "session", receipt=admitted["scratch"], verified=True)
        with pytest.raises(api.Refused, match="reconciliation"):
            a.call("begin", "session", owner=owner, call="next", request=encoded(b"code"))


@pytest.mark.parametrize(
    "boundary,remaining",
    [
        ("before_remote_reservation_release", 1),
        ("after_remote_reservation_release", 0),
        ("before_remote_scratch_release", 0),
    ],
)
def test_remote_cleanup_storage_failure_never_refunds_unconfirmed_ledger(
    tmp_path, boundary, remaining
):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        _, receipt = scratch_begin(db)

        def fail(point):
            if point == boundary:
                raise api.sqlite3.OperationalError("disk full")

        db.boundary = fail
        with pytest.raises((api.Refused, api.sqlite3.OperationalError)):
            db.reconcile_scratch(receipt, lambda _: True)
        assert db.pending_scratch() == [receipt]
        store = db.stores["session"][1]
        assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == remaining
        db.boundary = lambda _: None
        db.reconcile_scratch(receipt, lambda _: True)
        store.audit_usage()
        assert db.pending_scratch() == []


def test_remote_cleanup_verifier_outage_preserves_charges(tmp_path):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        _, receipt = scratch_begin(db)

        def unavailable(_):
            raise OSError("host unavailable")

        with pytest.raises(OSError, match="unavailable"):
            db.reconcile_scratch(receipt, unavailable)
        assert db.pending_scratch() == [receipt]
        assert (
            db.stores["session"][1].db.execute("SELECT count(*) FROM reservations").fetchone()[0]
            == 1
        )


def test_remote_scratch_revocation_between_admission_transactions_retains_charge(tmp_path):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        owner = direct(db, "acquire", guest="guest")["owner"]

        def revoke(point):
            if point == "after_remote_scratch_reserve":
                with db.authority() as authority:
                    authority.request_fence(authority._decode_owner(owner), "stop")

        db.boundary = revoke
        with pytest.raises(api.Refused, match="revoked"):
            direct(db, "begin", owner=owner, call="call", request=encoded(b"code"))
        assert len(db.pending_scratch()) == 1
        assert db.stores["session"][1].db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0


@pytest.mark.parametrize("streaming", [False, True])
def test_remote_cleanup_intent_during_publication_prevents_final_commit(tmp_path, streaming):
    with coordinator(tmp_path / "db", scratch_policy=SCRATCH) as db:
        owner, receipt = scratch_begin(db)

        def revoke(point):
            if point == "before_commit":
                with db.authority() as authority, authority._transaction():
                    authority.db.execute(
                        "UPDATE remote_scratch SET state='cleaning' WHERE token=?", (receipt.token,)
                    )

        db.boundary = revoke

        class Connection:
            data = (
                b"checkpoint"
                + struct.pack("!I", len(api._json({"complete": True})))
                + api._json({"complete": True})
            )

            def settimeout(self, _):
                pass

            def sendall(self, _):
                pass

            def recv(self, count):
                block, self.data = self.data[:count], self.data[count:]
                return block

        wire = request("publish", **publication(owner))
        if streaming:
            wire["version"] = 2
            wire["arguments"]["files"] = [
                {
                    "path": "index.json",
                    "size": 10,
                    "sha256": hashlib.sha256(b"checkpoint").hexdigest(),
                }
            ]
        with pytest.raises(api.Refused, match="no longer authorized"):
            db.dispatch(FINGERPRINT, wire, Connection() if streaming else None)
        store = db.stores["session"][1]
        assert store.db.execute("SELECT status FROM calls").fetchone()[0] == "pending"
        assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
        assert db.pending_scratch() == [receipt]
        assert not (db.root / "transfer").exists()


@pytest.mark.parametrize("values", [(0, 1, 1), (1, 0, 1), (2, 1, 1), (True, 1, 1), (1, 1, 2**63)])
def test_remote_scratch_invalid_policy_refuses(values):
    with pytest.raises(api.Refused):
        api.remote_scratch.Policy(*values)


# These controls use real local processes but substitute the native payload producer.
@pytest.fixture
def native_host(tmp_path, monkeypatch):
    module = importlib.import_module("scripts.experiments.mxc_session_patch.remote_host")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "index.json").write_bytes(b"{}")
    helper = Path(sys.executable)
    limits = api.Limits(8_000_000, 16_000_000, checkpoint_bytes=1024, result_bytes=4096, files=2)
    config = api.Session(
        {
            "helper": module.digest(helper),
            "startup_index": module.digest(startup / "index.json"),
            "policy": "closed",
        },
        limits,
    )
    scratch = api.remote_scratch.Policy(3_000_000, 1100, 3_000_000)
    host = module.Host(
        tmp_path / "host",
        "host-a",
        "boot-a",
        "session",
        config,
        scratch,
        module.physical.Policy(1024 * 1024, 1024 * 1024, 0),
    )
    children = []

    def execute(
        helper, startup, work, code, output_limit, *, before_spawn, before_start, checkpoint_limits
    ):
        assert checkpoint_limits == (limits.checkpoint_bytes, limits.files)
        before_spawn()
        with subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
        ) as child:
            children.append(child.pid)
            try:
                before_start(child)
                candidate = work / "candidate"
                candidate.mkdir()
                (candidate / "index.json").write_bytes(code)
            finally:
                child.kill()
                child.wait(timeout=10)
        return b"result"

    monkeypatch.setattr(module, "execute", execute)
    return host, helper, startup, children


def native_coordinator(root, host):
    return api.Coordinator(
        root,
        {FINGERPRINT: GRANT},
        {"session": host.config},
        enabled=True,
        scratch_policy=host.scratch,
    )


def native_admit(db, code=b"code"):
    owner = direct(db, "acquire", guest="guest-a")["owner"]
    value = direct(db, "begin", owner=owner, call="call", request=encoded(code))
    return api.remote_scratch.Receipt(**value["scratch"])


class DirectNativeClient:
    def __init__(self, db):
        self.db = db

    def restore(self, session, owner, destination, **limits):
        value = direct(self.db, "restore", owner=owner)
        if value["call"]:
            destination.mkdir()
            for name, data in value["files"].items():
                (destination / name).write_bytes(base64.b64decode(data))
        return value["call"]

    def publish(self, session, owner, call, request, checkpoint, result):
        direct(
            self.db,
            "publish",
            owner=owner,
            call=call,
            request=encoded(request),
            files={p.name: encoded(p.read_bytes()) for p in checkpoint.iterdir()},
            result=encoded(result),
        )


def test_native_receipt_publication_cleanup_and_delayed_replay(tmp_path, native_host):
    host, helper, startup, children = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        transport = DirectNativeClient(db)
        assert host.execute(transport, receipt, b"code", helper, startup, 128) == b"result"
        assert len(children) == 1
        assert db.pending_scratch() == [receipt]
        with pytest.raises(api.Refused, match="recovery"):
            host.execute(transport, receipt, b"code", helper, startup, 128)
        db.reconcile_scratch(receipt, host.reconcile)
        assert db.pending_scratch() == []
        assert host.reconcile(receipt) is True
        assert not list((host.root / "scratch").iterdir())
        with pytest.raises(api.Refused, match="recovery"):
            host.execute(transport, receipt, b"code", helper, startup, 128)
        assert len(children) == 1
        assert direct(db, "retry", owner=receipt.owner, call="call", request=encoded(b"code")) == {
            "result": encoded(b"result")
        }
    finally:
        db.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "invalid"),
        ("call", "other"),
        ("token", "b" * 32),
        ("request", "f" * 64),
        ("bytes", 2_999_999),
        ("entries", 1099),
    ],
)
def test_native_cleanup_rejects_changed_receipt(tmp_path, native_host, field, value):
    host, helper, startup, _ = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        host.execute(DirectNativeClient(db), receipt, b"code", helper, startup, 128)
        with pytest.raises(api.Refused):
            host.reconcile(replace(receipt, **{field: value}))
        assert db.pending_scratch() == [receipt]
        db.reconcile_scratch(receipt, host.reconcile)
    finally:
        db.close()


@pytest.mark.parametrize(
    "point,reclaimable",
    [
        ("after_local_admission", True),
        ("after_remote_restore", True),
        ("after_spawn_intent", False),
        ("after_spawn", False),
        ("after_launch_commit", True),
        ("after_remote_publication", True),
    ],
)
def test_native_failure_keeps_charges_until_local_proof(tmp_path, native_host, point, reclaimable):
    host, helper, startup, _ = native_host
    db = native_coordinator(tmp_path / "service", host)

    def fail(at):
        if at == point:
            raise OSError("injected host stop")

    try:
        receipt = native_admit(db)
        with pytest.raises(OSError, match="injected"):
            host.execute(DirectNativeClient(db), receipt, b"code", helper, startup, 128, fail)
        assert db.pending_scratch() == [receipt]
        if reclaimable:
            db.reconcile_scratch(receipt, host.reconcile)
            assert db.pending_scratch() == []
        else:
            with pytest.raises(api.Refused, match="unidentified"):
                db.reconcile_scratch(receipt, host.reconcile)
            assert db.pending_scratch() == [receipt]
    finally:
        db.close()


@pytest.mark.parametrize(
    "point",
    [
        "after_helper_termination",
        "after_cleanup_intent",
        "after_scratch_removal",
        "before_cleanup_release",
        "after_cleanup_release",
    ],
)
def test_native_cleanup_failure_can_resume_without_refunding_early(tmp_path, native_host, point):
    host, helper, startup, _ = native_host
    db = native_coordinator(tmp_path / "service", host)

    def fail(at):
        if at == point:
            raise OSError("injected cleanup stop")

    try:
        receipt = native_admit(db)
        host.execute(DirectNativeClient(db), receipt, b"code", helper, startup, 128)
        with pytest.raises(OSError, match="injected"):
            db.reconcile_scratch(receipt, lambda r: host.reconcile(r, fail))
        assert db.pending_scratch() == [receipt]
        db.reconcile_scratch(receipt, host.reconcile)
        assert db.pending_scratch() == []
    finally:
        db.close()


def test_native_missing_journal_never_proves_cleanup(tmp_path, native_host):
    host, _, _, children = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        with pytest.raises(api.Refused, match="unavailable"):
            db.reconcile_scratch(receipt, host.reconcile)
        assert db.pending_scratch() == [receipt]
        assert not host.root.exists() and children == []
    finally:
        db.close()


@pytest.mark.parametrize("change", ["code", "helper", "startup", "budget"])
def test_native_preflight_refuses_before_launch(tmp_path, native_host, change):
    host, helper, startup, children = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        code = b"different" if change == "code" else b"code"
        if change == "helper":
            helper = tmp_path / "wrong-helper"
            helper.write_bytes(b"wrong")
        if change == "startup":
            (startup / "index.json").write_bytes(b"wrong")
        if change == "budget":
            host.scratch = replace(host.scratch, entries=1)
            receipt = replace(receipt, entries=1)
        with pytest.raises(api.Refused):
            host.execute(DirectNativeClient(db), receipt, code, helper, startup, 128)
        assert children == [] and not host.root.exists()
    finally:
        db.close()


@pytest.mark.skipif(
    not os.environ.get("MXC_REMOTE_HELPER"), reason="requires a built native helper"
)
def test_native_host_real_helper_tls_restore_and_cleanup(tmp_path, certificates, record_property):
    module = importlib.import_module("scripts.experiments.mxc_session_patch.remote_host")
    helper = Path(os.environ["MXC_REMOTE_HELPER"])
    startup = Path(os.environ["MXC_REMOTE_STARTUP"])
    limits = api.Limits(8 * api.MAX_CHECKPOINT, 16 * api.MAX_CHECKPOINT)
    config = api.Session(
        {
            "helper": module.digest(helper),
            "startup_index": module.digest(startup / "index.json"),
            "policy": "closed",
        },
        limits,
    )
    scratch = api.remote_scratch.Policy(
        2 * api.MAX_CHECKPOINT + 5 * module.CHUNK,
        2 * limits.files * (module.PATH_BYTES // 2 + 1) + 14,
        2 * api.MAX_CHECKPOINT + 5 * module.CHUNK,
    )
    host = module.Host(
        tmp_path / "host",
        "host-a",
        "boot-a",
        "session",
        config,
        scratch,
        module.physical.Policy(1024 * 1024, 1024 * 1024, 0),
    )
    root = tmp_path / "runner"
    previous = None
    for call, code, expected in [
        ("seed", b"remote_counter = 41; print(remote_counter)", b"41"),
        ("next", b"assert remote_counter == 41; remote_counter += 1; print(remote_counter)", b"42"),
    ]:
        with running(
            root, certificates, limits=limits, profile=dict(config.profile), scratch=scratch
        ) as (_, address):
            transport = client(certificates, address)
            owner = transport.call("acquire", "session", guest="guest-a")["owner"]
            result = host.call(transport, owner, call, code, helper, startup, 1024)
            assert base64.b64decode(json.loads(result)["console_base64"]).strip() == expected
            assert (
                host.call(transport, owner, call, code, tmp_path / "absent", startup, 1024)
                == result
            )
        grants = {fingerprint(certificates, "a"): GRANT}
        db = api.Coordinator(
            root / "service", grants, {"session": config}, enabled=True, scratch_policy=scratch
        )
        try:
            receipts = db.pending_scratch()
            assert len(receipts) == 1 and receipts[0].call == call
            owner_record = remote.Authority._decode_owner(owner)
            stored = db._store(owner_record)
            size = stored.db.execute(
                "SELECT sum(size) FROM files WHERE session=? AND call=?", ("session", call)
            ).fetchone()[0]
            assert size > api.MAX_FRAME
            record_property(f"{call}_checkpoint_bytes", size)
            db.reconcile_scratch(receipts[0], host.reconcile)
            assert not db.pending_scratch()
            assert not list((host.root / "scratch").iterdir())
            previous = receipts[0]
        finally:
            db.close()
    assert previous is not None
    with pytest.raises(api.Refused, match="recovery"):
        host.execute(None, previous, code, helper, startup, 1024)


def test_native_cleanup_terminates_recorded_live_helper(tmp_path, native_host):
    host, _, _, _ = native_host
    module = importlib.import_module("scripts.experiments.mxc_session_patch.remote_host")
    db = native_coordinator(tmp_path / "service", host)
    child = None
    try:
        receipt = native_admit(db)
        with host._store() as store:
            store.begin(
                receipt.token,
                host._binding(receipt),
                scratch=module.ScratchLimits(
                    receipt.bytes, receipt.entries, host.scratch.total_bytes
                ),
            )
            journal = module.NativeJournal(store)
            work = journal.prepare(receipt.token)
            journal.start(receipt.token)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            journal.arm(receipt.token, child)
            (work / "partial").write_bytes(b"unpublished")
        assert child.poll() is None
        db.reconcile_scratch(receipt, host.reconcile)
        assert child.wait(timeout=10) != 0
        assert not work.exists() and not db.pending_scratch()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        db.close()


def test_native_cleanup_refuses_unowned_files(tmp_path, native_host):
    host, helper, startup, _ = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        host.execute(DirectNativeClient(db), receipt, b"code", helper, startup, 128)
        unknown = host.root / "scratch" / "unowned"
        unknown.write_bytes(b"keep")
        with pytest.raises(api.Refused, match="unknown"):
            db.reconcile_scratch(receipt, host.reconcile)
        assert unknown.read_bytes() == b"keep" and db.pending_scratch() == [receipt]
    finally:
        db.close()


@pytest.mark.parametrize(
    "point,reclaimable",
    [("after_local_admission", True), ("after_spawn_intent", False), ("after_launch_commit", True)],
)
def test_native_host_process_death_retains_durable_proof(tmp_path, native_host, point, reclaimable):
    host, helper, startup, _ = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        config = tmp_path / "native-config.json"
        config.write_text(
            json.dumps(
                {
                    "root": str(host.root),
                    "profile": dict(host.config.profile),
                    "limits": asdict(host.config.limits),
                    "scratch": asdict(host.scratch),
                    "storage": asdict(host.storage),
                    "receipt": asdict(receipt),
                    "point": point,
                }
            ),
            encoding="utf-8",
        )
        script = r"""
import json, os, subprocess, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.remote_host import Host, Session, physical, remote_scratch, ScratchLimits, NativeJournal
from scripts.experiments.mxc_session_patch.shared_store import Limits
v = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
host = Host(Path(v['root']), 'host-a', 'boot-a', 'session', Session(v['profile'], Limits(**v['limits'])), remote_scratch.Policy(**v['scratch']), physical.Policy(**v['storage']))
r = remote_scratch.Receipt(**v['receipt'])
def boundary(point):
    if point == v['point']:
        os._exit(76)
with host._store() as store:
    store.begin(r.token, host._binding(r), scratch=ScratchLimits(r.bytes,r.entries,host.scratch.total_bytes))
    boundary('after_local_admission')
    journal = NativeJournal(store)
    work = journal.prepare(r.token)
    (work/'partial').write_bytes(b'partial')
    journal.start(r.token, boundary)
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE)
    journal.arm(r.token, child, boundary)
"""
        child = subprocess.run([sys.executable, "-c", script, str(config)], timeout=20)
        assert child.returncode == 76
        if reclaimable:
            db.reconcile_scratch(receipt, host.reconcile)
            assert not db.pending_scratch()
            with pytest.raises(api.Refused, match="recovery"):
                host.execute(DirectNativeClient(db), receipt, b"code", helper, startup, 128)
        else:
            with pytest.raises(api.Refused, match="unidentified"):
                db.reconcile_scratch(receipt, host.reconcile)
            assert db.pending_scratch() == [receipt]
    finally:
        db.close()


def generation_host(host, owner, root=None):
    return type(host)(
        root or host.root,
        host.host,
        host.boot,
        host.session,
        host.config,
        host.scratch,
        host.storage,
        owner=owner,
    )


CONTROL_SERVER = r"""
import json, os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch import remote_api as api, remote_control as control
from scripts.experiments.mxc_session_patch import remote_host, physical
config = json.loads(Path(sys.argv[1]).read_text())
certs = Path(config['certificates'])
owner = control.Authority._decode_owner(config['owner'])
host = remote_host.Host(Path(config['root']), owner.resource.host, owner.resource.boot,
    owner.session, api.Session(config['profile'], api.Limits(**config['limits'])),
    api.remote_scratch.Policy(**config['scratch']), physical.Policy(**config['storage']), owner=owner)
def boundary(point):
    if point == config.get('crash'):
        os._exit(76)
controller = control.Controller({owner.token: host},
    {config['fingerprint']: frozenset({owner.token})}, enabled=True,
    automatic_shutdown=config['automatic'], boundary=boundary)
with api.Server(('127.0.0.1', 0), api.server_context(certs/'server.pem', certs/'server.key', certs/'ca.pem'), controller) as server:
    Path(config['ready'] + '.tmp').write_text(str(server.server_address[1]))
    Path(config['ready'] + '.tmp').replace(config['ready'])
    server.serve_forever(poll_interval=0.05)
"""


@contextmanager
def control_running(root, certs, host, *, crash=None, automatic=False):
    import time

    root.mkdir(exist_ok=True)
    ready = root / "ready"
    ready.unlink(missing_ok=True)
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "root": str(host.root),
                "certificates": str(certs),
                "ready": str(ready),
                "owner": remote.Authority._encode(host.owner),
                "profile": dict(host.config.profile),
                "limits": asdict(host.config.limits),
                "scratch": asdict(host.scratch),
                "storage": asdict(host.storage),
                "fingerprint": fingerprint(certs, "controller"),
                "crash": crash,
                "automatic": automatic,
            }
        ),
        encoding="utf-8",
    )
    with subprocess.Popen(
        [sys.executable, "-c", CONTROL_SERVER, str(config)],
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
                    raise TimeoutError("controller did not become ready")
                time.sleep(0.01)
            yield child, ("127.0.0.1", int(ready.read_text()))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def control_verifier(certs, address, owner, name="controller"):
    control = importlib.import_module("scripts.experiments.mxc_session_patch.remote_control")
    context = api.client_context(certs / f"{name}.pem", certs / f"{name}.key", certs / "ca.pem")
    return control.Verifier(api.Client(address, context, "localhost"), owner)


def armed_generation(host, receipt):
    module = importlib.import_module("scripts.experiments.mxc_session_patch.remote_host")
    with host._store() as store:
        store.begin(
            receipt.token,
            host._binding(receipt),
            scratch=module.ScratchLimits(receipt.bytes, receipt.entries, host.scratch.total_bytes),
        )
        journal = module.NativeJournal(store)
        work = journal.prepare(receipt.token)
        journal.start(receipt.token)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            journal.arm(receipt.token, child)
            (work / "partial").write_bytes(b"unpublished")
        except BaseException:
            child.kill()
            child.wait(timeout=10)
            raise
    return child, work


def test_controller_tls_fences_generation_before_replacement(tmp_path, certificates, native_host):
    host, helper, startup, children = native_host
    db = native_coordinator(tmp_path / "service", host)
    child = unrelated = None
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        child, work = armed_generation(host, receipt)
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with control_running(tmp_path / "controller", certificates, host) as (_, address):
            verifier = control_verifier(certificates, address, owner)
            request = db.fence(owner, "stop", verifier)
            assert child.wait(timeout=10) != 0 and not work.exists()
            assert unrelated.poll() is None
            assert db.fence(owner, "stop", verifier) == request
        assert db.pending_scratch() == [receipt]
        db.reconcile_scratch(receipt, host.reconcile)
        assert db.pending_scratch() == []
        with db.authority() as authority:
            replacement = authority.acquire(
                "session", host.config.digest, remote.Resource("host-b", "boot-b", "replacement")
            )
            assert replacement.generation > owner.generation
            with pytest.raises(api.Refused, match="revoked"):
                with authority.guard(owner):
                    pass
        delayed = replace(receipt, token="f" * 32, call="delayed")
        with pytest.raises(api.Refused, match="retired"):
            host.execute(None, delayed, b"code", helper, startup, 128)
        assert children == []
    finally:
        for process in (child, unrelated):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        db.close()


@pytest.mark.parametrize(
    "point",
    [
        "before_retire_commit",
        "after_retire_commit",
        "after_helper_termination",
        "after_cleanup_intent",
        "after_scratch_removal",
        "after_cleanup_release",
    ],
)
def test_controller_crash_keeps_takeover_blocked_until_retry(
    tmp_path, certificates, native_host, point
):
    host, _, _, _ = native_host
    db = native_coordinator(tmp_path / "service", host)
    child = None
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        child, _ = armed_generation(host, receipt)
        with control_running(tmp_path / "controller", certificates, host, crash=point) as (
            server,
            address,
        ):
            with pytest.raises(api.Refused):
                db.fence(owner, "stop", control_verifier(certificates, address, owner))
            assert server.wait(timeout=10) == 76
        with db.authority() as authority:
            with pytest.raises(api.Refused, match="not confirmed"):
                authority.acquire(
                    "session",
                    host.config.digest,
                    remote.Resource("host-b", "boot-b", "replacement"),
                )
        db.close()
        db = native_coordinator(tmp_path / "service", host)
        with control_running(tmp_path / "controller", certificates, host) as (_, address):
            db.fence(owner, "stop", control_verifier(certificates, address, owner))
        assert child.wait(timeout=10) != 0
        assert db.pending_scratch() == [receipt]
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        db.close()


@pytest.mark.parametrize("failure", ["missing", "busy", "unknown", "unidentified", "wrong-owner"])
def test_controller_missing_or_ambiguous_evidence_refuses(tmp_path, native_host, failure):
    host, _, _, _ = native_host
    module = importlib.import_module("scripts.experiments.mxc_session_patch.remote_host")
    db = native_coordinator(tmp_path / "service", host)
    held = None
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        if failure != "missing":
            host.provision()
        if failure == "busy":
            held = host._store()
        elif failure == "unknown":
            (host.root / "scratch").mkdir()
            (host.root / "scratch" / "unknown").write_bytes(b"keep")
        elif failure == "unidentified":
            with host._store() as store:
                store.begin(
                    receipt.token,
                    host._binding(receipt),
                    scratch=module.ScratchLimits(
                        receipt.bytes, receipt.entries, host.scratch.total_bytes
                    ),
                )
                journal = module.NativeJournal(store)
                journal.prepare(receipt.token)
                journal.start(receipt.token)
        elif failure == "wrong-owner":
            host = generation_host(host, replace(owner, token="e" * 32))
        with pytest.raises(api.Refused):
            db.fence(owner, "stop", host.fence)
        with db.authority() as authority:
            with pytest.raises(api.Refused, match="not confirmed"):
                authority.acquire(
                    "session",
                    host.config.digest,
                    remote.Resource("host-b", "boot-b", "replacement"),
                )
        assert db.pending_scratch() == [receipt]
    finally:
        if held is not None:
            held.close()
        db.close()


def test_controller_retirement_covers_receipt_not_yet_journaled(tmp_path, native_host):
    host, helper, startup, children = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        db.fence(owner, "stop", host.fence)
        with pytest.raises(api.Refused, match="retired"):
            host.execute(None, receipt, b"code", helper, startup, 128)
        with pytest.raises(api.Refused, match="not recorded"):
            db.reconcile_scratch(receipt, host.reconcile)
        assert db.pending_scratch() == [receipt] and children == []
    finally:
        db.close()


@pytest.mark.parametrize("certificate", ["a", "b", "unknown", "expired", "untrusted"])
def test_controller_tls_worker_certificates_cannot_fence(
    tmp_path, certificates, native_host, certificate
):
    host, _, _, _ = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        with db.authority() as authority:
            request = authority.request_fence(owner, "stop")
        with control_running(tmp_path / "controller", certificates, host) as (_, address):
            with pytest.raises(api.Refused):
                control_verifier(certificates, address, owner, certificate)(request)
        with host._store() as store:
            assert store._owner()["state"] == "active"
    finally:
        db.close()


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "boot",
        "resource",
        "sessions",
        "machine",
        "automatic",
        "version",
        "operation",
        "extra",
    ],
)
def test_controller_rejects_changed_authority_before_retirement(tmp_path, native_host, change):
    host, _, _, _ = native_host
    control = importlib.import_module("scripts.experiments.mxc_session_patch.remote_control")
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        endpoint = control.Controller(
            {owner.token: host}, {FINGERPRINT: frozenset({owner.token})}, enabled=True
        )
        with db.authority() as authority:
            fence = authority.request_fence(owner, "stop")
        changed_owner = owner
        if change == "owner":
            changed_owner = replace(owner, generation=owner.generation + 1)
        elif change == "boot":
            changed_owner = replace(owner, resource=replace(owner.resource, boot="other"))
        elif change == "resource":
            fence = replace(fence, resource=replace(fence.resource, guest="other"))
        elif change == "sessions":
            fence = replace(fence, sessions=("other",))
        elif change == "machine":
            fence = replace(fence, scope="machine")
        elif change == "automatic":
            fence = replace(fence, automatic=True)
        payload = request(
            "fence",
            owner=remote.Authority._encode(changed_owner),
            fence=remote.Authority._encode(fence),
        )
        if change == "version":
            payload["version"] = True
        elif change == "operation":
            payload["operation"] = "publish"
        elif change == "extra":
            payload["arguments"]["path"] = str(host.root)
        with pytest.raises(api.Refused):
            endpoint.dispatch(FINGERPRINT, payload)
        with host._store() as store:
            assert store._owner()["state"] == "active"
    finally:
        db.close()


@pytest.mark.parametrize(
    "coordinator_opt_in,controller_opt_in",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_controller_automatic_shutdown_requires_both_opt_ins(
    tmp_path, native_host, coordinator_opt_in, controller_opt_in
):
    host, _, _, _ = native_host
    control = importlib.import_module("scripts.experiments.mxc_session_patch.remote_control")
    db = native_coordinator(tmp_path / "service", host)
    db.fencing_policy = remote.Policy(automatic_shutdown=coordinator_opt_in)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        endpoint = control.Controller(
            {owner.token: host},
            {FINGERPRINT: frozenset({owner.token})},
            enabled=True,
            automatic_shutdown=controller_opt_in,
        )

        def verify(fence):
            endpoint.dispatch(
                FINGERPRINT,
                request(
                    "fence",
                    owner=remote.Authority._encode(owner),
                    fence=remote.Authority._encode(fence),
                ),
            )
            return True

        if coordinator_opt_in and controller_opt_in:
            db.fence(owner, "stop", verify, automatic=True)
        else:
            with pytest.raises(api.Refused, match="automatic shutdown"):
                db.fence(owner, "stop", verify, automatic=True)
        with host._store() as store:
            assert (store._owner()["state"] == "retired") == (
                coordinator_opt_in and controller_opt_in
            )
    finally:
        db.close()


def test_controller_response_must_match_request_and_owner(tmp_path, native_host):
    host, _, _, _ = native_host
    control = importlib.import_module("scripts.experiments.mxc_session_patch.remote_control")
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        with db.authority() as authority:
            fence = authority.request_fence(owner, "stop")

        class WrongResponse:
            def call(self, *args, **kwargs):
                return {
                    "owner": receipt.owner,
                    "fence": remote.Authority._encode(replace(fence, id="old")),
                }

        with pytest.raises(api.Refused, match="response binding"):
            db.fence(owner, "stop", control.Verifier(WrongResponse(), owner))
        with db.authority() as authority:
            assert authority.db.execute("SELECT state FROM sessions").fetchone()[0] == "fencing"
    finally:
        db.close()


def test_controller_refuses_unbound_legacy_or_nonclosed_roots(tmp_path, native_host):
    host, helper, startup, _ = native_host
    control = importlib.import_module("scripts.experiments.mxc_session_patch.remote_control")
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        with pytest.raises(api.Refused, match="generation-bound"):
            control.Controller(
                {owner.token: host}, {FINGERPRINT: frozenset({owner.token})}, enabled=True
            )
        bound = generation_host(host, owner)
        with pytest.raises(api.Refused, match="provisioned"):
            bound.execute(None, receipt, b"code", helper, startup, 128)
        with host._store():
            pass
        with pytest.raises(api.Refused, match="profile"):
            bound.provision()
        config = api.Session(
            dict(host.config.profile) | {"policy": "allowlist"}, host.config.limits
        )
        with pytest.raises(api.Refused, match="closed-network"):
            type(host)(
                tmp_path / "other",
                host.host,
                host.boot,
                host.session,
                config,
                host.scratch,
                host.storage,
                owner=replace(owner, profile=config.digest),
            )
    finally:
        db.close()


def test_controller_storage_failure_before_retirement_never_confirms(
    tmp_path, native_host, monkeypatch
):
    host, _, _, _ = native_host
    module = importlib.import_module("scripts.experiments.mxc_session_patch.remote_host")
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()

        def fail(*args, **kwargs):
            raise api.Refused("storage unavailable")

        monkeypatch.setattr(module.NativeJournal, "delete", fail)
        with pytest.raises(api.Refused, match="storage unavailable"):
            db.fence(owner, "stop", host.fence)
        with db.authority() as authority:
            assert authority.db.execute("SELECT state FROM fences").fetchone()[0] == "pending"
    finally:
        db.close()


@pytest.mark.parametrize("damage", ["zero", "empty-schema", "missing-session"])
def test_controller_empty_journal_never_becomes_termination_proof(tmp_path, native_host, damage):
    import sqlite3

    host, helper, startup, _ = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        path = host.root / "shared.sqlite"
        if damage in ("zero", "empty-schema"):
            path.write_bytes(b"")
            if damage == "empty-schema":
                with sqlite3.connect(path) as connection:
                    connection.execute("PRAGMA user_version=1")
        else:
            with sqlite3.connect(path) as connection:
                connection.execute("DELETE FROM sessions")
        with pytest.raises(api.Refused):
            db.fence(owner, "stop", host.fence)
        with pytest.raises(api.Refused):
            host.execute(None, receipt, b"code", helper, startup, 128)
        with db.authority() as authority:
            assert authority.db.execute("SELECT state FROM fences").fetchone()[0] == "pending"
    finally:
        db.close()


def test_controller_fencing_preserves_committed_checkpoint_and_retry(tmp_path, native_host):
    host, helper, startup, children = native_host
    db = native_coordinator(tmp_path / "service", host)
    try:
        receipt = native_admit(db)
        owner = remote.Authority._decode_owner(receipt.owner)
        host = generation_host(host, owner)
        host.provision()
        assert (
            host.execute(DirectNativeClient(db), receipt, b"code", helper, startup, 128)
            == b"result"
        )
        db.fence(owner, "stop", host.fence)
        db.reconcile_scratch(receipt, host.reconcile)
        replacement = direct(db, "acquire", guest="replacement")["owner"]
        assert direct(db, "retry", owner=replacement, call="call", request=encoded(b"code")) == {
            "result": encoded(b"result")
        }
        assert direct(db, "restore", owner=replacement)["files"] == {"index.json": encoded(b"code")}
        assert len(children) == 1
    finally:
        db.close()
