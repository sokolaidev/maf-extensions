"""Measure saved OAuth reference authority after an orderly, retained-state gateway restart."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import ssl
import time
import uuid

from openshell_composition_probe import receipt_installed
from openshell_credential_lifecycle import (
    HTTPSRefreshProfile,
    OriginJournal,
    ProviderCleanup,
    generation_interruption,
    protobuf_evidence,
    unexpired,
)
from openshell_credentials_probe import credential_observation
from openshell_egress_probe import origin_get
from openshell_https_probe import control_https
from openshell_owned_probe import fixture_spec, snapshot
from openshell_refresh_probe import TokenIssuer
from openshell_replay_probe import wait_gateway
from openshell_restart_probe import (
    RestartAuthority,
    authority_identity,
    boundary_descriptor,
    fresh_authority,
)
from openshell_transition_probe import runtime_continuity


def reference_program(key, path, *, capture=False, url=None):
    """Persist only an opaque guest reference; emit its digest and a bounded HTTP result."""
    return f"""
import hashlib, json, os, urllib.request, urllib.error
from pathlib import Path
path = {path!r}
url = {url!r}
if {capture!r}:
    value = os.environ.get({key!r}, '')
    if not value.startswith('openshell:resolve:'):
        raise RuntimeError('missing opaque launch reference')
    with open(path, 'x') as target:
        target.write(value)
value = Path(path).read_text() if path else ''
if path and not value.startswith('openshell:resolve:'):
    raise RuntimeError('saved reference is not opaque')
result = {{'reference_sha256': hashlib.sha256(value.encode()).hexdigest()}}
if url is not None:
    headers = {{'Authorization': 'Bearer ' + value}} if path else {{}}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:
            result['guest'] = {{'status': response.status, 'body': response.read(4096).decode()}}
    except urllib.error.HTTPError as exc:
        result['guest'] = {{'status': exc.code, 'body': exc.read(4096).decode()}}
    except Exception as exc:
        result['guest'] = {{'error': type(exc).__name__, 'errno': getattr(getattr(exc, 'reason', exc), 'errno', None)}}
print(json.dumps(result))
"""


def fingerprint_rows(value):
    """Inherited authority observers expose protobuf identity without retaining field values."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.endswith("_hex"):
                payload = bytes.fromhex(item)
                result[key[:-4] + "_sha256"] = hashlib.sha256(payload).hexdigest()
                result[key[:-4] + "_size"] = len(payload)
            else:
                result[key] = fingerprint_rows(item)
        return result
    if isinstance(value, list):
        return [fingerprint_rows(item) for item in value]
    return value


def classify(observed, events, request, mints):
    """Accept only an exact issued token at the origin or a specific credential refusal."""
    for mint in mints:
        if mint.get("mode") == "ok" and credential_observation(
            observed, events, 200, {**request, "authorization_sha256": mint["authorization_sha256"]}
        ):
            unexpired(mint)
            return mint["authorization_sha256"]
    for refusal in ("credential_unavailable", "credential_endpoint_mismatch"):
        if credential_observation(observed, events, refusal, request):
            return refusal
    if request["authorization_sha256"] is None and credential_observation(
        observed, events, 200, request
    ):
        return "anonymous"
    raise RuntimeError("unattributed credential recovery traffic")


class CredentialRecovery(RestartAuthority):
    """Restore the same owned policy and bind credential traffic to replacement authority."""

    def submit(self, method, request):
        row = {"method": method, "state": "pending", **protobuf_evidence(request, "request")}
        self.record("mutations", row)
        response = self.client.rpc(method, request)
        self.record(
            "mutations",
            {
                "method": method,
                "state": "acknowledged",
                **protobuf_evidence(request, "request"),
                **protobuf_evidence(response, "response"),
            },
        )
        return response

    def current_status(self):
        self.current()
        status = self.client.rpc(
            "GetSandboxProviderStatus",
            self.client.pb.GetSandboxProviderStatusRequest(
                sandbox=self.name, provider=self.profile.provider, workspace_scope=self.client.scope
            ),
        ).status
        if (status.receipt.workspace, status.receipt.provider) != (
            self.client.scope.workspace,
            self.profile.provider,
        ) or (
            status.receipt.desired.sandbox_id,
            status.receipt.desired.sandbox,
            status.receipt.desired.provider_id,
        ) != (self.identity, self.name, self.profile.provider_id):
            raise RuntimeError("current credential authority identity mismatch")
        return status

    def rotation_target(self, receipt):
        """Pin the new environment target without treating the superseded receipt as installed."""
        deadline = time.monotonic() + 30
        while True:
            status = self.current_status()
            target, previous = status.receipt.desired, receipt.desired
            unchanged = (
                "sandbox_id",
                "sandbox",
                "provider_id",
                "attachment_epoch",
                "config_revision",
                "policy_hash",
            )
            if any(getattr(target, field) != getattr(previous, field) for field in unchanged):
                raise RuntimeError("rotation changed authority outside the provider environment")
            if (
                target.provider_env_revision
                and target.provider_env_revision != previous.provider_env_revision
            ):
                self.record(
                    "barriers",
                    {
                        "rotation_target": protobuf_evidence(target, "desired"),
                        "previous_env_revision": previous.provider_env_revision,
                        "current_env_revision": target.provider_env_revision,
                    },
                )
                return target
            if time.monotonic() >= deadline:
                raise RuntimeError("rotation environment target did not change")
            time.sleep(0.1)

    def attest_current(self, target, before, runtime, spec, accepted, *, boundary):
        """Bracket traffic against one pinned reconstructed target and advancing runtime reports."""
        deadline = time.monotonic() + 30
        while True:
            status = self.current_status()
            self.record(
                "attestations",
                {
                    **protobuf_evidence(status, "status"),
                    "state": status.state,
                    "authority_identity": list(authority_identity(status)),
                    "accepted_identity": list(authority_identity(accepted)),
                    "sequence": status.observed.sequence,
                    "accepted_sequence": accepted.observed.sequence,
                },
            )
            if status.receipt.desired != target:
                raise RuntimeError("current credential target changed during observation")
            self.stable(runtime, spec)
            if boundary_descriptor(self.identity, runtime) != boundary:
                raise RuntimeError("replacement boundary descriptor changed")
            identity, previous = authority_identity(status), authority_identity(accepted)
            pending_process = (
                status.state == self.client.pb.PROVIDER_READINESS_STATE_PENDING
                and identity[:2] == previous[:2]
                and not identity[2]
                and not status.observed.launch_environment_installed
            )
            if identity != previous and not pending_process:
                raise RuntimeError("replacement authority identity changed")
            if receipt_installed(
                status, status.receipt, self.client.pb.PROVIDER_READINESS_STATE_READY
            ) and fresh_authority(
                before, status, accepted, boundary_session=boundary["session_id"]
            ):
                return status
            if time.monotonic() >= deadline:
                raise RuntimeError("current credential target installation unconfirmed")
            time.sleep(0.1)

    def replace(self, gateway, receipt):
        before = self.wait_provider(receipt, True)
        runtime = snapshot(self.identity)
        boundary = boundary_descriptor(self.identity, runtime)
        if before.observed.process_instance_id != boundary["session_id"]:
            raise RuntimeError("original authority does not match its runtime descriptor")
        spec = self.current().spec
        expected = spec.SerializeToString(deterministic=True)
        row = {
            "before": runtime,
            "before_boundary": boundary,
            **protobuf_evidence(spec, "spec"),
            **protobuf_evidence(before, "before_status"),
            "completed": False,
            "graceful_shutdown": False,
        }
        self.record("restarts", row)
        try:
            if gateway.process.poll() is not None:
                raise RuntimeError("gateway exited before requested shutdown")
            gateway.stop()
            row["stopped_returncode"] = gateway.process.poll()
            row["graceful_shutdown"] = row["stopped_returncode"] == 0
            if not row["graceful_shutdown"]:
                raise RuntimeError("gateway shutdown was not graceful")
        finally:
            self.record("restarts", dict(row))
            gateway.start()
            wait_gateway(self.client)
        if self.current().spec.SerializeToString(deterministic=True) != expected:
            raise RuntimeError("restart changed sandbox intent")
        after = snapshot(self.identity)
        continuity = runtime_continuity(self.identity, runtime, after)
        row.update(after=after, runtime_continuity=continuity)
        self.record("restarts", dict(row))
        if continuity:
            raise RuntimeError("credential recovery requires verified runtime replacement")
        boundary = boundary_descriptor(self.identity, after)
        row.update(
            replacement_boundary=boundary,
            original_processes_retired=True,
            original_supervisor_absent=True,
        )
        self.record("restarts", dict(row))
        retained = self.client.rpc(
            "GetSandboxProviderStatus",
            self.client.pb.GetSandboxProviderStatusRequest(
                sandbox=self.name,
                provider=self.profile.provider,
                workspace_scope=self.client.scope,
                receipt_id=receipt.receipt_id,
            ),
        ).status
        self.record(
            "barriers",
            {
                "retained_receipt_state": self.client.pb.ProviderReadinessState.Name(
                    retained.state
                ),
                **protobuf_evidence(retained, "status"),
            },
        )
        request = self.client.pb.UpdateConfigRequest(
            sandbox=self.name,
            workspace_scope=self.client.scope,
            expected_resource_version=self.current().metadata.resource_version,
            request_id=str(uuid.uuid4()),
            policy=spec.policy,
        )
        self.wait_policy(self.submit("UpdateConfig", request))
        restored = self.provider(True)
        if not restored.receipt_id or restored.receipt_id == receipt.receipt_id:
            raise RuntimeError("restoration requires a new provider receipt")
        restored_spec = self.current().spec.SerializeToString(deterministic=True).hex()
        accepted = self.attest(restored, True, before, after, restored_spec, boundary=boundary)
        row["completed"] = True
        self.record("restarts", dict(row))
        return restored, before, after, restored_spec, boundary, accepted


async def measure_credential_recovery(fixture, ip, ca_file, gateway):
    """Use fresh request processes and guest-saved references across one verified replacement."""
    evidence = fixture.report["credential_recovery"] = {
        "scope": "synthetic OAuth; saved guest references; explicit restoration after orderly gateway restart",
        "cases": [],
        "controls": [],
        "mutations": [],
        "barriers": [],
        "restarts": [],
        "attestations": [],
        "references": {},
        "characterization_completed": False,
        "qualification_passed": False,
        "provider_and_profile_cleanup_verified": False,
        "unqualified": [
            "continuous process or transport survival",
            "automatic recovery without intent resubmission",
            "crash, database loss and network partitions",
            "persistent/in-flight traffic",
            "refresh-token reauthorization and production identity",
            "backend adoption",
        ],
    }

    def record(kind, row):
        evidence[kind].append(fingerprint_rows(row))
        fixture.save()

    host = "allowed." + ip + ".nip.io"
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, 8443, socket.AF_INET)
    if {address[4][0] for address in addresses} != {ip}:
        raise RuntimeError("credential origin DNS did not resolve to the owned origin")
    context = ssl.create_default_context(cafile=str(ca_file))
    origin = OriginJournal()

    async def reconcile(*, expected=None):
        events = await asyncio.to_thread(origin_get, ip, "/__tls_events")
        evidence["origin_journal"] = events
        fixture.save()
        return origin.advance(events, expected=expected)

    await reconcile(expected=[])
    for path in ("/provider/ok", "/outside"):
        control = await asyncio.to_thread(control_https, host, path, port=8443, context=context)
        expected = {
            "method": "GET",
            "path": path,
            "host": host + ":8443",
            "port": 8443,
            "authorization_sha256": None,
        }
        if control != expected:
            raise RuntimeError("owned TLS positive control mismatch")
        record("controls", control)
        await reconcile(expected=[control])

    with TokenIssuer() as issuer:
        profile = HTTPSRefreshProfile(issuer)
        profile.ip, profile.provider_host = ip, host
        cleanup = None
        try:
            await asyncio.to_thread(profile.prepare_workspace, fixture.client)
            evidence["provider_prepared"] = True
            policy = fixture_spec(fixture.client.image).policy
            rule = policy.network_policies["credential_control"]
            rule.name = "credential_control"
            rule.binaries.add(path="/usr/local/bin/python*")
            profile.endpoint(rule, host, "/**")
            sandbox = await fixture.acquire(policy=policy)
            authority = CredentialRecovery(fixture, profile, ip, record)
            cleanup = ProviderCleanup(sandbox, authority)
            interruption_owner = sandbox.owner
            receipt = await asyncio.to_thread(authority.provider, True)
            replacement = None
            rotation_target = None
            attached = True
            prefix = "/sandbox/recovery-" + uuid.uuid4().hex
            paths = {}
            guarded_mints = []

            async def execute(code):
                result = await sandbox.exec(
                    ["python", "-c", code], working_directory="/sandbox", timeout=15
                )
                if result.exit_code or result.stderr:
                    raise RuntimeError("credential recovery guest observation failed")
                return json.loads(result.stdout)

            async def capture(slot):
                paths[slot] = prefix + "-" + slot
                result = await execute(
                    reference_program(profile.env_key, paths[slot], capture=True)
                )
                digest = result.get("reference_sha256", "")
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise RuntimeError("invalid reference fingerprint")
                evidence["references"][slot] = digest
                fixture.save()
                return digest

            async def mint(after=0):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    value = issuer.snapshot()
                    status = await asyncio.to_thread(profile.refresh_status, fixture.client)
                    expires = status.expiration_time.seconds + status.expiration_time.nanos / 1e9
                    if (
                        value.get("generation", 0) > after
                        and status.status == "refreshed"
                        and abs(expires - value["expires_at"]) < 2
                    ):
                        unexpired(value)
                        guarded_mints.append(value)
                        record("barriers", {"mint": value, **protobuf_evidence(status, "status")})
                        return value
                    await asyncio.sleep(0.2)
                raise RuntimeError("issuer and refresh status did not converge")

            async def attest():
                if replacement is None:
                    await asyncio.to_thread(authority.current)
                else:
                    before, runtime, spec, boundary, accepted = replacement
                    if rotation_target is not None and attached:
                        replacement[-1] = await asyncio.to_thread(
                            authority.attest_current,
                            rotation_target,
                            before,
                            runtime,
                            spec,
                            accepted,
                            boundary=boundary,
                        )
                    else:
                        replacement[-1] = await asyncio.to_thread(
                            authority.attest,
                            receipt,
                            attached,
                            before,
                            runtime,
                            spec,
                            accepted,
                            boundary=boundary,
                        )

            async def check(label, slot, expected, *, path="/provider/ok", converge=False):
                row = {"case": label, "attempts": [], "passed": False}
                evidence["cases"].append(row)
                fixture.save()
                deadline = time.monotonic() + 30
                while True:
                    for value in guarded_mints:
                        unexpired(value)
                    await attest()
                    await reconcile(expected=[])
                    target = path + "?case=" + uuid.uuid4().hex
                    started = time.time_ns()
                    observed = await execute(
                        reference_program(
                            profile.env_key, paths.get(slot), url=f"https://{host}:8443{target}"
                        )
                    )
                    ended = time.time_ns()
                    events = await reconcile()
                    attempt = {
                        "observation": observed,
                        "origin_events": events,
                        "matched": False,
                        "started_unix_ns": started,
                        "ended_unix_ns": ended,
                    }
                    row["attempts"].append(attempt)
                    fixture.save()
                    digest = (
                        evidence["references"][slot] if slot else hashlib.sha256(b"").hexdigest()
                    )
                    if observed.get("reference_sha256") != digest:
                        raise RuntimeError("saved credential reference changed")
                    for value in guarded_mints:
                        unexpired(value)
                    request = {
                        "method": "GET",
                        "path": target,
                        "host": host + ":8443",
                        "port": 8443,
                        "authorization_sha256": "" if slot else None,
                    }
                    try:
                        actual = classify(observed["guest"], events, request, issuer.journal())
                    except RuntimeError:
                        if (
                            not converge
                            or events
                            or observed["guest"] != {"error": "RemoteDisconnected", "errno": None}
                        ):
                            raise
                        closures = await asyncio.to_thread(
                            generation_interruption, interruption_owner, host, started, ended
                        )
                        attempt["generation_interruptions"] = closures
                        fixture.save()
                        if len(closures) != 1:
                            raise
                        actual = "generation_interruption"
                    attempt["result"] = actual
                    # Two advancing post-traffic reports exclude a cached pre-traffic receipt.
                    await attest()
                    await attest()
                    timely = time.monotonic() <= deadline
                    attempt["matched"] = actual in expected and timely
                    row["passed"] = attempt["matched"]
                    fixture.save()
                    if row["passed"]:
                        return actual
                    if not converge or not timely:
                        raise RuntimeError("credential recovery case failed: " + label)
                    await asyncio.sleep(0.2)

            initial = await mint()
            await capture("retired")
            await check("initial_reference", "retired", [initial["authorization_sha256"]])
            await asyncio.to_thread(profile.configure_refresh, fixture.client)
            await asyncio.to_thread(profile.rotate, fixture.client)
            current = await mint(initial["generation"])
            await check(
                "reconfiguration_retires_old_reference",
                "retired",
                ["credential_unavailable"],
                converge=True,
            )
            await capture("saved")
            if evidence["references"]["saved"] == evidence["references"]["retired"]:
                raise RuntimeError("reconfiguration did not change reference epoch")
            await check(
                "saved_reference_before_restart",
                "saved",
                [current["authorization_sha256"]],
                converge=True,
            )
            receipt = await asyncio.to_thread(authority.provider, True)
            receipt, before, runtime, spec, boundary, accepted = await asyncio.to_thread(
                authority.replace, gateway, receipt
            )
            replacement = [before, runtime, spec, boundary, accepted]
            interruption_owner = {
                "sandbox_id": sandbox.owner["sandbox_id"],
                "containers": [
                    container["Id"]
                    for container in runtime["containers"]
                    if container["Labels"].get("openshell.ai/isolation-role") == "supervisor"
                ],
            }
            recovered = await mint()
            await check(
                "saved_reference_after_restoration",
                "saved",
                [recovered["authorization_sha256"], "credential_unavailable"],
            )
            await check(
                "retired_reference_after_restoration", "retired", ["credential_unavailable"]
            )
            await capture("replacement")
            await check(
                "replacement_reference_reaches_origin",
                "replacement",
                [recovered["authorization_sha256"]],
                converge=True,
            )
            await check(
                "replacement_reference_confined",
                "replacement",
                ["credential_endpoint_mismatch"],
                path="/outside",
            )
            await check("no_implicit_credential", None, ["anonymous"])
            await asyncio.to_thread(profile.rotate, fixture.client)
            rotated = await mint(recovered["generation"])
            rotation_target = await asyncio.to_thread(authority.rotation_target, receipt)
            await check(
                "rotation_after_recovery",
                "replacement",
                [rotated["authorization_sha256"]],
                converge=True,
            )
            receipt = await cleanup.detach()
            attached = False
            replacement[2] = authority.current().spec.SerializeToString(deterministic=True).hex()
            await check("detached_saved_reference", "saved", ["credential_unavailable"])
            await check("detached_replacement_reference", "replacement", ["credential_unavailable"])
            await check("detached_base_route", None, ["anonymous"])
            await reconcile(expected=[])
            evidence["characterization_completed"] = True
            fixture.save()
        finally:
            try:
                if cleanup is not None:
                    await cleanup.dispose()
                else:
                    for owner in fixture.owners:
                        await fixture.dispose(owner)
            finally:
                try:
                    await asyncio.to_thread(profile.cleanup_workspace, fixture.client)
                    evidence["provider_and_profile_cleanup_verified"] = (
                        evidence.get("provider_prepared") is True
                    )
                finally:
                    evidence["issuer_events"] = issuer.journal()
                    fixture.save()
