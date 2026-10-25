"""Three-machine native qualification harness; never a deployable worker API."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import cross_compat, cross_crash, remote_control, remote_host
from . import remote_api as api
from .remote_authority import Authority

ROLES = ("a", "b", "coordinator")
SESSIONS = frozenset({"session", "missing"})
PORT = 9443
SEED = b"remote_counter = 41; print(remote_counter)"
NEXT = b"assert remote_counter == 41; remote_counter += 1; print(remote_counter)"
DEADLINE = 1200


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _binding() -> dict:
    return cross_compat.binding()


def _fingerprint(pem: str) -> str:
    return x509.load_pem_x509_certificate(pem.encode()).fingerprint(hashes.SHA256()).hex()


def _boot() -> str:
    return hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()


def bootstrap(root: Path, bundle: Path, role: str, address: str) -> None:
    """Publish only a certificate and run-bound discovery; the private key stays local."""
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"mxc-{role}")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(f"mxc-{role}")]), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (root / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(root / "key.pem", 0o600)
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    (root / "certificate.pem").write_text(pem, encoding="ascii")
    profile = cross_compat.load_fixture(bundle)
    vendor = cross_compat.cpu_vendor() if role != "coordinator" else None
    _save(
        root / "public" / f"{role}.json",
        {
            "role": role,
            "address": address,
            "certificate": pem,
            "profile": profile,
            "cpu_vendor": vendor,
            "boot": _boot(),
            "binding": _binding(),
        },
    )


def validate_peers(peers: dict[str, dict], expected: dict) -> None:
    """Reject mixed attempts, duplicate machines and incompatible native fixtures."""
    _require(set(peers) == set(ROLES), "three roles are required")
    for role, peer in peers.items():
        _require(
            set(peer)
            == {"role", "address", "certificate", "profile", "cpu_vendor", "boot", "binding"},
            "unexpected discovery _fields",
        )
        _require(
            peer["role"] == role and peer["binding"] == expected,
            "discovery belongs to another run or role",
        )
        _require(
            ipaddress.ip_address(peer["address"]) in ipaddress.ip_network("100.64.0.0/10"),
            "peer is outside the overlay",
        )
        _require(
            len(peer["boot"]) == 64 and all(c in "0123456789abcdef" for c in peer["boot"]),
            "invalid boot identity",
        )
        cross_compat.validate_profile(peer["profile"])
        _require(peer["profile"] == peers["a"]["profile"], "native profiles differ")
        if role != "coordinator":
            _require(
                peer["cpu_vendor"] == peer["profile"]["cpu_vendor"],
                f"{role}: execution CPU vendor differs from fixture producer",
            )
        else:
            _require(peer["cpu_vendor"] is None, "coordinator must not claim native execution")
        certificate = x509.load_pem_x509_certificate(peer["certificate"].encode())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        _require(
            names.get_values_for_type(x509.DNSName) == [f"mxc-{role}"], "certificate role differs"
        )
    for field in ("address", "boot", "certificate"):
        _require(len({p[field] for p in peers.values()}) == 3, f"roles share a {field}")
    _require(
        peers["a"]["profile"]["platform"] == "Linux", "only Linux is qualified by this harness"
    )


def validate_local(bundle: Path, role: str, peer: dict) -> None:
    """Recheck local fixture and CPU before opening an execution or coordinator endpoint."""
    _require(cross_compat.load_fixture(bundle) == peer["profile"], "local fixture profile differs")
    if role != "coordinator":
        _require(
            cross_compat.cpu_vendor() == peer["cpu_vendor"] == peer["profile"]["cpu_vendor"],
            "local execution CPU vendor differs from admitted profile",
        )


def discover(root: Path, role: str) -> None:
    """Read public manifests from this exact Actions run, with bounded readiness polling."""
    peers = {role: json.loads((root / "public" / f"{role}.json").read_text("utf-8"))}
    until = time.monotonic() + 600
    while set(peers) != set(ROLES):
        for other in set(ROLES) - peers.keys():
            destination = root / "discovery" / other
            destination.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [
                    "gh",
                    "run",
                    "download",
                    os.environ["GITHUB_RUN_ID"],
                    "--repo",
                    os.environ["GITHUB_REPOSITORY"],
                    "--name",
                    f"mxc-peer-{other}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
                    "--dir",
                    str(destination),
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                peers[other] = json.loads((destination / f"{other}.json").read_text("utf-8"))
        _require(time.monotonic() < until, "peer jobs did not become _ready within ten minutes")
        if set(peers) != set(ROLES):
            time.sleep(5)
    validate_peers(peers, _binding())
    _save(root / "peers.json", peers)
    for other, peer in peers.items():
        (root / f"{other}.pem").write_text(peer["certificate"], encoding="ascii")
    (root / "trust.pem").write_text(
        "".join(p["certificate"] for p in peers.values()), encoding="ascii"
    )


def _client(root: Path, peers: dict, role: str) -> api.Client:
    return api.Client(
        (peers[role]["address"], PORT),
        api.client_context(root / "certificate.pem", root / "key.pem", root / f"{role}.pem"),
        f"mxc-{role}",
    )


def call(
    transport: api.Client, operation: str, session: str = "session", **arguments: object
) -> dict:
    """Allow bounded native execution time on the test-control connection, without retry."""
    until = time.monotonic() + 600
    with socket.create_connection(transport.address, timeout=10) as raw:
        with transport.context.wrap_socket(raw, server_hostname=transport.hostname) as connection:
            api._send(
                connection,
                dict(version=1, operation=operation, session=session, arguments=arguments),
                until,
            )
            return api._reply(connection, until)


def _configuration(profile: dict) -> tuple[api.Session, api.remote_scratch.Policy]:
    limits = api.Limits(8 * api.MAX_CHECKPOINT, 16 * api.MAX_CHECKPOINT)
    scratch = api.remote_scratch.Policy(
        2 * api.MAX_CHECKPOINT + 5 * remote_host.CHUNK,
        2 * limits.files * (remote_host.PATH_BYTES // 2 + 1) + 14,
        2 * api.MAX_CHECKPOINT + 5 * remote_host.CHUNK,
    )
    return api.Session(profile, limits), scratch


def _fields(request: dict, names: set[str]) -> dict:
    api._fields(request, {"version", "operation", "session", "arguments"})
    if (
        request["version"] != 1
        or request["session"] not in SESSIONS
        or not isinstance(request["arguments"], dict)
    ):
        raise api.Refused("invalid harness request")
    api._fields(request["arguments"], names)
    return request["arguments"]


class Worker:
    """Trusted test driver with fixed programs; only the coordinator certificate can drive it."""

    def __init__(self, root: Path, bundle: Path, role: str, peers: dict):
        validate_local(bundle, role, peers[role])
        self.root, self.bundle, self.role, self.peers = root, bundle, role, peers
        self.grants = {_fingerprint(peers["coordinator"]["certificate"]): frozenset(SESSIONS)}
        self.config, self.scratch = _configuration(peers[role]["profile"])
        self.hosts: dict[str, remote_host.Host] = {}
        self.transport = _client(root, peers, "coordinator")
        self.done = self.passed = False

    def dispatch(
        self, fingerprint: str, request: dict, connection: ssl.SSLSocket | None = None
    ) -> dict:
        if fingerprint not in self.grants:
            raise api.Refused("test driver certificate required")
        operation = request.get("operation")
        if operation == "fence":
            hosts = {h.owner.token: h for h in self.hosts.values() if h.owner is not None}
            controller = remote_control.Controller(
                hosts, {fingerprint: frozenset(hosts)}, enabled=True
            )
            return controller.dispatch(fingerprint, request, connection)
        args = _fields(
            request,
            {"receipt"}
            if operation in {"reconcile", "late"}
            else {"passed"}
            if operation == "finish"
            else set(),
        )
        session = request["session"]
        if operation == "_ready":
            return {"_ready": True}
        if operation == "finish":
            self.done, self.passed = True, args["passed"] is True
            return {"finished": True}
        if operation == "acquire":
            if session in self.hosts:
                raise api.Refused("test generation already configured")
            encoded = self.transport.call("acquire", session, guest=session)["owner"]
            owner = Authority._decode_owner(encoded)
            host = remote_host.Host(
                self.root / session,
                self.role,
                self.peers[self.role]["boot"],
                session,
                self.config,
                self.scratch,
                remote_host.physical.Policy(1024**2, 1024**2, 0),
                owner=owner,
            )
            if session != "missing":
                host.provision()
            self.hosts[session] = host
            return {"owner": encoded}
        if session not in self.hosts:
            raise api.Refused("test generation is not configured")
        host = self.hosts[session]
        if operation in {"reconcile", "late"}:
            receipt = api.remote_scratch.Receipt(**args["receipt"])
            if operation == "reconcile":
                _require(host.reconcile(receipt) is True, "scratch proof failed")
                return {"receipt": asdict(receipt)}
            host.execute(
                self.transport,
                receipt,
                cross_crash.CODE if receipt.call == "crash" else SEED,
                self.bundle / "helper",
                self.bundle / "startup",
                1024,
            )
            raise RuntimeError("retired generation executed a receipt")
        if operation == "crash":
            if self.role != "a" or session != "session":
                raise api.Refused("crash control requires the primary A generation")
            return cross_crash.interrupt(host, self.transport, self.root, self.bundle)
        if operation not in {"seed", "next", "replay", "stale", "retry-crash"}:
            raise api.Refused("unsupported test operation")
        if operation == "retry-crash":
            code, call_id = cross_crash.CODE, "crash"
        else:
            code = NEXT if operation == "next" else SEED
            call_id = "seed" if operation == "replay" else operation
        helper = self.root / "absent-helper" if operation == "replay" else self.bundle / "helper"
        if host.owner is None:
            raise api.Refused("generation ownership is missing")
        result = host.call(
            self.transport,
            Authority._encode(host.owner),
            call_id,
            code,
            helper,
            self.bundle / "startup",
            1024,
        )
        return {"result": api._encoded(result)}


class Service:
    """Serialize test orchestration and real coordinator requests on the SQLite owner thread."""

    def __init__(self, root: Path, peers: dict):
        self.root, self.peers = root, peers
        self.admin = _fingerprint(peers["coordinator"]["certificate"])
        grants = {
            _fingerprint(peers[r]["certificate"]): api.Grant(r, peers[r]["boot"], SESSIONS)
            for r in ("a", "b")
        }
        config, scratch = _configuration(peers["a"]["profile"])
        self.db = api.Coordinator(
            root / "service",
            grants,
            {s: config for s in SESSIONS},
            enabled=True,
            scratch_policy=scratch,
        )
        self.grants = {**grants, self.admin: None}
        self.done = False

    def dispatch(
        self, fingerprint: str, request: dict, connection: ssl.SSLSocket | None = None
    ) -> dict:
        if fingerprint != self.admin:
            return self.db.dispatch(fingerprint, request, connection)
        operation = request.get("operation")
        args = _fields(
            request,
            {"owner"}
            if operation in {"inspect", "fence-owner", "request-fence"}
            else {"receipt"}
            if operation == "reconcile"
            else set(),
        )
        if operation == "_ready":
            return {"_ready": True}
        if operation == "stop":
            self.done = True
            return {"stopped": True}
        if operation == "reconcile":
            receipt = api.remote_scratch.Receipt(**args["receipt"])
            target = Authority._decode_owner(receipt.owner).resource.host
            self.db.reconcile_scratch(
                receipt,
                lambda r: (
                    call(
                        _client(self.root, self.peers, target),
                        "reconcile",
                        Authority._decode_owner(r.owner).session,
                        receipt=asdict(r),
                    )
                    == {"receipt": asdict(r)}
                ),
            )
            return {"released": True}
        if operation not in {"inspect", "fence-owner", "request-fence"}:
            raise api.Refused("unsupported test orchestration")
        owner = Authority._decode_owner(args["owner"])
        if operation == "request-fence":
            with self.db.authority() as authority:
                authority.request_fence(owner, f"stop-{owner.session}-{owner.generation}")
            return {"pending": True}
        if operation == "fence-owner":
            proof = remote_control.Verifier(
                _client(self.root, self.peers, owner.resource.host), owner
            )
            self.db.fence(owner, f"stop-{owner.session}-{owner.generation}", proof)
            return {"fenced": True}
        store = self.db._store(owner)
        size = store.db.execute(
            "SELECT coalesce(sum(size),0) FROM files WHERE session=? AND call=(SELECT current_call FROM sessions WHERE id=?)",
            (owner.session, owner.session),
        ).fetchone()[0]
        current = store.db.execute(
            "SELECT current_call FROM sessions WHERE id=?", (owner.session,)
        ).fetchone()[0]
        return {
            "bytes": size,
            "current_call": current,
            "receipts": [asdict(r) for r in self.db.pending_scratch()],
        }


def serve(root: Path, bundle: Path, role: str, peers: dict) -> bool:
    """Serve one role until explicit completion or its bounded deadline."""
    endpoint = Service(root, peers) if role == "coordinator" else Worker(root, bundle, role, peers)
    context = api.server_context(root / "certificate.pem", root / "key.pem", root / "trust.pem")
    until = time.monotonic() + DEADLINE
    try:
        with api.Server((peers[role]["address"], PORT), context, endpoint) as server:
            server.timeout = 1
            while not endpoint.done:
                _require(time.monotonic() < until, "test controller timed out")
                server.handle_request()
        return not isinstance(endpoint, Worker) or endpoint.passed
    finally:
        if isinstance(endpoint, Service):
            endpoint.db.close()


def _ready(transport: api.Client) -> None:
    until = time.monotonic() + 120
    while True:
        try:
            _require(call(transport, "_ready") == {"_ready": True}, "invalid readiness")
            return
        except (OSError, api.Refused):
            _require(time.monotonic() < until, "test endpoint did not become _ready")
            time.sleep(1)


def _refused(action: Callable[[], object], message: str) -> None:
    try:
        action()
    except api.Refused as error:
        _require(message in str(error), f"unexpected refusal: {error}")
    else:
        raise RuntimeError(f"expected refusal: {message}")


def _output(result: dict, expected: bytes) -> None:
    value = api._bytes(result["result"], api.MAX_PAYLOAD)
    _require(
        api._bytes(json.loads(value)["console_base64"], 1024).strip() == expected,
        "native Python state differs",
    )


def scenario(
    a: api.Client,
    b: api.Client,
    admin: api.Client,
    restart: Callable[[], None],
    *,
    worker_crash: bool = False,
) -> dict:
    """Exercise recovery through the actual API; artifact handoff never transports call state."""
    owner_a = call(a, "acquire")["owner"]
    _refused(lambda: call(b, "acquire"), "already has an owner")
    seed = call(a, "seed")
    _output(seed, b"41")
    state_a = call(admin, "inspect", owner=owner_a)
    _require(state_a["bytes"] > api.MAX_FRAME, "checkpoint did not exercise native-size streaming")
    _require(len(state_a["receipts"]) == 1, "expected one retained receipt")
    restart()
    _require(call(a, "replay") == seed, "coordinator restart lost saved result")
    missing = call(a, "acquire", "missing")["owner"]
    _refused(lambda: call(admin, "fence-owner", "missing", owner=missing), "journal is unavailable")
    _refused(lambda: call(b, "acquire", "missing"), "termination is not confirmed")
    crash = {}
    if worker_crash:
        call(admin, "reconcile", receipt=state_a["receipts"][0])
        crash = call(a, "crash")
        _require(
            crash == {**cross_crash.READY, "worker_killed": True},
            "worker crash was not established",
        )
        _refused(lambda: call(b, "acquire"), "already has an owner")
        pending = call(admin, "inspect", owner=owner_a)
        _require(
            pending["bytes"] == state_a["bytes"] and pending["current_call"] == "seed",
            "unpublished candidate changed the checkpoint",
        )
        _require(
            len(pending["receipts"]) == 1 and pending["receipts"][0]["call"] == "crash",
            "crash scratch charge was lost",
        )
        _require(
            call(admin, "request-fence", owner=owner_a) == {"pending": True},
            "termination request was not pending",
        )
        _refused(lambda: call(b, "acquire"), "termination is not confirmed")
        restart()
        _refused(lambda: call(b, "acquire"), "termination is not confirmed")
        _require(
            call(admin, "inspect", owner=owner_a) == pending, "pending fence changed retained state"
        )
        state_a = pending
    call(admin, "fence-owner", owner=owner_a)
    _refused(lambda: call(a, "stale"), "revoked")
    _refused(lambda: call(a, "late", receipt=state_a["receipts"][0]), "explicit recovery")
    call(admin, "reconcile", receipt=state_a["receipts"][0])
    owner_b = call(b, "acquire")["owner"]
    _require(
        Authority._decode_owner(owner_b).generation > Authority._decode_owner(owner_a).generation,
        "replacement generation did not advance",
    )
    _require(call(b, "replay") == seed, "replacement reexecuted or lost saved result")
    if worker_crash:
        _refused(lambda: call(b, "retry-crash"), "explicit recovery")
    _output(call(b, "next"), b"42")
    state_b = call(admin, "inspect", owner=owner_b)
    _require(
        state_b["bytes"] > api.MAX_FRAME and len(state_b["receipts"]) == 1,
        "replacement publication is incomplete",
    )
    call(admin, "fence-owner", owner=owner_b)
    call(admin, "reconcile", receipt=state_b["receipts"][0])
    _require(call(admin, "inspect", owner=owner_b)["receipts"] == [], "scratch charges remain")
    return {
        **({"worker_crash": crash} if worker_crash else {}),
        "seed_checkpoint_bytes": state_a["bytes"],
        "replacement_checkpoint_bytes": state_b["bytes"],
        "controls": [
            "competing-owner-_refused",
            "coordinator-restart",
            "missing-proof-_refused",
            "stale-owner-_refused",
            "retired-receipt-_refused",
            "saved-result-without-helper",
            "native-state-restored",
            "scratch-reconciled",
        ]
        + (
            [
                "worker-process-killed-before-publication",
                "takeover-blocked-without-proof",
                "pending-fence-survived-restart",
                "unpublished-state-discarded",
                "interrupted-call-refused",
            ]
            if worker_crash
            else []
        ),
    }


def orchestrate(root: Path, bundle: Path, peers: dict, *, worker_crash: bool = False) -> dict:
    """Restart the coordinator process while both execution hosts remain available."""
    transports = {role: _client(root, peers, role) for role in ROLES}
    process = None
    log = (root / "service.log").open("wb")
    passed = False

    def start() -> subprocess.Popen:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.experiments.mxc_session_patch.cross_runner",
                "service",
                "--root",
                str(root),
                "--bundle",
                str(bundle),
                "--role",
                "coordinator",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _ready(transports["coordinator"])
        except BaseException:
            child.terminate()
            child.wait(timeout=15)
            raise
        return child

    def restart() -> None:
        nonlocal process
        _require(process is not None, "coordinator was not started")
        call(transports["coordinator"], "stop")
        if process is not None:
            _require(process.wait(timeout=15) == 0, "coordinator shutdown failed")
        process = start()

    try:
        process = start()
        for role in ("a", "b"):
            _ready(transports[role])
        report = scenario(
            transports["a"],
            transports["b"],
            transports["coordinator"],
            restart,
            worker_crash=worker_crash,
        )
        passed = True
        return report
    finally:
        failures = []
        for role in ("a", "b"):
            try:
                call(transports[role], "finish", passed=passed)
            except (OSError, api.Refused):
                failures.append(role)
        if process is not None and process.poll() is None:
            try:
                call(transports["coordinator"], "stop")
                process.wait(timeout=15)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        log.close()
        _require(not failures, "worker shutdown was not acknowledged")


def main() -> int:
    """Run one explicit qualification stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bootstrap", "discover", "node", "service"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--address")
    parser.add_argument("--worker-crash", action="store_true")
    args = parser.parse_args()
    report: dict = {
        "status": "failed",
        "phase": args.mode,
        "role": args.role,
        "binding": _binding(),
        "python": platform.python_version(),
    }
    try:
        if args.mode == "bootstrap":
            bootstrap(args.root, args.bundle, args.role, args.address)
            report["status"] = "pending"
            return 0
        if args.mode == "discover":
            local = json.loads((args.root / "public" / f"{args.role}.json").read_text("utf-8"))
            report.update(profile=local["profile"], cpu_vendor=local["cpu_vendor"])
            discover(args.root, args.role)
            report["status"] = "pending"
            return 0
        peers = json.loads((args.root / "peers.json").read_text("utf-8"))
        report.update(
            profile=peers[args.role]["profile"],
            cpu_vendor=peers[args.role]["cpu_vendor"],
            distinct_boots=len({p["boot"] for p in peers.values()}),
        )
        report["phase"] = "admission"
        validate_peers(peers, _binding())
        validate_local(args.bundle, args.role, peers[args.role])
        if args.mode == "service":
            return 0 if serve(args.root, args.bundle, args.role, peers) else 1
        report["phase"] = "native"
        if args.role == "coordinator":
            report.update(
                orchestrate(args.root, args.bundle, peers, worker_crash=args.worker_crash)
            )
        else:
            _require(
                serve(args.root, args.bundle, args.role, peers),
                "coordinator did not confirm successful completion",
            )
        report["status"] = "passed"
        return 0
    except RuntimeError as error:
        if report["phase"] != "native":
            report["error"] = str(error)[:1024]
        raise
    finally:
        if args.mode != "service":
            _save(args.root / "report.json", report)


if __name__ == "__main__":
    raise SystemExit(main())
