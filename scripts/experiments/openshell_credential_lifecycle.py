"""Measure synthetic OAuth references in one continuous guest over owned, verified HTTPS."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
import ssl
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from openshell_allocation_probe import Client
from openshell_composition_probe import CompositionProfile, receipt_installed
from openshell_credential_transport import GUEST_HTTPS, measure_transport
from openshell_credentials_probe import credential_observation
from openshell_egress_probe import origin_get
from openshell_https_probe import control_https
from openshell_owned_probe import fixture_spec, snapshot
from openshell_refresh_probe import RefreshProfile, TokenIssuer
from openshell_sftp_probe import native_sftp
from openshell_transition_probe import Transitions, runtime_continuity

PROFILE_EDIT_PATHS = ("/provider/first", "/provider/second")


def guest_program(directory, env_key):
    """Keep the launch reference in memory; control files contain only bounded probe commands."""
    return f"""
import hashlib, json, os, ssl, time, uuid, urllib.request, urllib.error
from pathlib import Path
{GUEST_HTTPS}
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
        (directory / (command['source'] + '-reference')).read_text()
        if command['source'] in ('current', 'edited') else '')
    if token and not token.startswith('openshell:resolve:'):
        raise RuntimeError('expected opaque credential reference')
    headers = {{'Authorization': 'Bearer ' + token}} if token else {{}}
    try:
        if command['operation'] in ('prime', 'hold', 'reuse'):
            observed = transport_get(command['operation'], command['url'], headers)
        else:
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
        return await asyncio.shield(self.detachment)

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


def protobuf_evidence(message, label):
    """Retain payload identity and size without retaining serialized field values."""
    payload = message.SerializeToString()
    return {label + "_sha256": hashlib.sha256(payload).hexdigest(), label + "_size": len(payload)}


def current_provider_status(client, name, identity, profile):
    """Read the reconstructed provider target for exactly this owned sandbox and provider."""
    status = client.rpc(
        "GetSandboxProviderStatus",
        client.pb.GetSandboxProviderStatusRequest(
            sandbox=name, provider=profile.provider, workspace_scope=client.scope
        ),
    ).status
    receipt = status.receipt
    if (receipt.workspace, receipt.provider) != (client.scope.workspace, profile.provider) or (
        receipt.desired.sandbox_id,
        receipt.desired.sandbox,
        receipt.desired.provider_id,
    ) != (identity, name, profile.provider_id):
        raise RuntimeError("current credential authority identity mismatch")
    return status


def session_sample(client, owner, profile):
    """Record the gateway's connection identity and explicit reason for the owned provider."""
    status = current_provider_status(client, owner["name"], owner["sandbox_id"], profile)
    if (
        not status.receipt.receipt_id
        or status.receipt.kind != client.pb.PROVIDER_MUTATION_KIND_OBSERVE
    ):
        raise RuntimeError("missing supervisor session observation receipt")
    return {
        "time_unix_ns": time.time_ns(),
        "session": status.observed.session_id,
        "process": status.observed.process_instance_id,
        "network": status.network_instance_id,
        "sequence": status.observed.sequence,
        "installed": receipt_installed(
            status, status.receipt, client.pb.PROVIDER_READINESS_STATE_READY
        ),
        "reason": client.pb.ProviderReadinessReason.Name(status.reason),
    }


def session_outcome(before, after):
    """Identify a disconnected session followed by fresh installation reports after guest traffic."""
    first, last = before[0], before[-1]
    if not all(
        s["installed"]
        and all(s[k] for k in ("session", "network", "process"))
        and s["sequence"] > 0
        for s in (first, last, *after)
    ):
        return "installation_unconfirmed"
    if any(s[k] != first[k] for s in (last, *after) for k in ("process", "network")):
        return "runtime_identity_changed"
    pre_request_max = max(s["sequence"] for s in before if s["session"] == last["session"])
    sequence = last["sequence"]
    advances = 0
    for report in after:
        if report["session"] != last["session"] or report["sequence"] < sequence:
            return "session_changed_around_guest_request"
        if report["sequence"] > max(pre_request_max, sequence):
            advances += 1
        sequence = report["sequence"]
    disconnected = "PROVIDER_READINESS_REASON_SUPERVISOR_DISCONNECTED"
    gap = [
        i
        for i, s in enumerate(before[1:-1], 1)
        if s["reason"] == disconnected and not s["installed"]
    ]
    installed_sessions = {s["session"] for s in before if s["installed"]}
    if first["session"] == last["session"]:
        if len(installed_sessions) > 1:
            return "multiple_sessions_observed"
        return "disconnect_without_new_session" if gap else "no_disconnect_observed"
    if not gap:
        return "session_changed_without_observed_disconnect"
    if (
        any(s["installed"] and s["session"] != first["session"] for s in before[: gap[0]])
        or any(s["installed"] for s in before[gap[0] : gap[-1]])
        or any(s["installed"] and s["session"] != last["session"] for s in before[gap[-1] :])
        or any(
            s["installed"] and (s["network"], s["process"]) != (first["network"], first["process"])
            for s in before
        )
    ):
        return "multiple_sessions_observed"
    # The first advance read after traffic may already have been cached when the request began.
    return "reconnected" if advances >= 2 else "fresh_report_unconfirmed"


def observe_session_after_request(client, owner, profile, before, after):
    """Allow up to fifteen seconds for two new post-request reports on a reconnect candidate."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        after.append(session_sample(client, owner, profile))
        remaining = deadline - time.monotonic()
        if remaining < 0:
            return "post_request_window_elapsed"
        outcome = session_outcome(before, after)
        if outcome != "fresh_report_unconfirmed":
            return outcome
        time.sleep(min(1, remaining))
    return "fresh_report_unconfirmed"


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


def generation_closures(log, sandbox_id, host, started, ended):
    """Extract only exact, owned OCSF relay closures within one serial request interval."""
    signals = []
    for line in log.splitlines():
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{1,9})Z .*?OCSF-JSON (\{.*\})",
            line,
        )
        if not match:
            continue
        try:
            stamp = int(datetime.fromisoformat(match[1] + "+00:00").timestamp()) * 10**9
            stamp += int(match[2].ljust(9, "0"))
            event = json.loads(match[3])
            if not started <= stamp <= ended or not isinstance(event, dict):
                continue
            if (
                event.get("container", {}).get("uid") != sandbox_id
                or event.get("dst_endpoint", {}).get("domain") != host
                or event.get("dst_endpoint", {}).get("port") != 8443
            ):
                continue
            generation = re.fullmatch(
                r"L7 tunnel closed before inspection because policy changed: "
                r"policy generation is stale \[captured_generation:([0-9]{1,20}) "
                r"current_generation:([0-9]{1,20})\]",
                event.get("message", ""),
            )
        except (ValueError, TypeError, AttributeError):
            continue
        if generation and int(generation[2]) > int(generation[1]):
            signals.append(
                {
                    "signal": "relay_closed_for_policy_generation_change",
                    "time_unix_ns": stamp,
                    "captured_generation": int(generation[1]),
                    "current_generation": int(generation[2]),
                }
            )
    return signals


def generation_interruption(owner, host, started, ended):
    """Read one owned supervisor; raw logs never enter retained evidence or exception text."""
    current = snapshot(owner["sandbox_id"])
    supervisors = [
        c["Id"]
        for c in current["containers"]
        if c["Labels"].get("openshell.ai/isolation-role") == "supervisor"
    ]
    if len(supervisors) != 1 or supervisors[0] not in owner["containers"]:
        raise RuntimeError("supervisor identity changed during credential observation")
    try:
        result = subprocess.run(
            ["docker", "logs", "--timestamps", "--tail", "5000", supervisors[0]],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        raise RuntimeError("supervisor evidence unavailable: " + type(exc).__name__) from None
    if result.returncode:
        raise RuntimeError("supervisor evidence unavailable")
    return generation_closures(
        result.stdout + "\n" + result.stderr, owner["sandbox_id"], host, started, ended
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
        self.references = {}

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
        if source not in ("original", "current", "edited", "none"):
            raise ValueError("unknown credential reference source")
        await self.command(operation="request", url=url, source=source)
        return await self.request_result(source)

    async def request_result(self, source):
        reference = (
            self.ready["reference_sha256"]
            if source == "original"
            else self.references[source]
            if source in ("current", "edited")
            else hashlib.sha256(b"").hexdigest()
        )
        result = await self.read(f"result-{self.sequence}.json")
        if not continuous_result(self.ready, result, self.sequence, reference):
            raise RuntimeError("continuous process/reference identity mismatch")
        return result

    async def capture_current(self, slot="current"):
        # Exec launch observes the new epoch; the original process retains its old reference.
        if slot not in ("current", "edited"):
            raise ValueError("unknown credential reference slot")
        code = (
            "import os,json,hashlib; from pathlib import Path; v=os.environ.get("
            + repr(self.key)
            + ",''); assert v.startswith('openshell:resolve:'); Path("
            + repr(self.directory + "/" + slot + "-reference")
            + ").write_text(v); print(json.dumps({'reference_sha256':hashlib.sha256(v.encode()).hexdigest()}))"
        )
        result = await self.sandbox.exec(
            ["python", "-c", code], working_directory="/sandbox", timeout=20
        )
        if result.exit_code or result.stderr:
            raise RuntimeError("fresh launch reference observation failed")
        self.references[slot] = json.loads(result.stdout)["reference_sha256"]
        return self.references[slot]

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

    def edit_baseline(self, client):
        query = client.pb.GetProviderProfileRequest(
            workspace_scope=client.scope, id=self.profile_id
        )
        before = client.rpc("GetProviderProfile", query).profile
        if (
            before.id != self.profile_id
            or not before.resource_version
            or (before.source, before.scope) != ("user", "workspace")
            or len(before.endpoints) != 1
            or len(before.endpoints[0].rules) != 1
            or before.endpoints[0].path != self.endpoint_path
            or before.endpoints[0].rules[0].allow.path != self.endpoint_path
        ):
            raise RuntimeError("profile edit baseline mismatch")
        return query, before

    def path_edit_request(self, client, before, path):
        edited = type(before)()
        edited.CopyFrom(before)
        # Catalogue attribution is read-only and absent from mutation responses.
        edited.source = edited.scope = ""
        edited.endpoints[0].path = path
        edited.endpoints[0].rules[0].allow.path = path
        return client.pb.UpdateProviderProfilesRequest(
            workspace_scope=client.scope,
            id=self.profile_id,
            expected_resource_version=before.resource_version,
            profile=client.pb.ProviderProfileImportItem(profile=edited),
            request_id=str(uuid.uuid4()),
        )

    def update_path(self, client, path, record):
        """Persist a conditional path edit and verify it against the catalogue."""
        query, before = self.edit_baseline(client)
        request = self.path_edit_request(client, before, path)
        self.store_edit(client, query, before, request, record)

    def update_description(self, client, record):
        query, before = self.edit_baseline(client)
        request = self.path_edit_request(client, before, self.endpoint_path)
        request.profile.profile.description = before.description + " (transport observation)"
        self.store_edit(client, query, before, request, record)

    def store_edit(self, client, query, before, request, record):
        row = {
            "method": "UpdateProviderProfiles",
            **protobuf_evidence(request, "request"),
            "started_ns": time.monotonic_ns(),
            "state": "pending",
        }
        record("mutations", row)
        response = client.rpc("UpdateProviderProfiles", request)
        row.update(
            state="acknowledged",
            acknowledged_ns=time.monotonic_ns(),
            **protobuf_evidence(response, "response"),
        )
        record("barriers", {"profile_update_acknowledged": dict(row)})
        self.confirm_path_edit(client, query, before, request, response, record)

    def confirm_path_edit(self, client, query, before, request, response, record):
        if not response.updated or response.profile.resource_version <= before.resource_version:
            raise RuntimeError("profile edit was not stored at a newer revision")
        edited = type(before)()
        edited.CopyFrom(request.profile.profile)
        edited.resource_version = response.profile.resource_version
        if response.profile != edited:
            raise RuntimeError("profile update response differs from the exact profile edit")
        edited.source, edited.scope = before.source, before.scope
        current = client.rpc("GetProviderProfile", query).profile
        if current != edited:
            raise RuntimeError("stored profile differs from the exact profile edit")
        self.endpoint_path = current.endpoints[0].path
        record(
            "barriers",
            {
                "stored_profile_revision": current.resource_version,
                **protobuf_evidence(current, "profile"),
                "guest_installation_confirmed": False,
            },
        )

    def competing_path_edits(self, client, record):
        """Submit both fixture edits once and verify the committed winner."""
        query, before = self.edit_baseline(client)
        requests = [self.path_edit_request(client, before, path) for path in PROFILE_EDIT_PATHS]
        row = {
            "method": "UpdateProviderProfiles",
            "expected_resource_version": before.resource_version,
            "candidates": [
                {"path": path, **protobuf_evidence(request, "request")}
                for path, request in zip(PROFILE_EDIT_PATHS, requests, strict=True)
            ],
        }
        record("mutations", row)
        barrier = threading.Barrier(2, timeout=10)

        def submit(request):
            peer = Client(client.scope.workspace, client.token, client.image)
            try:
                barrier.wait()
                started = time.monotonic_ns()
                try:
                    response = peer.rpc("UpdateProviderProfiles", request)
                except peer.grpc.RpcError as exc:
                    outcome = {
                        "started_ns": started,
                        "ended_ns": time.monotonic_ns(),
                        "status": exc.code().name,
                    }
                    if outcome["status"] == "ABORTED":
                        match = re.fullmatch(
                            r"(?:provider profile was modified concurrently|update provider profile "
                            r"failed due to concurrent modification) \(current resource_version: (\d+)\)",
                            exc.details() or "",
                        )
                        if match:
                            outcome["conflicting_revision"] = int(match[1])
                    return None, outcome
                return response, {
                    "started_ns": started,
                    "ended_ns": time.monotonic_ns(),
                    "status": "OK",
                    **protobuf_evidence(response, "response"),
                }
            finally:
                peer.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, requests))
        outcomes = [
            {"path": path, **outcome}
            for path, (_, outcome) in zip(PROFILE_EDIT_PATHS, results, strict=True)
        ]
        overlap = min(o["ended_ns"] for o in outcomes) - max(o["started_ns"] for o in outcomes)
        record("barriers", {"profile_edit_outcomes": outcomes, "rpc_overlap_ns": overlap})
        if overlap <= 0 or sorted(o["status"] for o in outcomes) != ["ABORTED", "OK"]:
            raise RuntimeError(
                "expected overlapping profile edits with one success and one conflict"
            )
        winner = next(i for i, o in enumerate(outcomes) if o["status"] == "OK")
        response = results[winner][0]
        if (
            response is None
            or outcomes[1 - winner].get("conflicting_revision") != response.profile.resource_version
        ):
            raise RuntimeError("profile conflict did not identify the winning revision")
        self.confirm_path_edit(client, query, before, requests[winner], response, record)
        return outcomes[winner]["ended_ns"]


async def measure_credentials(fixture, ip, ca_file):
    """Retain propagation attempts and exact detach barriers without declaring attached identity."""
    evidence = fixture.report["credentials"] = {
        "scope": "one synthetic OAuth provider; fresh HTTPS requests and a held response across a description edit",
        "transport": [],
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
            "persistent connections and in-flight traffic across changes to credential authority",
            "profile credential declarations, refresh settings, methods and hosts",
            "additional controllers and concurrent edits beyond two conditional path updates",
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
    for path in ("/provider/ok", "/provider/other", "/outside", *PROFILE_EDIT_PATHS):
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

            async def minted(after=0, budget=15, *, sessions=None):
                next_sample = time.monotonic()
                deadline = time.monotonic() + budget
                while time.monotonic() < deadline:
                    if sessions is not None and time.monotonic() >= next_sample:
                        sessions.append(
                            await asyncio.to_thread(
                                session_sample, fixture.client, sandbox.owner, profile
                            )
                        )
                        next_sample = time.monotonic() + 1
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
                            {"mint": latest, **protobuf_evidence(status, "status")},
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
                prior_refusal=None,
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
                    started = time.monotonic_ns()
                    started_unix = time.time_ns()
                    observed = await guest.request(f"https://{host}:8443{target}", source)
                    ended_unix = time.time_ns()
                    events = await reconcile()
                    interruptions = []
                    if (
                        converge
                        and not events
                        and observed["guest"] == {"error": "RemoteDisconnected", "errno": None}
                    ):
                        interruptions = await asyncio.to_thread(
                            generation_interruption,
                            sandbox.owner,
                            host,
                            started_unix,
                            ended_unix,
                        )
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
                        {
                            "observation": observed,
                            "origin_events": events,
                            "matched": passed,
                            "started_ns": started,
                            "ended_ns": time.monotonic_ns(),
                            "started_unix_ns": started_unix,
                            "ended_unix_ns": ended_unix,
                            "generation_interruptions": interruptions,
                        }
                    )
                    timely = not converge or time.monotonic() < deadline
                    row["passed"] = passed and timely
                    fixture.save()
                    if not timely:
                        raise RuntimeError("unexpected continuous credential observation: " + label)
                    if passed:
                        return row["attempts"][-1]["ended_ns"]
                    if not (
                        propagation_observation(
                            observed["guest"], events, request, issuer.journal()
                        )
                        or len(interruptions) == 1
                        or prior_refusal is not None
                        and credential_observation(
                            observed["guest"], events, prior_refusal, request
                        )
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
            runtime = await asyncio.to_thread(snapshot, transitions.identity)
            sessions = [
                await asyncio.to_thread(session_sample, fixture.client, sandbox.owner, profile)
            ]
            window = evidence["worker_refresh_session_window"] = {
                "samples": sessions,
                "outcome": "incomplete",
            }
            fixture.save()
            issuer.configure(ttl=300)
            worker = await minted(short["generation"], budget=85, sessions=sessions)
            sessions.append(
                await asyncio.to_thread(session_sample, fixture.client, sandbox.owner, profile)
            )
            await check("worker_refresh_preserves_launch_reference", worker, converge=True)
            post_request = window["post_request_samples"] = []
            outcome = await asyncio.to_thread(
                observe_session_after_request,
                fixture.client,
                sandbox.owner,
                profile,
                sessions,
                post_request,
            )
            current_runtime = await asyncio.to_thread(snapshot, transitions.identity)
            same_runtime = await asyncio.to_thread(
                runtime_continuity, transitions.identity, runtime, current_runtime
            )
            window["same_physical_runtime"] = same_runtime
            window["outcome"] = outcome if same_runtime else "runtime_changed"
            fixture.save()

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
                {"retryable_failure": failed[-1], **protobuf_evidence(status, "status")},
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
            current = await guest.capture_current()
            await check(
                "reattach_new_reference_reaches_origin",
                reconfigured,
                source="current",
                converge=True,
            )
            unexpired(reconfigured)
            await asyncio.to_thread(profile.update_path, fixture.client, "/provider/other", record)
            await check(
                "profile_narrowing_revokes_removed_path",
                source="current",
                expected="credential_endpoint_mismatch",
                converge=True,
                revoked=reconfigured,
            )
            await check(
                "profile_narrowing_revokes_prior_reference_on_retained_path",
                source="current",
                expected="credential_unavailable",
                path="/provider/other",
                converge=True,
                revoked=reconfigured,
            )
            edited = await guest.capture_current("edited")
            if edited == current:
                raise RuntimeError("narrowed profile did not change the reference")
            record("barriers", {"pre_edit_reference": current, "narrowed_reference": edited})
            await check(
                "profile_narrowing_new_reference_reaches_retained_path",
                reconfigured,
                source="edited",
                path="/provider/other",
                converge=True,
                revoked=reconfigured,
            )
            await check(
                "profile_narrowing_new_reference_excludes_removed_path",
                source="edited",
                expected="credential_endpoint_mismatch",
                revoked=reconfigured,
            )
            await check("base_route_remains_after_profile_narrowing", source="none")
            unexpired(reconfigured)
            await asyncio.to_thread(profile.update_path, fixture.client, "/provider/**", record)
            await check(
                "profile_restoration_revives_same_current_reference",
                reconfigured,
                source="current",
                converge=True,
                revoked=reconfigured,
                prior_refusal="credential_endpoint_mismatch",
            )
            await check(
                "profile_restoration_revokes_narrowed_reference",
                source="edited",
                expected="credential_unavailable",
                path="/provider/other",
                revoked=reconfigured,
            )
            await check(
                "profile_restoration_keeps_outside_path_confined",
                source="current",
                path="/outside",
                expected="credential_endpoint_mismatch",
                revoked=reconfigured,
            )
            await check(
                "profile_restoration_does_not_revive_original_epoch",
                expected="credential_unavailable",
                revoked=recovered,
            )
            unexpired(reconfigured)
            acknowledged = await asyncio.to_thread(
                profile.competing_path_edits, fixture.client, record
            )
            prior_references = {fresh, current, edited, guest.ready["reference_sha256"]}
            deadline = time.monotonic() + 30
            while True:
                unexpired(reconfigured)
                reference = await guest.capture_current("edited")
                record(
                    "barriers",
                    {
                        "profile_edit_launch_reference": reference,
                        "observed_ns": time.monotonic_ns(),
                    },
                )
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "winning profile did not supply a new launch reference in time"
                    )
                if reference not in prior_references:
                    break
                await asyncio.sleep(0.2)
            observed = await check(
                "winning_profile_reference_reaches_owned_origin",
                reconfigured,
                source="edited",
                path=profile.endpoint_path,
                converge=True,
                revoked=reconfigured,
            )
            record(
                "barriers",
                {
                    "winning_path": profile.endpoint_path,
                    "acknowledged_ns": acknowledged,
                    "guest_and_origin_observed_ns": observed,
                    "ack_to_observation_ns": observed - acknowledged,
                },
            )
            unexpired(reconfigured)
            await measure_transport(
                guest, profile, fixture.client, reconfigured, ip, host, reconcile, record
            )
            unexpired(reconfigured)
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
