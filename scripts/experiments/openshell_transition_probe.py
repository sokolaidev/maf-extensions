"""Characterize competing egress mutations and retained-state gateway restarts."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from openshell_allocation_probe import Client
from openshell_composition_probe import (
    CompositionProfile,
    observe_native,
    receipt_installed,
    revision_installed,
)
from openshell_egress_probe import fixture_policy, native_origin_ip
from openshell_owned_probe import snapshot
from openshell_replay_probe import wait_gateway


def competing_calls(client, requests, schedule, record):
    """Retain both outcomes; only an explicit ABORTED permits a new mutation intent."""
    if schedule not in ("policy_first", "detach_first", "concurrent"):
        raise ValueError("unknown mutation schedule")
    if (
        len(requests) != 2
        or not requests[0][1].expected_resource_version
        or requests[0][1].expected_resource_version != requests[1][1].expected_resource_version
    ):
        raise ValueError("competing intents require the same nonzero resource version")
    row = {
        "schedule": schedule,
        "requests": [
            {"method": method, "request_hex": request.SerializeToString().hex()}
            for method, request in requests
        ],
        "outcomes": [],
        "passed": False,
    }
    record("races", row)
    barrier = threading.Barrier(2, timeout=10)

    def call(item):
        method, request = item
        peer = None
        started = time.monotonic_ns()
        try:
            peer = Client(client.scope.workspace, client.token, client.image)
            if schedule == "concurrent":
                barrier.wait()
            started = time.monotonic_ns()
            response = peer.rpc(method, request)
            return response, {
                "method": method,
                "status": "OK",
                "started_ns": started,
                "ended_ns": time.monotonic_ns(),
                "response_hex": response.SerializeToString().hex(),
            }
        except Exception as exc:
            status = (
                getattr(exc, "code")().name
                if isinstance(exc, client.grpc.RpcError)
                else type(exc).__name__
            )
            return None, {
                "method": method,
                "status": status,
                "started_ns": started,
                "ended_ns": time.monotonic_ns(),
                "detail": str(exc),
            }
        finally:
            if peer is not None:
                peer.close()

    if schedule == "concurrent":
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, item) for item in requests]
            results = [future.result() for future in futures]
    else:
        ordered = requests if schedule == "policy_first" else list(reversed(requests))
        results = [call(item) for item in ordered]
    row["outcomes"] = [outcome for _, outcome in results]
    statuses = sorted(outcome["status"] for _, outcome in results)
    if statuses != ["ABORTED", "OK"]:
        raise RuntimeError(
            "competing intents did not produce one success and one explicit conflict"
        )
    if schedule != "concurrent" and row["outcomes"][0]["status"] != "OK":
        raise RuntimeError("ordered control did not accept its first intent")
    if schedule == "concurrent":
        first, second = row["outcomes"]
        row["rpc_intervals_overlap"] = max(first["started_ns"], second["started_ns"]) < min(
            first["ended_ns"], second["ended_ns"]
        )
        if not row["rpc_intervals_overlap"]:
            raise RuntimeError("concurrent RPC intervals did not overlap")
    row["passed"] = True
    return {outcome["method"]: response for response, outcome in results}


class Transitions:
    """Bind mutation and observation barriers to one exclusively owned physical sandbox."""

    def __init__(self, fixture, profile, ip, record):
        self.fixture, self.client, self.profile = fixture, fixture.client, profile
        self.owner = fixture.owners[-1]
        self.name, self.identity = self.owner["name"], self.owner["sandbox_id"]
        self.ip, self.record = ip, record

    def current(self):
        client = self.client
        sandbox = client.rpc(
            "GetSandbox", client.pb.GetSandboxRequest(name=self.name, workspace_scope=client.scope)
        ).sandbox
        if sandbox.metadata.id != self.identity:
            raise RuntimeError("refuse replaced transition fixture")
        return sandbox

    def policy_request(self, version, enabled):
        policy = fixture_policy(self.client.image, (), self.ip)
        if enabled:
            rule = policy.network_policies["overlap"]
            rule.name = "overlap"
            rule.binaries.add(path="/usr/local/bin/python*")
            self.profile.endpoint(rule, self.profile.provider_host, "/provider", ("POST",))
        return self.client.pb.UpdateConfigRequest(
            sandbox=self.name,
            workspace_scope=self.client.scope,
            expected_resource_version=version,
            request_id=str(uuid.uuid4()),
            policy=policy,
        )

    def provider_request(self, version, attached):
        method = ("Attach" if attached else "Detach") + "SandboxProvider"
        request = getattr(self.client.pb, method + "Request")(
            sandbox=self.name,
            provider=self.profile.provider,
            workspace_scope=self.client.scope,
            expected_resource_version=version,
            request_id=str(uuid.uuid4()),
        )
        return method, request

    def submit(self, method, request):
        row = {
            "method": method,
            "request_hex": request.SerializeToString().hex(),
            "state": "pending",
        }
        self.record("mutations", row)
        response = self.client.rpc(method, request)
        row.update(state="acknowledged", response_hex=response.SerializeToString().hex())
        self.fixture.save()
        return response

    def wait_policy(self, response):
        client = self.client
        deadline = time.monotonic() + 30
        while True:
            self.current()
            status = client.rpc(
                "GetSandboxPolicyStatus",
                client.pb.GetSandboxPolicyStatusRequest(
                    sandbox=self.name, workspace_scope=client.scope, version=response.version
                ),
            )
            if revision_installed(status, response, client.pb.POLICY_STATUS_LOADED):
                self.record(
                    "barriers",
                    {
                        "policy_loaded": response.version,
                        "status_hex": status.SerializeToString().hex(),
                    },
                )
                return status
            if time.monotonic() >= deadline:
                raise RuntimeError("exact policy installation unconfirmed: " + str(status))
            time.sleep(0.1)

    def wait_provider(self, receipt, attached):
        client, profile = self.client, self.profile
        kind = (
            client.pb.PROVIDER_MUTATION_KIND_ATTACH
            if attached
            else client.pb.PROVIDER_MUTATION_KIND_DETACH
        )
        if (
            not receipt.receipt_id
            or receipt.desired.sandbox_id != self.identity
            or receipt.desired.sandbox != self.name
            or receipt.provider != profile.provider
            or receipt.workspace != client.scope.workspace
            or receipt.kind != kind
            or receipt.desired.provider_id != (profile.provider_id if attached else "")
        ):
            raise RuntimeError("provider mutation identity mismatch")
        expected = (
            client.pb.PROVIDER_READINESS_STATE_READY
            if attached
            else client.pb.PROVIDER_READINESS_STATE_REVOKED
        )
        deadline = time.monotonic() + 30
        while True:
            self.current()
            status = client.rpc(
                "GetSandboxProviderStatus",
                client.pb.GetSandboxProviderStatusRequest(
                    sandbox=self.name,
                    provider=profile.provider,
                    workspace_scope=client.scope,
                    receipt_id=receipt.receipt_id,
                ),
            ).status
            if receipt_installed(status, receipt, expected):
                self.record(
                    "barriers",
                    {
                        "provider_state": client.pb.ProviderReadinessState.Name(status.state),
                        "status_hex": status.SerializeToString().hex(),
                    },
                )
                return status
            if time.monotonic() >= deadline:
                raise RuntimeError("exact provider installation unconfirmed: " + str(status))
            time.sleep(0.1)

    def policy(self, enabled):
        request = self.policy_request(self.current().metadata.resource_version, enabled)
        response = self.submit("UpdateConfig", request)
        self.wait_policy(response)

    def provider(self, attached):
        method, request = self.provider_request(self.current().metadata.resource_version, attached)
        receipt = self.submit(method, request).receipt
        self.wait_provider(receipt, attached)
        return receipt

    def check(self, label, expected_get, expected_post, *, blocked=False):
        cases = [("GET", "/provider", expected_get), ("POST", "/provider", expected_post)]
        if blocked:
            cases.append(("GET", "/blocked", 403))
        for method, path, expected in cases:
            self.current()
            observe_native(
                self.client,
                self.name,
                self.ip,
                self.record,
                label + "_" + method.lower() + path.replace("/", "_"),
                self.profile.provider_host,
                path,
                expected,
                method,
            )
            self.fixture.save()

    def race(self, schedule):
        self.policy(True)
        self.provider(True)
        self.check(schedule + "_before", 200, 200, blocked=True)
        version = self.current().metadata.resource_version
        detach = self.provider_request(version, False)
        requests = [("UpdateConfig", self.policy_request(version, False)), detach]
        responses = competing_calls(self.client, requests, schedule, self.record)
        self.fixture.save()
        policy = responses["UpdateConfig"]
        if policy is not None:
            self.wait_policy(policy)
            self.check(schedule + "_policy_won", 200, 403, blocked=True)
            self.provider(False)
        else:
            detached = responses[detach[0]]
            if detached is None:
                raise RuntimeError("missing successful detach response")
            self.wait_provider(detached.receipt, False)
            self.check(schedule + "_detach_won", 403, 200, blocked=True)
            self.policy(False)
        self.check(schedule + "_converged", "EACCES", "EACCES")

    def restart(self, gateway, receipt, attached):
        self.wait_provider(receipt, attached)
        before = snapshot(self.identity)
        before_spec = self.current().spec.SerializeToString(deterministic=True).hex()
        row = {
            "attached": attached,
            "receipt_hex": receipt.SerializeToString().hex(),
            "before": before,
            "before_spec_hex": before_spec,
            "completed": False,
        }
        self.record("restarts", row)
        gateway.stop()
        try:
            row["stopped_returncode"] = gateway.process.poll()
            if row["stopped_returncode"] is None:
                raise RuntimeError("gateway did not stop")
        finally:
            gateway.start()
        wait_gateway(self.client)
        row["after_spec_hex"] = self.current().spec.SerializeToString(deterministic=True).hex()
        after = snapshot(self.identity)
        row["after"] = after
        if (
            row["after_spec_hex"] != before_spec
            or len(before["containers"]) != 2
            or {c["Id"] for c in before["containers"]} != {c["Id"] for c in after["containers"]}
            or not before["processes"]
            or any(
                start is None or after["processes"].get(pid) != start
                for pid, start in before["processes"].items()
            )
        ):
            raise RuntimeError("restart changed sandbox intent or physical identity")
        self.wait_provider(receipt, attached)
        self.check(
            "restart_attached" if attached else "restart_detached",
            200 if attached else "EACCES",
            200 if attached else "EACCES",
            blocked=attached,
        )
        row["completed"] = True
        self.fixture.save()


async def measure_transitions(fixture, ip, gateway):
    """Keep race/restart evidence separate from serial composition and adoption gates."""
    ip = native_origin_ip(ip)
    evidence = fixture.report["transitions"] = {
        "scope": "one anonymous provider; same-version policy/detach conflicts; retained-state gateway restart",
        "cases": [],
        "controls": [],
        "races": [],
        "mutations": [],
        "barriers": [],
        "restarts": [],
        "qualification_passed": False,
        "characterization_completed": False,
        "provider_and_profile_cleanup_verified": False,
        "unqualified": [
            "server-side interleaving beyond observed RPC overlap",
            "in-flight traffic and persistent connections",
            "crash, database loss, supervisor/workload restart and credential refresh",
            "multiple providers, profile edits, HTTPS and other protocols",
            "complete egress conformance and backend adoption",
        ],
    }
    fixture.save()

    def record(kind, row):
        evidence[kind].append(row)
        fixture.save()

    profile = CompositionProfile()
    try:
        await asyncio.to_thread(profile.preflight, ip)
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, profile.provider_host, 8000, socket.AF_INET
        )
        if {address[4][0] for address in addresses} != {ip}:
            raise RuntimeError("fixture DNS did not resolve to the owned origin")
        evidence.update(
            profile=profile.profile_id, provider=profile.provider, provider_prepare_state="pending"
        )
        fixture.save()
        await asyncio.to_thread(profile.prepare_workspace, fixture.client)
        evidence.update(provider_id=profile.provider_id, provider_prepare_state="confirmed")
        fixture.save()
        await fixture.acquire(policy=fixture_policy(fixture.client.image, (), ip))
        transitions = Transitions(fixture, profile, ip, record)
        for schedule in ("policy_first", "detach_first", "concurrent"):
            await asyncio.to_thread(transitions.race, schedule)
        await asyncio.to_thread(transitions.policy, True)
        receipt = await asyncio.to_thread(transitions.provider, True)
        await asyncio.to_thread(transitions.restart, gateway, receipt, True)
        await asyncio.to_thread(transitions.policy, False)
        receipt = await asyncio.to_thread(transitions.provider, False)
        await asyncio.to_thread(transitions.restart, gateway, receipt, False)
        if len(evidence["cases"]) != 29 or not all(row["passed"] for row in evidence["cases"]):
            raise RuntimeError("incomplete native transition matrix")
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
