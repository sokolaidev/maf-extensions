"""Measure operator CA snapshot, rollover and removal on one exclusively owned sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
import tomllib
import uuid

from openshell_https_probe import (
    control_https,
    guest_request,
    matches_native_https,
    native_https_policy,
    origin_get,
)
from openshell_owned_probe import snapshot
from openshell_replay_probe import wait_gateway
from openshell_transition_probe import runtime_continuity


def ca_source(config, ca_file):
    """Restrict writes to the explicitly configured, disposable public fixture bundle."""
    source = ca_file.with_name("active-ca.pem")
    configured = tomllib.loads(config.read_text(encoding="utf-8"))["openshell"]["supervisor"][
        "network"
    ]["additional_ca_bundle"]
    if (
        source.is_symlink()
        or str(source.resolve()) != configured
        or source.read_bytes() != ca_file.read_bytes()
    ):
        raise ValueError("requires an explicit active-ca.pem copy of the owned fixture CA")
    return source


def retain_runtime(fixture, owner):
    """Preserve every observed runtime identity for final independent absence checks."""
    if fixture.client.current_id(owner["name"]) != owner["sandbox_id"]:
        raise RuntimeError("CA fixture identity changed")
    observed = snapshot(owner["sandbox_id"])
    owner["containers"] = sorted(
        set(owner["containers"]) | {c["Id"] for c in observed["containers"]}
    )
    owner["processes"].update(observed["processes"])
    fixture.save()
    return observed


async def recycle(fixture, owner, gateway, row):
    """Apply the documented gateway restart then sandbox stop/start sequence once."""
    client = fixture.client
    retain_runtime(fixture, owner)
    await asyncio.to_thread(gateway.stop)
    row["gateway_exit_code"] = gateway.process.returncode
    fixture.save()
    if row["gateway_exit_code"] != 0:
        raise RuntimeError("CA reload requires an orderly gateway exit")
    gateway.start()
    await asyncio.to_thread(wait_gateway, client)
    retain_runtime(fixture, owner)
    for method, phase in (
        ("StopSandbox", client.pb.SANDBOX_PHASE_STOPPED),
        ("StartSandbox", client.pb.SANDBOX_PHASE_READY),
    ):
        request_id = str(uuid.uuid4())
        receipt = {"method": method, "request_id": request_id, "state": "pending"}
        row.setdefault("mutations", []).append(receipt)
        fixture.save()
        response = await asyncio.to_thread(
            client.rpc,
            method,
            getattr(client.pb, method + "Request")(
                workspace_scope=client.scope, name=owner["name"], request_id=request_id
            ),
        )
        receipt["sandbox_id"] = response.sandbox.metadata.id
        receipt["response_hex"] = response.SerializeToString().hex()
        fixture.save()
        if receipt["sandbox_id"] != owner["sandbox_id"]:
            raise RuntimeError("CA lifecycle mutation returned another sandbox")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = await asyncio.to_thread(
                client.rpc,
                "GetSandbox",
                client.pb.GetSandboxRequest(workspace_scope=client.scope, name=owner["name"]),
            )
            if current.sandbox.metadata.id != owner["sandbox_id"]:
                raise RuntimeError("CA fixture identity changed during stop/start")
            retain_runtime(fixture, owner)
            if current.sandbox.status.phase == phase:
                receipt["state"] = "observed"
                receipt["observed_hex"] = current.SerializeToString().hex()
                fixture.save()
                break
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("CA fixture did not reach requested lifecycle phase")


async def measure_ca_lifecycle(fixture, ip, ca_file, source, gateway):
    """Bracket fresh TLS connections with independent origin and runtime observations."""
    host = "allowed." + ip + ".nip.io"
    old = ca_file.read_bytes()
    new_file = ca_file.with_name("rotation-ca.pem")
    new = new_file.read_bytes()
    evidence = {
        "scope": "operator-driven CA rollover on native Docker; fresh connections only",
        "qualification_passed": False,
        "stages": [],
        "unqualified": [
            "in-flight connections",
            "automatic revocation",
            "gateway mTLS separation",
            "production trust management",
            "other drivers",
        ],
    }
    fixture.report["ca_lifecycle"] = evidence
    fixture.save()
    sandbox = await fixture.acquire(policy=native_https_policy(fixture.client.image, host, ip))
    evidence["sandbox_id"] = sandbox.owner["sandbox_id"]
    try:
        for label, bundle, restart, expected in (
            ("original_root", old, False, (200, "certificate_refused")),
            ("source_only_overlap", old + new, False, (200, "certificate_refused")),
            ("applied_overlap", old + new, True, (200, 200)),
            ("removed_original_root", new, True, ("certificate_refused", 200)),
            ("restored_original_root", old, True, (200, "certificate_refused")),
        ):
            row = {"stage": label, "source_sha256": hashlib.sha256(bundle).hexdigest(), "cases": []}
            evidence["stages"].append(row)
            source.write_bytes(bundle)
            fixture.save()
            if restart:
                await recycle(fixture, sandbox.owner, gateway, row)
            for port, root, outcome in zip(
                (8443, 8446), (ca_file, new_file), expected, strict=True
            ):
                path = "/anything?case=" + uuid.uuid4().hex
                request = {"method": "GET", "path": path, "host": f"{host}:{port}", "port": port}
                control = await asyncio.to_thread(
                    control_https,
                    host,
                    path,
                    port=port,
                    context=ssl.create_default_context(cafile=str(root)),
                )
                if control != request:
                    raise RuntimeError("CA lifecycle positive origin control failed")
                runtime_before = retain_runtime(fixture, sandbox.owner)
                before = await asyncio.to_thread(origin_get, ip, "/__tls_events")
                response = await sandbox.exec(
                    ["python", "-c", guest_request(f"https://{host}:{port}{path}", "GET")],
                    working_directory="/sandbox",
                    timeout=30,
                )
                if response.exit_code or response.stderr_bytes:
                    raise RuntimeError("CA lifecycle guest measurement failed")
                observed = json.loads(response.stdout)
                after = await asyncio.to_thread(origin_get, ip, "/__tls_events")
                runtime_after = retain_runtime(fixture, sandbox.owner)
                if after[: len(before)] != before:
                    raise RuntimeError("CA lifecycle origin journal continuity lost")
                events = after[len(before) :]
                case = {
                    "port": port,
                    "expected": outcome,
                    "control": control,
                    "guest": observed,
                    "origin_events": events,
                    "runtime_before": runtime_before,
                    "runtime_after": runtime_after,
                    "passed": False,
                }
                row["cases"].append(case)
                fixture.save()
                case["passed"] = runtime_continuity(
                    sandbox.owner["sandbox_id"], runtime_before, runtime_after
                ) and matches_native_https(observed, events, outcome, request)
                fixture.save()
                if not case["passed"]:
                    raise RuntimeError("unexpected CA lifecycle observation: " + label)
        evidence["qualification_passed"] = True
    finally:
        await fixture.dispose(sandbox.owner)
        fixture.save()
