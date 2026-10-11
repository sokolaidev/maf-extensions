# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize gateway-managed OAuth refresh, authorization epochs and bounded failure recovery."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from openshell_composition_probe import receipt_installed
from openshell_credentials_probe import CredentialProfile, credential_observation, guest_request
from openshell_egress_probe import measure as run_matrix
from openshell_egress_probe import origin_get
from openshell_files_probe import execute, signed_gateway


def select_refresh_status(response, provider, provider_id, key, strategy):
    """Require exactly the owned credential status before interpreting refresh progress."""
    if len(response.credentials) != 1:
        raise RuntimeError("ambiguous refresh status set")
    status = response.credentials[0]
    if (status.provider, status.provider_id, status.credential_key, status.strategy) != (
        provider,
        provider_id,
        key,
        strategy,
    ):
        raise RuntimeError("refresh status identity mismatch")
    return status


class TokenIssuer:
    """Serve synthetic OAuth tokens on loopback and retain only digest evidence."""

    def __init__(self):
        self.client_secret = secrets.token_hex(24)
        self.lock = threading.Lock()
        self.mode = "ok"
        self.ttl = 90
        self.events = []
        self.generation = 0
        self.latest = {}
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/token" or not 0 < length <= 4096:
                    self.send_error(400)
                    return
                fields = parse_qs(self.rfile.read(length).decode())
                valid = fields == {
                    "grant_type": ["client_credentials"],
                    "client_id": ["spike1797"],
                    "client_secret": [issuer.client_secret],
                }
                with issuer.lock:
                    mode = issuer.mode if valid else "invalid_client"
                    event = {"mode": mode, "valid_material": valid, "at": time.time()}
                    if mode == "ok":
                        issuer.generation += 1
                        token = "spike-only-" + secrets.token_hex(24)
                        event.update(
                            generation=issuer.generation,
                            authorization_sha256=hashlib.sha256(
                                ("Bearer " + token).encode()
                            ).hexdigest(),
                            expires_at=time.time() + issuer.ttl,
                        )
                        issuer.latest = dict(event)
                        payload = {
                            "access_token": token,
                            "expires_in": issuer.ttl,
                            "token_type": "Bearer",
                        }
                        code = 200
                    else:
                        payload = {"error": mode}
                        code = 503 if mode == "temporarily_unavailable" else 400
                    issuer.events.append(event)
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("token issuer did not stop")
        print(json.dumps({"issuer_stopped": True, "token_events": self.events}), flush=True)

    def configure(self, *, mode="ok", ttl=300):
        with self.lock:
            self.mode, self.ttl = mode, ttl

    def snapshot(self):
        with self.lock:
            return dict(self.latest)


class RefreshProfile(CredentialProfile):
    """Observe worker and forced refresh separately from explicit reauthorization."""

    MODES = ("refresh",)
    CASES = 26
    SCOPE = "synthetic OAuth client credentials, serial HTTP and eventual refresh convergence; not production identity"

    def __init__(self, token_issuer):
        super().__init__()
        self.token_issuer = token_issuer

    def build_profile(self, client):
        profile = super().build_profile(client)
        refresh = profile.credentials[0].refresh
        refresh.strategy = client.pb.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_CLIENT_CREDENTIALS
        refresh.token_url = self.token_issuer.url
        refresh.refresh_before.seconds = 75
        refresh.max_lifetime.seconds = 300
        refresh.material.add(name="client_id", required=True)
        refresh.material.add(name="client_secret", required=True, secret=True)
        return profile

    def configure_refresh(self, client):
        return client.rpc(
            "ConfigureProviderRefresh",
            client.pb.ConfigureProviderRefreshRequest(
                provider=self.provider,
                credential_key=self.env_key,
                workspace_scope=client.scope,
                strategy=client.pb.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_CLIENT_CREDENTIALS,
                material={
                    "client_id": "spike1797",
                    "client_secret": self.token_issuer.client_secret,
                },
                secret_material_keys=["client_secret"],
                request_id=str(uuid.uuid4()),
            ),
        )

    def rotate(self, client):
        return client.rpc(
            "RotateProviderCredential",
            client.pb.RotateProviderCredentialRequest(
                provider=self.provider,
                credential_key=self.env_key,
                workspace_scope=client.scope,
                request_id=str(uuid.uuid4()),
            ),
        )

    def refresh_status(self, client):
        response = client.rpc(
            "GetProviderRefreshStatus",
            client.pb.GetProviderRefreshStatusRequest(
                provider=self.provider, credential_key=self.env_key, workspace_scope=client.scope
            ),
        )
        return select_refresh_status(
            response,
            self.provider,
            self.provider_id,
            self.env_key,
            client.pb.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_CLIENT_CREDENTIALS,
        )

    def prepare_workspace(self, client):
        super().prepare_workspace(client)
        self.configure_refresh(client)
        self.rotate(client)
        self.initial_mint = self.token_issuer.snapshot()
        self.token_issuer.configure(ttl=300)

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
                raise RuntimeError("unexpected refresh observation: " + label)

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

        def environment(label, present, save=None, *, same=None, different=None):
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
            if same is not None:
                passed = passed and observed["hash"] == same
            if different is not None:
                passed = passed and observed["hash"] != different
            record(label, passed, present=observed["present"])
            return observed["hash"]

        def wait_expiry(expiration):
            deadline = time.monotonic() + 65
            while time.time() < expiration + 1:
                if time.monotonic() >= deadline:
                    raise RuntimeError("expiry boundary not reached within bounded wait")
                time.sleep(0.2)
            current()
            print(
                json.dumps({"expiry_boundary": expiration, "observed_unix_time": time.time()}),
                flush=True,
            )

        def minted():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = a.refresh_status(client)
                latest = a.token_issuer.snapshot()
                expiration = status.expiration_time.seconds + status.expiration_time.nanos / 1e9
                if (
                    status.status == "refreshed"
                    and latest
                    and abs(expiration - latest["expires_at"]) < 2
                ):
                    a.authorization_hash = latest["authorization_sha256"]
                    return status, latest
                time.sleep(0.1)
            raise RuntimeError("minted credential status not confirmed")

        def failure(label, mode, rpc_code, state, failure_code, action, retry_delay):
            a.token_issuer.configure(mode=mode)
            try:
                a.rotate(client)
            except client.grpc.RpcError as exc:
                code = exc.code().name
            else:
                code = "OK"
            status = a.refresh_status(client)
            record(
                label,
                code == rpc_code
                and status.status == state
                and status.failure_code == failure_code
                and status.recovery_action == action
                and status.HasField("next_refresh_time")
                and status.HasField("last_error_time")
                and abs(
                    (status.next_refresh_time.seconds + status.next_refresh_time.nanos / 1e9)
                    - (status.last_error_time.seconds + status.last_error_time.nanos / 1e9)
                    - retry_delay
                )
                < 0.1,
                rpc_status=code,
                refresh_state=status.status,
                failure_code=status.failure_code,
                recovery_action=client.pb.ProviderCredentialRefreshRecoveryAction.Name(
                    status.recovery_action
                ),
                retry_scheduled=status.HasField("next_refresh_time"),
                retry_delay_seconds=status.next_refresh_time.seconds
                - status.last_error_time.seconds,
            )

        def check(
            label,
            *,
            source="saved",
            saved_path="/sandbox/first",
            expected: int | str = 200,
            converge=False,
        ):
            deadline = time.monotonic() + (30 if converge else 0)
            attempts = []
            while True:
                current()
                marker = uuid.uuid4().hex
                path = "/provider/ok?case=" + marker
                host = a.provider_host
                status, output, error = execute(
                    client,
                    name,
                    guest_request(
                        f"http://{host}:8000{path}",
                        "GET",
                        source,
                        env_key=a.env_key,
                        saved_path=saved_path,
                    ),
                )
                if status != 0 or error:
                    raise RuntimeError("guest refresh observation failed")
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
                    "path": path,
                    "authorization_sha256": None if source == "none" else a.authorization_hash,
                }
                passed = credential_observation(observed, hits, expected, request)
                attempts.append({"guest": observed, "origin_hits": hits})
                if passed or time.monotonic() >= deadline:
                    record(label, passed, attempts=attempts)
                    return
                time.sleep(0.2)

        transition(a, True)
        minted()
        first_hash = environment("initial_stable_placeholder", True, "/sandbox/first")
        check("initial_minted_value")
        before = client.rpc(
            "GetProvider",
            client.pb.GetProviderRequest(name=a.provider, workspace_scope=client.scope),
        ).provider
        if before.metadata.id != a.provider_id:
            raise RuntimeError("provider replaced before manual update control")
        replacement = a.build_provider(client)
        replacement.metadata.CopyFrom(before.metadata)
        try:
            client.rpc(
                "UpdateProvider",
                client.pb.UpdateProviderRequest(
                    provider=replacement, workspace_scope=client.scope, request_id=str(uuid.uuid4())
                ),
            )
        except client.grpc.RpcError as exc:
            code = exc.code().name
        else:
            code = "OK"
        after = client.rpc(
            "GetProvider",
            client.pb.GetProviderRequest(name=a.provider, workspace_scope=client.scope),
        ).provider
        record(
            "manual_value_update_rejected",
            code == "FAILED_PRECONDITION" and after.metadata == before.metadata,
            rpc_status=code,
        )
        check("value_survives_rejected_update")

        deadline = time.monotonic() + 85
        while time.monotonic() < deadline:
            if a.token_issuer.snapshot()["generation"] > a.initial_mint["generation"]:
                break
            time.sleep(0.2)
        status, latest = minted()
        record(
            "worker_refresh_without_rotate_request",
            latest["generation"] > a.initial_mint["generation"]
            and status.HasField("last_refresh_time"),
            generation=latest["generation"],
        )
        check("saved_handle_follows_worker_refresh", converge=True)
        environment("worker_refresh_preserves_handle", True, same=first_hash)
        a.rotate(client)
        minted()
        check("saved_handle_follows_forced_refresh", converge=True)
        environment("forced_refresh_preserves_handle", True, same=first_hash)

        a.configure_refresh(client)
        a.rotate(client)
        minted()
        check(
            "reconfiguration_revokes_old_handle", expected="credential_unavailable", converge=True
        )
        second_hash = environment(
            "reconfiguration_changes_handle", True, "/sandbox/second", different=first_hash
        )
        check("new_epoch_value", saved_path="/sandbox/second", converge=True)

        failure(
            "temporary_failure_schedules_retry",
            "temporarily_unavailable",
            "UNAVAILABLE",
            "error",
            "oauth_token_endpoint_retryable",
            client.pb.PROVIDER_CREDENTIAL_REFRESH_RECOVERY_ACTION_RETRY,
            60,
        )
        check("current_token_survives_temporary_failure", saved_path="/sandbox/second")
        a.token_issuer.configure()
        a.rotate(client)
        minted()
        check("manual_retry_recovers_same_handle", saved_path="/sandbox/second", converge=True)

        a.token_issuer.configure(ttl=30)
        a.rotate(client)
        _, short = minted()
        check("short_lived_token_installed", saved_path="/sandbox/second", converge=True)
        environment("short_rotation_preserves_handle", True, same=second_hash)
        failure(
            "invalid_client_defers_retry_one_hour",
            "invalid_client",
            "FAILED_PRECONDITION",
            "configuration_required",
            "oauth_invalid_client",
            client.pb.PROVIDER_CREDENTIAL_REFRESH_RECOVERY_ACTION_FIX_CONFIGURATION,
            3600,
        )
        check("current_token_survives_terminal_failure_until_expiry", saved_path="/sandbox/second")
        if time.time() >= short["expires_at"]:
            raise RuntimeError("pre-expiry failure control exceeded token lifetime")
        wait_expiry(short["expires_at"])
        check(
            "failed_refresh_token_rejected_after_expiry",
            saved_path="/sandbox/second",
            expected="credential_unavailable",
        )
        status = a.refresh_status(client)
        record(
            "configuration_failure_remains_scheduled_after_expiry",
            status.status == "configuration_required" and status.HasField("next_refresh_time"),
        )
        check("base_route_remains_after_refresh_expiry", source="none")

        a.token_issuer.configure()
        a.configure_refresh(client)
        a.rotate(client)
        minted()
        check("recovery_value_reaches_origin", source="env", converge=True)
        check(
            "recovery_keeps_previous_epoch_revoked",
            saved_path="/sandbox/second",
            expected="credential_unavailable",
            converge=True,
        )
        environment("recovery_installs_new_handle", True, different=second_hash)
        transition(a, False)
        environment("detached_environment_absent", False)
        return results


def main():
    """Run the owned refresh fixture with a synthetic loopback token issuer."""
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
    with TokenIssuer() as token_issuer, signed_gateway(args.config) as issuer:
        report = run_matrix(args.image, issuer, str(ip), profile=RefreshProfile(token_issuer))
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
