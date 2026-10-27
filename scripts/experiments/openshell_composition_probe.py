# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Measure anonymous provider authority and installed network-policy revisions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
import uuid
from importlib import import_module
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from openshell_egress_probe import guest_request, matches_observation, origin_get
from openshell_egress_probe import measure as run_matrix
from openshell_files_probe import execute, signed_gateway
from openshell_owned_probe import fixture_spec


def receipt_installed(status, receipt, expected_state):
    """Accept only the queried receipt and its exact installed authority, never a saved intent."""
    desired, observed = receipt.desired, status.observed
    return (
        status.receipt == receipt
        and status.state == expected_state
        and bool(observed.session_id)
        and bool(observed.process_instance_id)
        and observed.attachment_epoch == desired.attachment_epoch
        and observed.provider_env_revision == desired.provider_env_revision
        and observed.config_revision == desired.config_revision
        and observed.policy_hash == desired.policy_hash
        and observed.credentials_installed
        and observed.policy_active
        and observed.launch_environment_installed
    )


def revision_installed(status, response, loaded):
    """Bind the active policy acknowledgement to the submitted version and hash."""
    return (
        status.active_version == response.version
        and status.revision.version == response.version
        and status.revision.policy_hash == response.policy_hash
        and status.revision.status == loaded
    )


class CompositionProfile:
    """Own one anonymous provider and qualify its authority across serial policy changes."""

    MODES = ("composition",)
    CASES = 19
    SCOPE = "anonymous provider, distinct hosts, serial HTTP and policy revisions; not full composition conformance"

    def __init__(self):
        self.profile_id = "s1797-" + uuid.uuid4().hex[:12]
        self.provider = "s1797-" + uuid.uuid4().hex[:12]
        self.imported = False
        self.provider_id = None
        self.pb = import_module("openshell._proto.sandbox_pb2")

    def preflight(self, ip):
        self.ip = ip
        self.provider_host = "allowed." + ip + ".nip.io"
        self.base_host = "denied." + ip + ".nip.io"
        for host in (self.provider_host, self.base_host):
            for method in ("GET", "POST"):
                origin_get(ip, "/provider", method=method, host=host)

    def endpoint(self, target, host, path, methods=("GET",)):
        endpoint = target.endpoints.add(
            host=host, port=8000, protocol="rest", allowed_ips=[self.ip]
        )
        endpoint.enforcement = self.pb.NETWORK_ENFORCEMENT_MODE_ENFORCE
        for method in methods:
            endpoint.rules.add().allow.CopyFrom(self.pb.L7Allow(method=method, path=path))

    def build_profile(self, client):
        profile = client.pb.ProviderProfile(
            id=self.profile_id,
            display_name="Anonymous spike origin",
            category=client.pb.PROVIDER_PROFILE_CATEGORY_DATA,
        )
        profile.binaries.add(path="/usr/local/bin/python*")
        self.endpoint(profile, self.provider_host, "/provider")
        return profile

    def build_provider(self, client):
        dm = import_module("openshell._proto.datamodel_pb2")
        provider = dm.Provider(type=self.profile_id, profile_workspace=client.scope.workspace)
        provider.metadata.name = self.provider
        return provider

    def prepare_workspace(self, client):
        profile = self.build_profile(client)
        response = client.rpc(
            "ImportProviderProfiles",
            client.pb.ImportProviderProfilesRequest(
                workspace_scope=client.scope,
                profiles=[client.pb.ProviderProfileImportItem(profile=profile)],
                request_id=str(uuid.uuid4()),
            ),
        )
        self.imported = response.imported
        if (
            not self.imported
            or len(response.profiles) != 1
            or response.profiles[0].id != self.profile_id
        ):
            raise RuntimeError("profile import failed: " + str(response.diagnostics))
        provider = self.build_provider(client)
        created = client.rpc(
            "CreateProvider",
            client.pb.CreateProviderRequest(
                workspace_scope=client.scope, provider=provider, request_id=str(uuid.uuid4())
            ),
        )
        self.provider_id = created.provider.metadata.id
        if not self.provider_id:
            raise RuntimeError("provider identity missing")
        print(
            json.dumps(
                {
                    "provider_created": self.provider,
                    "provider_id": self.provider_id,
                    "profile": self.profile_id,
                    "credential_declarations": len(profile.credentials),
                }
            ),
            flush=True,
        )

    def configure(self, request, ip, mode):
        pass

    def cleanup_workspace(self, client):
        if self.provider_id:
            current = client.rpc(
                "GetProvider",
                client.pb.GetProviderRequest(name=self.provider, workspace_scope=client.scope),
            )
            if current.provider.metadata.id != self.provider_id:
                raise RuntimeError("refuse replacement provider cleanup")
            client.rpc(
                "DeleteProvider",
                client.pb.DeleteProviderRequest(
                    name=self.provider, workspace_scope=client.scope, request_id=str(uuid.uuid4())
                ),
            )
            self.require_absent(
                client,
                "GetProvider",
                client.pb.GetProviderRequest(name=self.provider, workspace_scope=client.scope),
            )
        if self.imported:
            client.rpc(
                "DeleteProviderProfile",
                client.pb.DeleteProviderProfileRequest(
                    id=self.profile_id, workspace_scope=client.scope, request_id=str(uuid.uuid4())
                ),
            )
            self.require_absent(
                client,
                "GetProviderProfile",
                client.pb.GetProviderProfileRequest(
                    id=self.profile_id, workspace_scope=client.scope
                ),
            )
        print(json.dumps({"provider_and_profile_cleanup_verified": True}), flush=True)

    @staticmethod
    def require_absent(client, method, request):
        try:
            client.rpc(method, request)
        except client.grpc.RpcError as exc:
            if exc.code() != client.grpc.StatusCode.NOT_FOUND:
                raise
        else:
            raise RuntimeError("resource remains: " + method)

    def run_cases(self, client, name, owner, ip, mode):
        return self._run_cases(client, name, owner, ip, mode)

    def _run_cases(self, client, name, owner, ip, mode, *, observe=None, record=None):
        results = []

        def retain(event):
            print(json.dumps(event), flush=True)
            if record is not None:
                record("transitions", event)

        def current():
            sandbox = client.rpc(
                "GetSandbox", client.pb.GetSandboxRequest(name=name, workspace_scope=client.scope)
            ).sandbox
            if sandbox.metadata.id != owner["id"]:
                raise RuntimeError("refuse replaced fixture")
            return sandbox

        def check(label, host, path, expected, method="GET"):
            current()
            if observe is not None:
                results.append(observe(label, host, path, expected, method))
                return
            marker = uuid.uuid4().hex
            url = f"http://{host}:8000{path}?case={marker}"
            status, output, error = execute(client, name, guest_request(url, method))
            if status != 0 or error:
                raise RuntimeError("guest composition observation failed")
            observed = json.loads(output)
            hits = [
                x
                for x in origin_get(ip, "/__events")
                if parse_qs(urlsplit(x["path"]).query).get("case") == [marker]
            ]
            request = {
                "method": method,
                "path": path + "?case=" + marker,
                "host": host + ":8000",
                "port": 8000,
            }
            passed = matches_observation(observed, hits, expected, int(expected == 200), request)
            row = {"case": label, "passed": passed, "guest": observed, "origin_hits": hits}
            results.append(row)
            print(json.dumps(row), flush=True)
            if not passed:
                raise RuntimeError("unexpected composition observation: " + label)

        def attach(attached):
            prefix = "Attach" if attached else "Detach"
            request_type = getattr(client.pb, prefix + "SandboxProviderRequest")
            response = client.rpc(
                prefix + "SandboxProvider",
                request_type(
                    sandbox=name,
                    provider=self.provider,
                    workspace_scope=client.scope,
                    expected_resource_version=current().metadata.resource_version,
                    request_id=str(uuid.uuid4()),
                ),
            )
            receipt = response.receipt
            if (
                receipt.desired.sandbox_id != owner["id"]
                or receipt.desired.provider_id != self.provider_id
                or not receipt.receipt_id
            ):
                raise RuntimeError("provider mutation identity mismatch")
            retain({"provider_intent": prefix, "receipt_hex": receipt.SerializeToString().hex()})
            expected = (
                client.pb.PROVIDER_READINESS_STATE_READY
                if attached
                else client.pb.PROVIDER_READINESS_STATE_REVOKED
            )
            status = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current()
                status = client.rpc(
                    "GetSandboxProviderStatus",
                    client.pb.GetSandboxProviderStatusRequest(
                        sandbox=name,
                        provider=self.provider,
                        receipt_id=receipt.receipt_id,
                        workspace_scope=client.scope,
                    ),
                ).status
                if receipt_installed(status, receipt, expected):
                    retain(
                        {
                            "provider_transition": prefix,
                            "receipt": receipt.receipt_id,
                            "state": client.pb.ProviderReadinessState.Name(status.state),
                            "attachment_epoch": receipt.desired.attachment_epoch,
                            "config_revision": receipt.desired.config_revision,
                            "policy_hash": receipt.desired.policy_hash,
                            "status_hex": status.SerializeToString().hex(),
                        }
                    )
                    return receipt
                time.sleep(0.1)
            raise RuntimeError("provider installation unconfirmed: " + str(status))

        def update(path, *, stale_version=None, host=None, methods=("GET",)):
            policy = fixture_spec(client.image).policy
            if path:
                rule = policy.network_policies["base_fixture"]
                rule.name = "base_fixture"
                rule.binaries.add(path="/usr/local/bin/python*")
                self.endpoint(rule, host or self.base_host, path, methods)
            request = client.pb.UpdateConfigRequest(
                sandbox=name,
                workspace_scope=client.scope,
                policy=policy,
                expected_resource_version=stale_version or current().metadata.resource_version,
                request_id=str(uuid.uuid4()),
            )
            if stale_version:
                try:
                    client.rpc("UpdateConfig", request)
                except client.grpc.RpcError as exc:
                    if exc.code() != client.grpc.StatusCode.ABORTED:
                        raise
                    retain({"stale_policy_update": "ABORTED"})
                    return
                raise RuntimeError("stale policy update accepted")
            response = client.rpc("UpdateConfig", request)
            retain(
                {
                    "policy_intent": response.version,
                    "request_hex": request.SerializeToString().hex(),
                    "response_hex": response.SerializeToString().hex(),
                }
            )
            status = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current()
                status = client.rpc(
                    "GetSandboxPolicyStatus",
                    client.pb.GetSandboxPolicyStatusRequest(
                        sandbox=name, workspace_scope=client.scope, version=response.version
                    ),
                )
                if revision_installed(status, response, client.pb.POLICY_STATUS_LOADED):
                    retain(
                        {
                            "policy_loaded": response.version,
                            "policy_hash": response.policy_hash,
                            "base_path": path,
                            "base_host": host or self.base_host,
                            "base_methods": list(methods),
                            "status_hex": status.SerializeToString().hex(),
                        }
                    )
                    return
                time.sleep(0.1)
            raise RuntimeError("policy installation unconfirmed: " + str(status))

        provider, base = self.provider_host, self.base_host
        check("initial_provider_closed", provider, "/provider", "EACCES")
        check("initial_base_closed", base, "/base", "EACCES")
        first = attach(True)
        check("attached_provider_get", provider, "/provider", 200)
        check("attached_provider_post_denied", provider, "/provider", 403, "POST")
        check("attached_provider_path_denied", provider, "/blocked", 403)
        check("attached_base_still_closed", base, "/base", "EACCES")
        if observe is not None:
            update("/provider", host=provider, methods=("POST",))
            check("overlap_retains_provider_get", provider, "/provider", 200)
            check("overlap_base_adds_post", provider, "/provider", 200, "POST")
            check("overlap_post_path_denied", provider, "/blocked", 403, "POST")
            check("overlap_distinct_host_closed", base, "/base", "EACCES")
            update(None)
            check("overlap_clear_retains_get", provider, "/provider", 200)
            check("overlap_clear_removes_post", provider, "/provider", 403, "POST")
        update("/base")
        stale = current().metadata.resource_version
        check("base_added", base, "/base", 200)
        check("provider_survives_base_add", provider, "/provider", 200)
        update("/next")
        check("base_old_path_denied", base, "/base", 403)
        check("base_new_path_allowed", base, "/next", 200)
        check("provider_survives_base_replace", provider, "/provider", 200)
        update("/base", stale_version=stale)
        check("stale_update_keeps_new_path", base, "/next", 200)
        check("stale_update_does_not_restore_old_path", base, "/base", 403)
        update(None)
        check("base_clear_closes_base_host", base, "/next", "EACCES")
        check("base_clear_retains_provider_authority", provider, "/provider", 200)
        attach(False)
        check("detach_closes_provider", provider, "/provider", "EACCES")
        check("detach_keeps_base_closed", base, "/next", "EACCES")
        second = attach(True)
        if (
            second.receipt_id == first.receipt_id
            or second.desired.attachment_epoch == first.desired.attachment_epoch
        ):
            raise RuntimeError("reattachment reused provider authority identity")
        old = client.rpc(
            "GetSandboxProviderStatus",
            client.pb.GetSandboxProviderStatusRequest(
                sandbox=name,
                provider=self.provider,
                receipt_id=first.receipt_id,
                workspace_scope=client.scope,
            ),
        ).status
        if old.state != client.pb.PROVIDER_READINESS_STATE_SUPERSEDED:
            raise RuntimeError("old provider receipt not superseded")
        retain(
            {
                "old_attachment_receipt": "SUPERSEDED",
                "new_attachment_identity": True,
                "status_hex": old.SerializeToString().hex(),
            }
        )
        check("reattach_restores_provider", provider, "/provider", 200)
        attach(False)
        check("final_detach_closes_provider", provider, "/provider", "EACCES")
        return results


def observe_native(client, name, ip, record, label, host, path, expected, method):
    """Require a direct positive control and the entire journal delta for each guest request."""
    from openshell_routes_probe import guest_code, raw_request, route_observation

    target = path + "?case=" + uuid.uuid4().hex
    request = {"method": method, "path": target, "host": host + ":8000", "port": 8000}
    wire = f"{method} {target} HTTP/1.1\r\nHost: {host}:8000\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    before = origin_get(ip, "/__events")
    control = raw_request(ip, 8000, wire)
    after = origin_get(ip, "/__events")
    control_row = {"case": label, "response": control, "origin_hits": after[len(before) :]}
    record("controls", control_row)
    if after[: len(before)] != before:
        raise RuntimeError("origin journal is not append-only")
    route_observation(control, control_row["origin_hits"], 200, request)
    before = after
    status, output, error = execute(client, name, guest_code(host, 8000, wire))
    after = origin_get(ip, "/__events")
    row = {
        "case": label,
        "expected": expected,
        "request": request,
        "wire_sha256": hashlib.sha256(wire.encode("ascii")).hexdigest(),
        "exit_code": status,
        "stdout": output.decode("utf-8", errors="replace"),
        "stderr": error.decode("utf-8", errors="replace"),
        "origin_hits": after[len(before) :],
        "observation": None,
        "passed": False,
    }
    record("cases", row)
    if status != 0 or error or after[: len(before)] != before:
        raise RuntimeError("guest execution or independent journal failed")
    row["guest"] = json.loads(output)
    row["observation"] = route_observation(row["guest"], row["origin_hits"], expected, request)
    row["passed"] = True
    return row


async def measure_native_composition(fixture, ip):
    """Retain serial anonymous provider composition evidence without qualifying egress modes."""
    from functools import partial

    from openshell_egress_probe import fixture_policy, native_origin_ip

    ip = native_origin_ip(ip)
    evidence = fixture.report["composition"] = {
        "scope": "one anonymous provider; serial IPv4 HTTP, distinct and overlapping endpoints",
        "origin_ip": ip,
        "cases": [],
        "controls": [],
        "transitions": [],
        "characterization_completed": False,
        "qualification_passed": False,
        "provider_and_profile_cleanup_verified": False,
        "unqualified": [
            "credentials and multiple providers",
            "profile edits and global overrides",
            "concurrent controllers and in-flight requests",
            "continuous guest process and persistent connections",
            "restart and reconnect",
            "HTTPS and broader protocols",
            "complete CLOSED/allowlist conformance and backend adoption",
        ],
    }
    fixture.save()

    def record(kind, row):
        evidence[kind].append(row)
        fixture.save()

    profile = CompositionProfile()
    try:
        await asyncio.to_thread(profile.preflight, ip)
        for host in (profile.provider_host, profile.base_host):
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, 8000, socket.AF_INET)
            if {address[4][0] for address in addresses} != {ip}:
                raise RuntimeError("fixture DNS did not resolve to the owned origin")
        evidence["profile"] = profile.profile_id
        evidence["provider"] = profile.provider
        evidence["provider_prepare_state"] = "pending"
        fixture.save()
        await asyncio.to_thread(profile.prepare_workspace, fixture.client)
        evidence["provider_prepare_state"] = "confirmed"
        evidence["provider_id"] = profile.provider_id
        fixture.save()
        await fixture.acquire(policy=fixture_policy(fixture.client.image, (), ip))
        owner = fixture.owners[-1]
        observe = partial(observe_native, fixture.client, owner["name"], ip, record)
        results = await asyncio.to_thread(
            profile._run_cases,
            fixture.client,
            owner["name"],
            {"id": owner["sandbox_id"]},
            ip,
            "composition",
            observe=observe,
            record=record,
        )
        if len(results) != 25 or not all(row["passed"] for row in results):
            raise RuntimeError("incomplete native provider matrix")
        evidence["characterization_completed"] = True
    finally:
        try:
            for owner in fixture.owners:
                await fixture.dispose(owner)
            await asyncio.to_thread(profile.cleanup_workspace, fixture.client)
            evidence["provider_and_profile_cleanup_verified"] = (
                evidence.get("provider_prepare_state") == "confirmed"
            )
        finally:
            fixture.save()


def main():
    """Run the explicitly selected composition experiment against the owned HTTP origin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--origin-ip")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if (
        not args.execute
        or not args.config
        or not args.image
        or not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image)
    ):
        parser.error("requires --execute, --config and immutable --image")
    ip = ipaddress.IPv4Address(args.origin_ip)
    if not ip.is_private or ip.is_loopback or ip.is_link_local:
        parser.error("requires owned private Docker origin")
    with signed_gateway(args.config) as issuer:
        report = run_matrix(args.image, issuer, str(ip), profile=CompositionProfile())
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
