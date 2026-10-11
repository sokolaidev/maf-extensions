# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize overlapping rules and synthetic static-credential endpoint binding."""

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

from openshell_composition_probe import CompositionProfile, receipt_installed, revision_installed
from openshell_egress_probe import matches_observation, origin_get
from openshell_egress_probe import measure as run_matrix
from openshell_files_probe import execute, signed_gateway

ENV_KEY = "SPIKE1797_TOKEN"


def credential_observation(observed, hits, expected, request):
    """Bind delivered authorization to the exact origin event or require a specific refusal."""
    if isinstance(expected, str):
        code = 403 if expected in ("policy_denied", "credential_endpoint_mismatch") else 500
        try:
            body = json.loads(observed.get("body", ""))
        except (TypeError, ValueError):
            return False
        return (
            not hits
            and observed.get("status") == code
            and isinstance(body, dict)
            and body.get("error") == expected
        )
    return matches_observation(observed, hits, expected, 1, request)


def guest_request(url, method, source, *, env_key=ENV_KEY, saved_path="/sandbox/saved-placeholder"):
    """Use only guest placeholders; never embed the synthetic provider value in guest code."""
    return (
        "import os,json,urllib.request,urllib.error\n"
        "from pathlib import Path\n"
        + "token="
        + (
            "os.environ.get(" + repr(env_key) + ",'')"
            if source == "env"
            else "Path(" + repr(saved_path) + ").read_text()"
            if source == "saved"
            else "''"
        )
        + "\n"
        + "headers={'Authorization':'Bearer '+token} if token else {}\ntry:\n r=urllib.request.urlopen(urllib.request.Request("
        + repr(url)
        + ",method="
        + repr(method)
        + ",headers=headers),timeout=3); print(json.dumps({'status':r.status,'body':r.read(4096).decode()}))\n"
        + "except urllib.error.HTTPError as e:\n print(json.dumps({'status':e.code,'body':e.read(4096).decode()}))\n"
        + "except Exception as e:\n print(json.dumps({'error':type(e).__name__,'errno':getattr(getattr(e,'reason',e),'errno',None)}))"
    )


class CredentialProfile(CompositionProfile):
    """Use one synthetic credential beside broad base rules and a narrower provider endpoint."""

    MODES = ("credentials",)
    CASES = 16
    SCOPE = "one static synthetic provider, overlapping HTTP rules, fresh processes; not complete credential confinement"

    def __init__(self):
        super().__init__()
        self.env_key = ENV_KEY
        self.endpoint_path = "/provider/**"
        self.broad_methods = ("GET", "POST")
        self.secret = "spike-only-" + secrets.token_hex(24)
        self.authorization_hash = hashlib.sha256(("Bearer " + self.secret).encode()).hexdigest()

    def build_profile(self, client):
        profile = super().build_profile(client)
        profile.display_name = "Synthetic credential spike origin"
        endpoint = profile.endpoints[0]
        endpoint.path = self.endpoint_path
        endpoint.rules[0].allow.path = self.endpoint_path
        profile.credentials.add(
            name="api_token",
            env_vars=[self.env_key],
            required=True,
            auth_style="bearer",
            header_name="authorization",
        )
        return profile

    def build_provider(self, client):
        provider = super().build_provider(client)
        provider.credentials[self.env_key] = self.secret
        return provider

    def configure(self, request, ip, mode):
        rule = request.spec.policy.network_policies["base_broad"]
        rule.name = "base_broad"
        rule.binaries.add(path="/usr/local/bin/python*")
        rule.binaries.add(path="/sandbox/probe-python")
        for host, port in (
            (self.provider_host, 8000),
            (self.base_host, 8000),
            (self.provider_host, 8001),
        ):
            endpoint = rule.endpoints.add(
                host=host,
                port=port,
                protocol="rest",
                allowed_ips=[ip],
                enforcement=self.pb.NETWORK_ENFORCEMENT_MODE_ENFORCE,
            )
            for method in self.broad_methods:
                endpoint.rules.add().allow.CopyFrom(self.pb.L7Allow(method=method, path="/**"))

    def run_cases(self, client, name, owner, ip, mode):
        results = []

        def current():
            sandbox = client.rpc(
                "GetSandbox", client.pb.GetSandboxRequest(name=name, workspace_scope=client.scope)
            ).sandbox
            if sandbox.metadata.id != owner["id"]:
                raise RuntimeError("refuse replaced fixture")
            return sandbox

        def transition(attached):
            prefix = "Attach" if attached else "Detach"
            response = client.rpc(
                prefix + "SandboxProvider",
                getattr(client.pb, prefix + "SandboxProviderRequest")(
                    sandbox=name,
                    provider=self.provider,
                    workspace_scope=client.scope,
                    expected_resource_version=current().metadata.resource_version,
                    request_id=str(uuid.uuid4()),
                ),
            )
            receipt = response.receipt
            if receipt.desired.sandbox_id != owner["id"] or not receipt.receipt_id:
                raise RuntimeError("provider mutation identity mismatch")
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
                        provider=self.provider,
                        receipt_id=receipt.receipt_id,
                        workspace_scope=client.scope,
                    ),
                ).status
                if receipt_installed(status, receipt, expected):
                    print(
                        json.dumps(
                            {
                                "transition": prefix,
                                "receipt": receipt.receipt_id,
                                "state": client.pb.ProviderReadinessState.Name(status.state),
                            }
                        ),
                        flush=True,
                    )
                    return
                time.sleep(0.1)
            raise RuntimeError("credential installation unconfirmed")

        def record(label, passed, **details):
            row = {"case": label, "passed": passed, **details}
            results.append(row)
            print(json.dumps(row), flush=True)
            if not passed:
                raise RuntimeError("unexpected credential observation: " + label)

        def environment(attached):
            code = "import os,json,hashlib; from pathlib import Path; v=os.environ.get('SPIKE1797_TOKEN',''); "
            if attached:
                code += "Path('/sandbox/saved-placeholder').write_text(v); "
            code += "print(json.dumps({'present':bool(v),'placeholder':v.startswith('openshell:resolve:'),'sha256':hashlib.sha256(v.encode()).hexdigest()}))"
            status, output, error = execute(client, name, code)
            observed = json.loads(output)
            passed = status == 0 and not error and observed["present"] == attached
            if attached:
                passed = (
                    passed
                    and observed["placeholder"]
                    and observed["sha256"] != hashlib.sha256(self.secret.encode()).hexdigest()
                )
            record(
                "guest_placeholder" if attached else "detached_environment_absent",
                passed,
                present=observed["present"],
                placeholder=observed["placeholder"],
            )

        def check(
            label,
            path="/provider/ok",
            *,
            host=None,
            port=8000,
            method="GET",
            source="env",
            expected: int | str = 200,
            alternate=False,
        ):
            current()
            host = host or self.provider_host
            marker = uuid.uuid4().hex
            url = f"http://{host}:{port}{path}?case={marker}"
            code = guest_request(url, method, source)
            if alternate:
                code = (
                    "import shutil,subprocess; shutil.copyfile('/usr/local/bin/python3.13','/sandbox/probe-python'); import os; os.chmod('/sandbox/probe-python',0o755); r=subprocess.run(['/sandbox/probe-python','-c',"
                    + repr(code)
                    + "],capture_output=True); print(r.stdout.decode(),end=''); print(r.stderr.decode(),end=''); raise SystemExit(r.returncode)"
                )
            status, output, error = execute(client, name, code)
            if status != 0 or error:
                raise RuntimeError("guest credential observation failed")
            observed = json.loads(output)
            hits = [
                x
                for x in origin_get(ip, "/__events")
                if parse_qs(urlsplit(x["path"]).query).get("case") == [marker]
            ]
            request = {
                "method": method,
                "path": path + "?case=" + marker,
                "host": host + ":" + str(port),
                "port": port,
                "authorization_sha256": self.authorization_hash if source != "none" else None,
            }
            passed = credential_observation(observed, hits, expected, request)
            record(label, passed, guest=observed, origin_hits=hits)

        check("base_provider_route_without_attachment", source="none")
        transition(True)
        environment(True)
        check("bound_placeholder_rewritten")
        check("no_implicit_static_header", source="none")
        check("broad_base_allows_post_with_credential", method="POST")
        self.broad_methods = ("GET",)
        replacement = client.pb.CreateSandboxRequest.FromString(
            client.request(name, str(uuid.uuid4()))
        )
        self.configure(replacement, ip, mode)
        response = client.rpc(
            "UpdateConfig",
            client.pb.UpdateConfigRequest(
                sandbox=name,
                workspace_scope=client.scope,
                policy=replacement.spec.policy,
                expected_resource_version=current().metadata.resource_version,
                request_id=str(uuid.uuid4()),
            ),
        )
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
                print(
                    json.dumps({"base_narrowed_to_get": True, "loaded_version": response.version}),
                    flush=True,
                )
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("narrowed base policy not installed")
        check("post_denied_after_base_narrowing", method="POST", expected="policy_denied")
        check("allowed_base_path_without_credential", path="/base", source="none")
        check("credential_path_mismatch", path="/base", expected="credential_endpoint_mismatch")
        check("allowed_other_host_without_credential", host=self.base_host, source="none")
        check(
            "credential_host_mismatch", host=self.base_host, expected="credential_endpoint_mismatch"
        )
        check("allowed_other_port_without_credential", port=8001, source="none")
        check("credential_port_mismatch", port=8001, expected="credential_endpoint_mismatch")
        check("base_authorized_other_binary_uses_credential", alternate=True)
        transition(False)
        environment(False)
        check("retained_placeholder_revoked", source="saved", expected="credential_unavailable")
        check("base_route_remains_after_detach", source="none")
        return results


def main():
    """Run synthetic credential characterization against an explicitly selected private origin."""
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
        report = run_matrix(args.image, issuer, str(ip), profile=CredentialProfile())
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
