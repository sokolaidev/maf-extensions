# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize elapsed static-credential expiry across retained and replacement references."""

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

from openshell_composition_probe import receipt_installed
from openshell_credentials_probe import CredentialProfile, credential_observation, guest_request
from openshell_egress_probe import measure as run_matrix
from openshell_egress_probe import origin_get
from openshell_files_probe import execute, signed_gateway
from openshell_rotation_probe import select_update_receipt


class ExpiryProfile(CredentialProfile):
    """Observe two elapsed expiry boundaries, then restore a non-expiring static value."""

    MODES = ("expiry",)
    CASES = 16
    SCOPE = (
        "synthetic static credential expiry across serial HTTP generations; not automatic refresh"
    )

    def prepare_workspace(self, client):
        self.expiration = int(time.time()) + 45
        super().prepare_workspace(client)

    def build_provider(self, client):
        provider = super().build_provider(client)
        if self.expiration:
            provider.credential_expiration_times[self.env_key].seconds = self.expiration
        return provider

    def run_cases(self, client, name, owner, ip, mode):
        a = self
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
                raise RuntimeError("unexpected expiry observation: " + label)

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

        def environment(label, present, save=None):
            code = (
                "import os,json,hashlib; from pathlib import Path; v=os.environ.get("
                + repr(a.env_key)
                + ",''); "
            )
            if save:
                code += "Path(" + repr(save) + ").write_text(v); "
            code += "print(json.dumps({'present':bool(v),'placeholder':v.startswith('openshell:resolve:'),'hash':hashlib.sha256(v.encode()).hexdigest()}))"
            status, output, error = execute(client, name, code)
            observed = json.loads(output)
            passed = status == 0 and not error and observed["present"] == present
            if present:
                passed = (
                    passed
                    and observed["placeholder"]
                    and observed["hash"] != hashlib.sha256(a.secret.encode()).hexdigest()
                )
            record(label, passed, present=observed["present"])

        def wait_expiry(expiration):
            deadline = time.monotonic() + 65
            while time.time() < expiration + 1:
                if time.monotonic() >= deadline:
                    raise RuntimeError("expiry wall-clock boundary not reached within bounded wait")
                time.sleep(0.2)
            current()
            print(
                json.dumps({"expiry_boundary": expiration, "observed_unix_time": time.time()}),
                flush=True,
            )

        def update(expiration):
            stored = client.rpc(
                "GetProvider",
                client.pb.GetProviderRequest(name=a.provider, workspace_scope=client.scope),
            ).provider
            if stored.metadata.id != a.provider_id:
                raise RuntimeError("provider replaced before update")
            a.secret = "spike-only-" + secrets.token_hex(24)
            a.authorization_hash = hashlib.sha256(("Bearer " + a.secret).encode()).hexdigest()
            a.expiration = expiration
            replacement = a.build_provider(client)
            replacement.metadata.CopyFrom(stored.metadata)
            request = client.pb.UpdateProviderRequest(
                provider=replacement, workspace_scope=client.scope, request_id=str(uuid.uuid4())
            )
            if expiration:
                request.credential_expiration_times[a.env_key].seconds = expiration
            else:
                request.clear_credential_expiration_keys.append(a.env_key)
            response = client.rpc("UpdateProvider", request)
            receipt = select_update_receipt(response, owner["id"], a.provider, a.provider_id)
            wait_receipt(a, receipt, True)

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
                raise RuntimeError("guest expiry observation failed")
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
        first_expiry = a.expiration
        first_digest = a.authorization_hash
        environment("initial_expiring_placeholder", True, "/sandbox/first")
        check("initial_value_before_expiry", a, "/provider/ok")
        if time.time() >= first_expiry - 5:
            raise RuntimeError("initial expiry left insufficient time for replacement control")
        second_expiry = first_expiry + 30
        update(second_expiry)
        environment("replacement_expiring_placeholder", True, "/sandbox/second")
        check("replacement_before_first_expiry", a, "/provider/ok")
        check(
            "retained_first_before_expiry",
            a,
            "/provider/ok",
            source="saved",
            saved_path="/sandbox/first",
            digest=first_digest,
        )
        if time.time() >= first_expiry:
            raise RuntimeError("pre-expiry controls crossed expiry boundary")
        wait_expiry(first_expiry)
        check(
            "retained_first_rejected_after_expiry",
            a,
            "/provider/ok",
            source="saved",
            saved_path="/sandbox/first",
            expected="credential_unavailable",
        )
        check("replacement_survives_first_expiry", a, "/provider/ok")
        wait_expiry(second_expiry)
        check(
            "retained_second_rejected_after_expiry",
            a,
            "/provider/ok",
            source="saved",
            saved_path="/sandbox/second",
            expected="credential_unavailable",
        )
        environment("expired_placeholder_retained_in_new_process", True)
        check(
            "new_process_expired_placeholder_rejected",
            a,
            "/provider/ok",
            expected="credential_unavailable",
        )
        check("base_route_without_expired_credential", a, "/provider/ok", source="none")
        update(0)
        environment("restored_nonexpiring_placeholder", True)
        check("restored_value_reaches_origin", a, "/provider/ok")
        check(
            "restore_does_not_revive_first",
            a,
            "/provider/ok",
            source="saved",
            saved_path="/sandbox/first",
            expected="credential_unavailable",
        )
        check(
            "restore_does_not_revive_second",
            a,
            "/provider/ok",
            source="saved",
            saved_path="/sandbox/second",
            expected="credential_unavailable",
        )
        transition(a, False)
        environment("detached_environment_absent", False)
        return results


def main():
    """Run the owned elapsed-expiry fixture with only generated synthetic credentials."""
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
        report = run_matrix(args.image, issuer, str(ip), profile=ExpiryProfile())
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
