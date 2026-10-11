# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize two static providers, conflicting environment keys and retained credential revisions."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from openshell_cleanup import Cleanup
from openshell_composition_probe import receipt_installed
from openshell_credentials_probe import CredentialProfile, credential_observation, guest_request
from openshell_egress_probe import measure as run_matrix
from openshell_egress_probe import origin_get
from openshell_files_probe import execute, signed_gateway


def select_update_receipt(response, sandbox_id, provider, provider_id):
    """Require the exact sole owned target and provider revision before waiting on an update."""
    if (
        not response.mutation_id
        or len(response.target_receipts) != 1
        or response.provider.metadata.id != provider_id
        or not response.provider.metadata.resource_version
    ):
        raise RuntimeError("unexpected provider update target set")
    receipt = response.target_receipts[0]
    if (
        not receipt.receipt_id
        or receipt.mutation_id != response.mutation_id
        or receipt.provider != provider
        or receipt.desired.sandbox_id != sandbox_id
        or receipt.desired.provider_id != provider_id
        or receipt.desired.provider_resource_version != response.provider.metadata.resource_version
    ):
        raise RuntimeError("provider update receipt binding mismatch")
    return receipt


class RotationProfile:
    """Own disjoint static bindings and qualify new versus retained placeholder generations."""

    MODES = ("rotation",)
    CASES = 23
    SCOPE = "two synthetic static providers, serial HTTP updates and detach; not automatic refresh or key retirement"

    def __init__(self):
        self.providers = [CredentialProfile() for _ in range(3)]
        for item, key, path in zip(
            self.providers,
            ("SPIKE1797_A", "SPIKE1797_B", "SPIKE1797_A"),
            ("/a/**", "/b/**", "/c/**"),
            strict=True,
        ):
            item.env_key, item.endpoint_path = key, path
            item.broad_methods = ("GET",)

    def preflight(self, ip):
        for item in self.providers:
            item.preflight(ip)

    def prepare_workspace(self, client):
        for item in self.providers:
            item.prepare_workspace(client)

    def configure(self, request, ip, mode):
        self.providers[0].configure(request, ip, mode)

    def cleanup_workspace(self, client):
        with Cleanup() as cleanup:
            for item in reversed(self.providers):
                cleanup.attempt(item.provider_id, item.cleanup_workspace, client)

    def run_cases(self, client, name, owner, ip, mode):
        a, b, collision = self.providers
        results = []

        def current():
            sandbox = client.rpc(
                "GetSandbox", client.pb.GetSandboxRequest(name=name, workspace_scope=client.scope)
            ).sandbox
            if sandbox.metadata.id != owner["id"]:
                raise RuntimeError("refuse replaced fixture")
            return sandbox

        def record(label, passed, **details):
            row = {"case": label, "passed": passed, **details}
            print(json.dumps(row), flush=True)
            results.append(row)
            if not passed:
                raise RuntimeError("unexpected rotation observation: " + label)

        def wait_receipt(item, receipt, attached):
            expected = (
                client.pb.PROVIDER_READINESS_STATE_READY
                if attached
                else client.pb.PROVIDER_READINESS_STATE_REVOKED
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current()
                status = client.rpc(
                    "GetSandboxProviderStatus",
                    client.pb.GetSandboxProviderStatusRequest(
                        sandbox=name,
                        provider=item.provider,
                        receipt_id=receipt.receipt_id,
                        workspace_scope=client.scope,
                    ),
                ).status
                if receipt_installed(status, receipt, expected):
                    print(
                        json.dumps(
                            {
                                "provider": item.provider,
                                "receipt": receipt.receipt_id,
                                "state": client.pb.ProviderReadinessState.Name(status.state),
                            }
                        ),
                        flush=True,
                    )
                    return
                time.sleep(0.1)
            raise RuntimeError("exact provider installation unconfirmed")

        def transition(item, attached):
            prefix = "Attach" if attached else "Detach"
            response = client.rpc(
                prefix + "SandboxProvider",
                getattr(client.pb, prefix + "SandboxProviderRequest")(
                    sandbox=name,
                    provider=item.provider,
                    workspace_scope=client.scope,
                    expected_resource_version=current().metadata.resource_version,
                    request_id=str(uuid.uuid4()),
                ),
            )
            receipt = response.receipt
            if (
                not receipt.receipt_id
                or receipt.desired.sandbox_id != owner["id"]
                or receipt.provider != item.provider
            ):
                raise RuntimeError("provider transition receipt mismatch")
            wait_receipt(item, receipt, attached)
            return receipt

        def environment(label, keys, save=()):
            code = (
                "import os,json,hashlib; from pathlib import Path; values={k:os.environ.get(k,'') for k in "
                + repr([a.env_key, b.env_key])
                + "}; "
            )
            for key, path in save:
                code += "Path(" + repr(path) + ").write_text(values[" + repr(key) + "]); "
            code += "print(json.dumps({k:{'present':bool(v),'placeholder':v.startswith('openshell:resolve:'),'hash':hashlib.sha256(v.encode()).hexdigest()} for k,v in values.items()}))"
            status, output, error = execute(client, name, code)
            observed = json.loads(output)
            passed = status == 0 and not error
            for item, present in zip((a, b), keys, strict=True):
                value = observed[item.env_key]
                passed = passed and value["present"] == present
                if present:
                    passed = (
                        passed
                        and value["placeholder"]
                        and value["hash"] != hashlib.sha256(item.secret.encode()).hexdigest()
                    )
            if all(keys):
                passed = passed and observed[a.env_key]["hash"] != observed[b.env_key]["hash"]
            record(label, passed, present=[observed[item.env_key]["present"] for item in (a, b)])

        def check(
            label,
            item,
            path,
            *,
            source="env",
            saved_path="/sandbox/unused",
            expected: int | str = 200,
            digest=None,
        ):
            current()
            marker = uuid.uuid4().hex
            host = a.provider_host
            url = f"http://{host}:8000{path}?case={marker}"
            status, output, error = execute(
                client,
                name,
                guest_request(url, "GET", source, env_key=item.env_key, saved_path=saved_path),
            )
            if status != 0 or error:
                raise RuntimeError("guest rotation observation failed")
            observed = json.loads(output)
            hits = [
                x
                for x in origin_get(ip, "/__events")
                if parse_qs(urlsplit(x["path"]).query).get("case") == [marker]
            ]
            request = {
                "method": "GET",
                "host": host + ":8000",
                "port": 8000,
                "path": path + "?case=" + marker,
                "authorization_sha256": None
                if source == "none"
                else digest or item.authorization_hash,
            }
            record(
                label,
                credential_observation(observed, hits, expected, request),
                guest=observed,
                origin_hits=hits,
            )

        transition(a, True)
        transition(b, True)
        environment(
            "distinct_guest_placeholders",
            (True, True),
            ((a.env_key, "/sandbox/a-old"), (b.env_key, "/sandbox/b-old")),
        )
        check("provider_a_value", a, "/a/ok")
        check("provider_b_value", b, "/b/ok")
        check("a_cannot_use_b_binding", a, "/b/ok", expected="credential_endpoint_mismatch")
        check("b_cannot_use_a_binding", b, "/a/ok", expected="credential_endpoint_mismatch")
        before = current()
        request = client.pb.AttachSandboxProviderRequest(
            sandbox=name,
            provider=collision.provider,
            workspace_scope=client.scope,
            expected_resource_version=before.metadata.resource_version,
            request_id=str(uuid.uuid4()),
        )
        try:
            client.rpc("AttachSandboxProvider", request)
        except client.grpc.RpcError as exc:
            code = exc.code().name
        else:
            code = "OK"
        attached = client.rpc(
            "ListSandboxProviders",
            client.pb.ListSandboxProvidersRequest(sandbox=name, workspace_scope=client.scope),
        ).providers
        record(
            "duplicate_environment_key_rejected",
            code == "FAILED_PRECONDITION"
            and {p.metadata.id for p in attached} == {a.provider_id, b.provider_id}
            and current().metadata.resource_version == before.metadata.resource_version,
            status=code,
        )
        check("a_survives_rejected_collision", a, "/a/ok")
        check("b_survives_rejected_collision", b, "/b/ok")

        old_digest = a.authorization_hash
        stored = client.rpc(
            "GetProvider",
            client.pb.GetProviderRequest(name=a.provider, workspace_scope=client.scope),
        ).provider
        if stored.metadata.id != a.provider_id:
            raise RuntimeError("provider replaced before update")
        a.secret = "spike-only-" + secrets.token_hex(24)
        a.authorization_hash = hashlib.sha256(("Bearer " + a.secret).encode()).hexdigest()
        replacement = a.build_provider(client)
        replacement.metadata.CopyFrom(stored.metadata)
        response = client.rpc(
            "UpdateProvider",
            client.pb.UpdateProviderRequest(
                provider=replacement, workspace_scope=client.scope, request_id=str(uuid.uuid4())
            ),
        )
        receipt = select_update_receipt(response, owner["id"], a.provider, a.provider_id)
        wait_receipt(a, receipt, True)
        check("new_process_uses_rotated_a", a, "/a/ok")
        check(
            "saved_a_retains_original_value",
            a,
            "/a/ok",
            source="saved",
            saved_path="/sandbox/a-old",
            digest=old_digest,
        )
        check("new_b_unchanged_by_a_rotation", b, "/b/ok")
        check(
            "saved_b_unchanged_by_a_rotation",
            b,
            "/b/ok",
            source="saved",
            saved_path="/sandbox/b-old",
        )
        environment("save_new_a_generation", (True, True), ((a.env_key, "/sandbox/a-new"),))
        transition(a, False)
        check(
            "detach_a_revokes_original_reference",
            a,
            "/a/ok",
            source="saved",
            saved_path="/sandbox/a-old",
            expected="credential_endpoint_mismatch",
        )
        check(
            "detach_a_revokes_rotated_reference",
            a,
            "/a/ok",
            source="saved",
            saved_path="/sandbox/a-new",
            expected="credential_endpoint_mismatch",
        )
        check(
            "detached_original_a_cannot_use_b",
            a,
            "/b/ok",
            source="saved",
            saved_path="/sandbox/a-old",
            expected="credential_endpoint_mismatch",
        )
        check(
            "detached_rotated_a_cannot_use_b",
            a,
            "/b/ok",
            source="saved",
            saved_path="/sandbox/a-new",
            expected="credential_endpoint_mismatch",
        )
        check("b_remains_usable_after_a_detach", b, "/b/ok")
        check(
            "saved_b_remains_usable_after_a_detach",
            b,
            "/b/ok",
            source="saved",
            saved_path="/sandbox/b-old",
        )
        environment("only_b_in_new_environment", (False, True))
        transition(b, False)
        check(
            "detach_b_revokes_saved_reference",
            b,
            "/b/ok",
            source="saved",
            saved_path="/sandbox/b-old",
            expected="credential_unavailable",
        )
        environment("both_detached_environment_empty", (False, False))
        check("base_traffic_remains_without_credentials", a, "/a/ok", source="none")
        return results


def main():
    """Run the owned multi-provider rotation fixture with only generated synthetic credentials."""
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
        report = run_matrix(args.image, issuer, str(ip), profile=RotationProfile())
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
