"""Measure fresh authority after verified runtime replacement, without claiming continuity."""

from __future__ import annotations

import asyncio
import socket
import time

from openshell_composition_probe import CompositionProfile, observe_native
from openshell_egress_probe import fixture_policy, native_origin_ip
from openshell_owned_probe import snapshot
from openshell_transition_probe import Transitions, runtime_continuity


def authority_identity(status):
    """Keep the network, control-session and workload-boundary identities distinct."""
    return (
        status.network_instance_id,
        status.observed.session_id,
        status.observed.process_instance_id,
    )


def fresh_authority(before, current, accepted=None):
    """Require replacement identities and advancing reports within one accepted runtime."""
    original, replacement = authority_identity(before), authority_identity(current)
    if not all(original) or not all(replacement):
        return False
    if any(old == new for old, new in zip(original, replacement, strict=True)):
        return False
    if current.observed.sequence <= 0:
        return False
    return accepted is None or (
        replacement == authority_identity(accepted)
        and current.observed.sequence > accepted.observed.sequence
    )


class RestartAuthority(Transitions):
    """Bind replacement-runtime observations to fresh installed authority and host identity."""

    def stable(self, runtime, spec):
        if self.current().spec.SerializeToString(deterministic=True).hex() != spec:
            raise RuntimeError("replacement runtime intent changed")
        if not runtime_continuity(self.identity, runtime, snapshot(self.identity)):
            raise RuntimeError("replacement runtime changed during authority observation")

    def attest(self, receipt, attached, before, runtime, spec, accepted=None):
        deadline = time.monotonic() + 30
        while True:
            status = self.wait_provider(receipt, attached)
            self.record("attestations", {"status_hex": status.SerializeToString().hex()})
            self.stable(runtime, spec)
            if accepted is not None and authority_identity(status) != authority_identity(accepted):
                raise RuntimeError("replacement authority identity changed")
            if fresh_authority(before, status, accepted):
                return status
            if time.monotonic() >= deadline:
                raise RuntimeError("fresh replacement authority unconfirmed")
            time.sleep(0.1)

    def recover(self, gateway, receipt, attached):
        row, before = self.restart_runtime(gateway, receipt, attached)
        row["before_status_hex"] = before.SerializeToString().hex()
        self.fixture.save()
        if row["runtime_continuity"]:
            raise RuntimeError("recovery experiment requires independently verified replacement")
        runtime, spec = row["after"], row["after_spec_hex"]
        self.stable(runtime, spec)
        retained = self.client.rpc(
            "GetSandboxProviderStatus",
            self.client.pb.GetSandboxProviderStatusRequest(
                sandbox=self.name,
                provider=self.profile.provider,
                workspace_scope=self.client.scope,
                receipt_id=receipt.receipt_id,
            ),
        ).status
        row["retained_receipt_status_hex"] = retained.SerializeToString().hex()
        row["retained_receipt_state"] = self.client.pb.ProviderReadinessState.Name(retained.state)
        row["restoration_state"] = "pending"
        self.fixture.save()
        self.policy(attached)
        restored = self.provider(attached)
        if not restored.receipt_id or restored.receipt_id == receipt.receipt_id:
            raise RuntimeError("restoration requires a new provider receipt")
        receipt = restored
        spec = self.current().spec.SerializeToString(deterministic=True).hex()
        row["restored_spec_hex"] = spec
        row["restored_receipt_hex"] = receipt.SerializeToString().hex()
        accepted = self.attest(receipt, attached, before, runtime, spec)
        row["restoration_state"] = "installed"
        row["replacement_status_hex"] = accepted.SerializeToString().hex()
        self.fixture.save()
        cases = [("GET", "/provider"), ("POST", "/provider")]
        if attached:
            cases.append(("GET", "/blocked"))
        for method, path in cases:
            accepted = self.attest(receipt, attached, before, runtime, spec, accepted)
            case = "recovered_attached" if attached else "recovered_detached"
            case += "_" + method.lower() + path.replace("/", "_")
            self.record("case_attempts", {"case": case})
            observation = observe_native(
                self.client,
                self.name,
                self.ip,
                self.record,
                case,
                self.profile.provider_host,
                path,
                (403 if path == "/blocked" else 200) if attached else "EACCES",
                method,
            )
            observation["authority_before_hex"] = accepted.SerializeToString().hex()
            observation["authority_verified"] = False
            self.fixture.save()
            accepted = self.attest(receipt, attached, before, runtime, spec, accepted)
            observation["authority_post_traffic_floor_hex"] = accepted.SerializeToString().hex()
            self.fixture.save()
            accepted = self.attest(receipt, attached, before, runtime, spec, accepted)
            observation["authority_after_hex"] = accepted.SerializeToString().hex()
            observation["authority_verified"] = True
            self.fixture.save()
        row["completed"] = True
        self.fixture.save()


async def measure_restart_authority(fixture, ip, gateway):
    """Run attached and detached replacement controls in one disposable owned sandbox."""
    ip = native_origin_ip(ip)
    evidence = fixture.report["restart_authority"] = {
        "scope": "anonymous provider; explicitly restored policy/provider intent after orderly runtime replacement",
        "cases": [],
        "case_attempts": [],
        "controls": [],
        "mutations": [],
        "barriers": [],
        "restarts": [],
        "attestations": [],
        "characterization_completed": False,
        "qualification_passed": False,
        "runtime_continuity_qualified": False,
        "provider_and_profile_cleanup_verified": False,
        "unrun_cases": [
            "recovered_attached_get_provider",
            "recovered_attached_post_provider",
            "recovered_attached_get_blocked",
            "recovered_detached_get_provider",
            "recovered_detached_post_provider",
        ],
        "unqualified": [
            "runtime continuity and original-runtime post-restart traffic",
            "crash, database loss, persistent connections and in-flight traffic",
            "credentials, multiple providers, HTTPS and other protocols",
            "complete egress conformance and backend adoption",
        ],
    }
    fixture.save()

    def record(kind, row):
        evidence[kind].append(row)
        if kind == "case_attempts" and row["case"] in evidence["unrun_cases"]:
            evidence["unrun_cases"].remove(row["case"])
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
        transitions = RestartAuthority(fixture, profile, ip, record)
        for attached in (True, False):
            await asyncio.to_thread(transitions.policy, attached)
            receipt = await asyncio.to_thread(transitions.provider, attached)
            await asyncio.to_thread(
                transitions.check,
                "before_attached" if attached else "before_detached",
                200 if attached else "EACCES",
                200 if attached else "EACCES",
                blocked=attached,
            )
            await asyncio.to_thread(transitions.recover, gateway, receipt, attached)
        recovered = [row for row in evidence["cases"] if row["case"].startswith("recovered_")]
        if (
            len(evidence["cases"]) != 10
            or len(recovered) != 5
            or evidence["unrun_cases"]
            or not all(row["passed"] for row in evidence["cases"])
            or not all(row.get("authority_verified") for row in recovered)
        ):
            raise RuntimeError("incomplete replacement authority matrix")
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
