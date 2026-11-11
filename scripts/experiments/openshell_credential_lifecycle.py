"""Measure synthetic OAuth references in one continuous guest over owned, verified HTTPS."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
import ssl
import time
import uuid

from openshell_composition_probe import CompositionProfile
from openshell_credentials_probe import credential_observation
from openshell_egress_probe import origin_get
from openshell_https_probe import control_https
from openshell_owned_probe import fixture_spec
from openshell_refresh_probe import RefreshProfile, TokenIssuer
from openshell_sftp_probe import native_sftp
from openshell_transition_probe import Transitions


def guest_program(directory, env_key):
    """Keep the launch reference in memory; control files contain only bounded probe commands."""
    return f"""
import hashlib, json, os, time, uuid, urllib.request, urllib.error
from pathlib import Path
directory = Path({directory!r})
directory.mkdir(exist_ok=True)
original = os.environ.get({env_key!r}, '')
if not original.startswith('openshell:resolve:'):
    raise RuntimeError('missing opaque launch reference')
identity = {{'pid': os.getpid(), 'nonce': uuid.uuid4().hex,
            'start': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]}}
def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()
def emit(name, value):
    temporary = directory / (name + '.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(directory / name)
emit('ready.json', {{'identity': identity, 'reference_sha256': digest(original)}})
deadline = time.monotonic() + 600
sequence = 1
while sequence <= 256 and time.monotonic() < deadline:
    try:
        command = json.loads((directory / f'command-{{sequence}}.json').read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        time.sleep(0.05)
        continue
    if command['sequence'] != sequence:
        raise RuntimeError('command sequence mismatch')
    if command['operation'] == 'stop':
        break
    token = original if command['source'] == 'original' else (
        (directory / 'current-reference').read_text() if command['source'] == 'current' else '')
    if token and not token.startswith('openshell:resolve:'):
        raise RuntimeError('expected opaque credential reference')
    headers = {{'Authorization': 'Bearer ' + token}} if token else {{}}
    try:
        with urllib.request.urlopen(urllib.request.Request(command['url'], headers=headers), timeout=5) as response:
            observed = {{'status': response.status, 'body': response.read(4096).decode()}}
    except urllib.error.HTTPError as exc:
        observed = {{'status': exc.code, 'body': exc.read(4096).decode()}}
    except Exception as exc:
        observed = {{'error': type(exc).__name__, 'errno': getattr(getattr(exc, 'reason', exc), 'errno', None)}}
    emit(f'result-{{sequence}}.json', {{'identity': identity, 'sequence': sequence,
         'reference_sha256': digest(token), 'guest': observed}})
    sequence += 1
else:
    raise RuntimeError('guest control budget exhausted')
"""


def continuous_result(ready, result, sequence, reference):
    """Reject replacement processes, replayed results and silently refreshed launch references."""
    identity = ready.get("identity", {})
    return (
        isinstance(identity.get("pid"), int)
        and identity["pid"] > 0
        and bool(identity.get("start"))
        and bool(identity.get("nonce"))
        and result.get("identity") == identity
        and result.get("sequence") == sequence
        and result.get("reference_sha256") == reference
    )


def journal_delta(before, after):
    """A lost origin journal cannot prove absence of a credential-bearing request."""
    if after[: len(before)] != before:
        raise RuntimeError("TLS origin journal continuity lost")
    return after[len(before) :]


class OriginJournal:
    """Account for every origin event across controls, traffic and idle measurement intervals."""

    def __init__(self):
        self.events = []

    def advance(self, snapshot, *, expected=None):
        events = journal_delta(self.events, snapshot)
        if expected is not None and events != expected:
            raise RuntimeError("unattributed TLS origin traffic between observations")
        self.events = snapshot
        return events


class ProviderCleanup:
    """Share one detach-before-delete operation between exec failure and experiment cleanup."""

    def __init__(self, sandbox, transitions):
        self.delete = sandbox.dispose
        self.transitions = transitions
        self.detachment = None
        self.disposal = None
        sandbox.dispose = self.dispose

    async def detach(self):
        if self.detachment is None:
            self.detachment = asyncio.create_task(
                asyncio.to_thread(self.transitions.provider, False)
            )
        await asyncio.shield(self.detachment)

    async def dispose(self):
        async def ordered():
            try:
                await self.detach()
            finally:
                await self.delete()

        if self.disposal is None:
            self.disposal = asyncio.create_task(ordered())
        await asyncio.shield(self.disposal)


def unexpired(mint, *, now=None):
    """Leave a full observation budget before expiry when attributing revocation."""
    if mint.get("expires_at", 0) <= (time.time() if now is None else now) + 35:
        raise RuntimeError("revocation observation is too close to token expiry")


def propagation_observation(observed, events, request, minted):
    """Only an attributed earlier token or an explicit credential refusal may converge later."""
    return credential_observation(observed, events, "credential_unavailable", request) or any(
        credential_observation(
            observed,
            events,
            200,
            {**request, "authorization_sha256": event["authorization_sha256"]},
        )
        for event in minted
        if event.get("mode") == "ok"
    )


class ControlFiles:
    """Keep an authenticated SFTP process alive for one exclusive fixture's command directory."""

    def __init__(self, sandbox, directory):
        if not re.fullmatch(r"/sandbox/credential-[0-9a-f]{32}", directory):
            raise ValueError("requires a fresh owned control directory")
        self.directory = directory
        self.context = native_sftp(
            sandbox.client, sandbox.owner["name"], sandbox.owner["sandbox_id"], seconds=620
        )
        self.session = self.context.__enter__()

    def path(self, path):
        if not path.startswith(self.directory + "/") or not re.fullmatch(
            r"(?:ready|command-[0-9]+|result-[0-9]+)\.json", path[len(self.directory) + 1 :]
        ):
            raise ValueError("file is outside the owned control protocol")
        return path.removeprefix("/sandbox/")

    async def read_file(self, path, *, working_directory, max_bytes):
        with self.session.open(self.path(path), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RuntimeError("control response exceeds its capture budget")
        return data

    async def write_file(self, path, content, *, working_directory):
        with self.session.open(self.path(path), "wb") as stream:
            stream.write(content.encode())

    def close(self):
        self.context.__exit__(None, None, None)


class ContinuousGuest:
    """Own one bounded exec stream and correlate each file-controlled request with its process."""

    def __init__(self, sandbox, key, *, record=None, persistent_files=False):
        self.sandbox, self.key = sandbox, key
        self.files = sandbox
        self.persistent_files = persistent_files
        self.record = record or (lambda row: None)
        self.directory = "/sandbox/credential-" + uuid.uuid4().hex
        self.sequence = 0
        self.task = None
        self.ready = {}
        self.current_reference = None

    async def control_io(self, operation, action):
        """Retry identical sequenced file operations, never guest execution or provider mutations."""
        deadline = time.monotonic() + 15
        while True:
            try:
                return await action()
            except (EOFError, ConnectionError) as exc:
                self.record(
                    {"operation": operation, "sequence": self.sequence, "error": type(exc).__name__}
                )
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.2)

    async def read(self, name):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.task is not None and self.task.done():
                result = await self.task
                raise RuntimeError("continuous guest exited before observation: " + result.stderr)
            try:
                data = await self.control_io(
                    "read",
                    lambda: self.files.read_file(
                        self.directory + "/" + name, working_directory="/sandbox", max_bytes=16384
                    ),
                )
                return json.loads(data)
            except FileNotFoundError:
                await asyncio.sleep(0.1)
        raise RuntimeError("continuous guest response deadline exceeded")

    async def start(self):
        if self.persistent_files:
            self.files = ControlFiles(self.sandbox, self.directory)
        self.task = asyncio.create_task(
            self.sandbox.exec(
                ["python", "-c", guest_program(self.directory, self.key)],
                working_directory="/sandbox",
                timeout=620,
            )
        )
        self.ready = await self.read("ready.json")
        return self.ready

    async def command(self, **fields):
        self.sequence += 1
        path = f"{self.directory}/command-{self.sequence}.json"
        content = json.dumps({"sequence": self.sequence, **fields})
        await self.control_io(
            "write", lambda: self.files.write_file(path, content, working_directory="/sandbox")
        )

    async def request(self, url, source="original"):
        await self.command(operation="request", url=url, source=source)
        result = await self.read(f"result-{self.sequence}.json")
        reference = (
            self.ready["reference_sha256"]
            if source == "original"
            else self.current_reference
            if source == "current"
            else hashlib.sha256(b"").hexdigest()
        )
        if not continuous_result(self.ready, result, self.sequence, reference):
            raise RuntimeError("continuous process/reference identity mismatch")
        return result

    async def capture_current(self):
        # Exec launch observes the new epoch; the original process retains its old reference.
        code = (
            "import os,json,hashlib; from pathlib import Path; v=os.environ.get("
            + repr(self.key)
            + ",''); assert v.startswith('openshell:resolve:'); Path("
            + repr(self.directory + "/current-reference")
            + ").write_text(v); print(json.dumps({'reference_sha256':hashlib.sha256(v.encode()).hexdigest()}))"
        )
        result = await self.sandbox.exec(
            ["python", "-c", code], working_directory="/sandbox", timeout=20
        )
        if result.exit_code or result.stderr:
            raise RuntimeError("fresh launch reference observation failed")
        self.current_reference = json.loads(result.stdout)["reference_sha256"]
        return self.current_reference

    async def close(self):
        if self.task is None:
            return
        try:
            if not self.task.done():
                await self.command(operation="stop")
            result = await asyncio.wait_for(asyncio.shield(self.task), 15)
            if result.exit_code or result.stderr:
                raise RuntimeError("continuous guest failed: " + result.stderr)
        finally:
            try:
                if not self.task.done():
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
            finally:
                if self.files is not self.sandbox:
                    self.files.close()
                    self.files = self.sandbox


class HTTPSRefreshProfile(RefreshProfile):
    """Restrict this experiment to one bearer credential and one verified TLS endpoint."""

    def endpoint(self, target, host, path, methods=("GET",)):
        endpoint = target.endpoints.add(
            host=host,
            port=8443,
            protocol="rest",
            allowed_ips=[self.ip],
            enforcement=self.pb.NETWORK_ENFORCEMENT_MODE_ENFORCE,
        )
        for method in methods:
            endpoint.rules.add().allow.CopyFrom(self.pb.L7Allow(method=method, path=path))

    def prepare_workspace(self, client):
        self.token_issuer.configure(ttl=300)
        CompositionProfile.prepare_workspace(self, client)
        self.configure_refresh(client)
        self.rotate(client)


async def measure_credentials(fixture, ip, ca_file):
    """Retain propagation attempts and exact detach barriers without declaring attached identity."""
    evidence = fixture.report["credentials"] = {
        "scope": "one synthetic OAuth client-credentials provider; continuous guest, fresh HTTPS connections",
        "cases": [],
        "barriers": [],
        "mutations": [],
        "control_retries": [],
        "issuer_events": [],
        "controls": [],
        "characterization_completed": False,
        "qualification_passed": False,
        "provider_and_profile_cleanup_verified": False,
        "unqualified": [
            "persistent connections and in-flight mutations",
            "provider profile edits",
            "concurrent controllers",
            "gateway/supervisor disconnect and restart",
            "refresh-token reauthorization",
            "co-minted credentials and non-bearer rewrites",
            "production identity and backend adoption",
        ],
    }
    fixture.save()
    host = "allowed." + ip + ".nip.io"
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, 8443, socket.AF_INET)
    if {address[4][0] for address in addresses} != {ip}:
        raise RuntimeError("credential origin DNS did not resolve to the owned origin")
    context = ssl.create_default_context(cafile=str(ca_file))
    origin = OriginJournal()

    async def reconcile(*, expected=None):
        snapshot = await asyncio.to_thread(origin_get, ip, "/__tls_events")
        evidence["origin_journal"] = snapshot
        fixture.save()
        return origin.advance(snapshot, expected=expected)

    await reconcile(expected=[])
    for path in ("/provider/ok", "/provider/other", "/outside"):
        row = await asyncio.to_thread(control_https, host, path, port=8443, context=context)
        if row != {
            "method": "GET",
            "path": path,
            "host": host + ":8443",
            "port": 8443,
            "authorization_sha256": None,
        }:
            raise RuntimeError("credential origin positive control failed")
        evidence["controls"].append(row)
        await reconcile(expected=[row])

    def record(kind, row):
        evidence[kind].append(row)
        fixture.save()

    with TokenIssuer() as issuer:
        profile = HTTPSRefreshProfile(issuer)
        profile.ip, profile.provider_host = ip, host
        profile.endpoint_path = "/provider/**"
        guest = None
        transitions = None
        cleanup = None
        try:
            evidence.update(
                profile=profile.profile_id, provider=profile.provider, prepare="pending"
            )
            fixture.save()
            await asyncio.to_thread(profile.prepare_workspace, fixture.client)
            evidence.update(prepare="confirmed", provider_id=profile.provider_id)
            policy = fixture_spec(fixture.client.image).policy
            rule = policy.network_policies["credential_control"]
            rule.name = "credential_control"
            rule.binaries.add(path="/usr/local/bin/python*")
            profile.endpoint(rule, host, "/**")
            sandbox = await fixture.acquire(policy=policy)
            transitions = Transitions(fixture, profile, ip, record)
            cleanup = ProviderCleanup(sandbox, transitions)
            await asyncio.to_thread(transitions.provider, True)
            guest = ContinuousGuest(
                sandbox,
                profile.env_key,
                record=lambda row: record("control_retries", row),
                persistent_files=True,
            )
            evidence["guest"] = await guest.start()
            fixture.save()

            async def minted(after=0, budget=15):
                deadline = time.monotonic() + budget
                while time.monotonic() < deadline:
                    latest = issuer.snapshot()
                    status = await asyncio.to_thread(profile.refresh_status, fixture.client)
                    expiration = status.expiration_time.seconds + status.expiration_time.nanos / 1e9
                    if (
                        latest.get("generation", 0) > after
                        and status.status == "refreshed"
                        and abs(expiration - latest["expires_at"]) < 2
                    ):
                        record(
                            "barriers",
                            {"mint": latest, "status_hex": status.SerializeToString().hex()},
                        )
                        return latest
                    await asyncio.sleep(0.2)
                raise RuntimeError("issuer mint and owned refresh status did not converge")

            async def check(
                label,
                mint=None,
                *,
                source="original",
                expected: int | str = 200,
                path="/provider/ok",
                converge=False,
                revoked=None,
            ):
                assert guest is not None
                row = {"case": label, "attempts": [], "passed": False}
                record("cases", row)
                deadline = time.monotonic() + (30 if converge else 0)
                while True:
                    if revoked is not None:
                        unexpired(revoked)
                    await asyncio.to_thread(transitions.current)
                    marker = uuid.uuid4().hex
                    target = path + "?case=" + marker
                    await reconcile(expected=[])
                    observed = await guest.request(f"https://{host}:8443{target}", source)
                    events = await reconcile()
                    if revoked is not None:
                        unexpired(revoked)
                    request = {
                        "method": "GET",
                        "path": target,
                        "host": host + ":8443",
                        "port": 8443,
                        "authorization_sha256": mint["authorization_sha256"] if mint else None,
                    }
                    passed = credential_observation(observed["guest"], events, expected, request)
                    row["attempts"].append(
                        {"observation": observed, "origin_events": events, "matched": passed}
                    )
                    row["passed"] = passed
                    fixture.save()
                    if passed:
                        return
                    if not propagation_observation(
                        observed["guest"], events, request, issuer.journal()
                    ):
                        raise RuntimeError(
                            "unattributed credential propagation observation: " + label
                        )
                    if time.monotonic() >= deadline:
                        raise RuntimeError("unexpected continuous credential observation: " + label)
                    await asyncio.sleep(0.2)

            initial = await minted()
            await check("initial_reference_reaches_owned_tls_origin", initial)
            await check("no_implicit_credential", source="none")
            await check(
                "reference_confined_to_profile",
                expected="credential_endpoint_mismatch",
                path="/outside",
            )
            issuer.configure(ttl=90)
            await asyncio.to_thread(profile.rotate, fixture.client)
            short = await minted(initial["generation"])
            await check("forced_rotation_preserves_launch_reference", short, converge=True)
            issuer.configure(ttl=300)
            worker = await minted(short["generation"], budget=85)
            await check("worker_refresh_preserves_launch_reference", worker, converge=True)

            issuer.configure(mode="temporarily_unavailable")
            try:
                await asyncio.to_thread(profile.rotate, fixture.client)
            except fixture.client.grpc.RpcError as exc:
                if exc.code() != fixture.client.grpc.StatusCode.UNAVAILABLE:
                    raise
            else:
                raise RuntimeError("retryable issuer failure was not reported")
            status = await asyncio.to_thread(profile.refresh_status, fixture.client)
            failed = issuer.journal()
            if (
                not failed
                or failed[-1]["mode"] != "temporarily_unavailable"
                or status.failure_code != "oauth_token_endpoint_retryable"
                or not status.HasField("next_refresh_time")
            ):
                raise RuntimeError("retryable failure and retry schedule were not observed")
            record(
                "barriers",
                {"retryable_failure": failed[-1], "status_hex": status.SerializeToString().hex()},
            )
            await check("current_value_survives_retryable_failure", worker)
            issuer.configure()
            # A scheduled retry can miss a sweep by almost one full worker interval.
            recovered = await minted(worker["generation"], budget=135)
            if recovered["at"] <= failed[-1]["at"]:
                raise RuntimeError("scheduled retry was not followed by a new issuer request")
            await check("automatic_retry_recovers_without_rotate", recovered, converge=True)

            unexpired(recovered)
            await asyncio.to_thread(profile.configure_refresh, fixture.client)
            await asyncio.to_thread(profile.rotate, fixture.client)
            reconfigured = await minted(recovered["generation"])
            await check(
                "reconfiguration_revokes_unexpired_launch_reference",
                expected="credential_unavailable",
                converge=True,
                revoked=recovered,
            )
            unexpired(recovered)
            fresh = await guest.capture_current()
            if fresh == guest.ready["reference_sha256"]:
                raise RuntimeError("reconfiguration did not advance the reference epoch")
            await check(
                "same_process_can_use_explicit_new_epoch",
                reconfigured,
                source="current",
                converge=True,
            )
            unexpired(reconfigured)
            await asyncio.to_thread(transitions.provider, False)
            await check(
                "installed_detach_revokes_current_reference",
                source="current",
                expected="credential_unavailable",
                revoked=reconfigured,
            )
            unexpired(reconfigured)
            await check("base_route_remains_without_credential_after_detach", source="none")
            await asyncio.to_thread(transitions.provider, True)
            await check(
                "reattach_does_not_revive_original_epoch",
                expected="credential_unavailable",
                revoked=recovered,
            )
            await guest.capture_current()
            await check(
                "reattach_new_reference_reaches_origin",
                reconfigured,
                source="current",
                converge=True,
            )
            await guest.close()
            guest = None
            await reconcile(expected=[])
            evidence["characterization_completed"] = True
        finally:
            try:
                try:
                    if cleanup is not None:
                        await cleanup.detach()
                finally:
                    if guest is not None:
                        await guest.close()
            finally:
                try:
                    try:
                        if cleanup is not None:
                            await cleanup.dispose()
                        else:
                            for owner in fixture.owners:
                                await fixture.dispose(owner)
                    finally:
                        await asyncio.to_thread(profile.cleanup_workspace, fixture.client)
                        evidence["provider_and_profile_cleanup_verified"] = (
                            evidence.get("prepare") == "confirmed"
                        )
                finally:
                    evidence["issuer_events"] = issuer.journal()
                    fixture.save()
