"""Opt-in public HTTPS conformance through the real worker and pinned guest."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from https_method_fixture import CLOUDFLARED_VERSION, public_origin
from https_method_guest import RawHttpSubject
from maf_sandbox import Capability, Egress, EgressRule, HttpMethod, SandboxKey, SandboxSpec
from maf_sandbox.conformance import assert_egress_methods_conformance

from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightSandboxConfig, _backend

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_HYPERLIGHT_LIVE") != "1"
    or os.environ.get("MAF_HYPERLIGHT_HTTPS_LIVE") != "1"
    or sys.platform not in {"win32", "linux"},
    reason="requires both live opt-ins, a public HTTPS relay, and Windows WHP or Linux KVM",
)
METHODS = (
    HttpMethod.GET,
    HttpMethod.HEAD,
    HttpMethod.POST,
    HttpMethod.PUT,
    HttpMethod.PATCH,
    HttpMethod.DELETE,
    HttpMethod.OPTIONS,
)


def test_https_methods_and_get_body_through_real_worker(tmp_path: Path):
    binary = Path(os.environ["MAF_HYPERLIGHT_CLOUDFLARED"])
    evidence: dict[str, object] = {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "cloudflared": CLOUDFLARED_VERSION,
        "versions": {
            name: version(name)
            for name in (
                "maf-sandbox",
                "maf-sandbox-hyperlight",
                "hyperlight-sandbox",
                "hyperlight-sandbox-backend-wasm",
                "hyperlight-sandbox-python-guest",
            )
        },
    }
    with public_origin(binary, tmp_path) as (origin, url):
        host = urlsplit(url).hostname
        assert host is not None
        key = SandboxKey("https-method-conformance", origin.prefix, "probe")
        spec = SandboxSpec(
            kind="python",
            work_dir=None,
            requires=frozenset({Capability.RUN_CODE}),
            egress=Egress.ALLOWLIST,
            egress_allow=(host,),
        )
        backend = HyperlightSandboxBackend(
            HyperlightSandboxConfig(linux_cgroup_root=os.environ.get("MAF_HYPERLIGHT_CGROUP_ROOT"))
        )
        workers = []

        async def subject(policy: SandboxSpec, name: str) -> RawHttpSubject:
            sandbox = cast(
                "_backend._HyperlightSandbox",
                await backend.acquire(replace(key, thread_id=name), policy),
            )
            workers.append(sandbox.worker)
            return RawHttpSubject(sandbox, backend.declarations.capabilities)

        async def check():
            try:
                assert backend.declarations.egress_method_tokens == frozenset(METHODS)
                control = await subject(spec, "control")
                for method in METHODS:
                    path = origin.prefix + "control-" + method
                    assert await control.request(method, url + path) == {"status": 200}
                    assert [r.method for r in origin.matching(path)] == [method]

                for allowed in METHODS:
                    scoped_spec = replace(
                        spec, egress_allow=(EgressRule(host, methods=(allowed,)),)
                    )
                    scoped = await subject(scoped_spec, allowed)
                    for requested in METHODS:
                        path = origin.prefix + f"{allowed}-{requested}"
                        response = await scoped.request(requested, url + path)
                        records = origin.matching(path)
                        if requested == allowed:
                            assert response == {"status": 200}, response
                            assert [r.method for r in records] == [requested]
                        else:
                            assert "HttpRequestDenied" in str(response.get("error")), response
                            assert not records, records

                    if allowed == HttpMethod.GET:
                        await assert_egress_methods_conformance(
                            scoped,
                            control,
                            allowed_url=url + origin.prefix + "shared",
                            request_timeout=15,
                        )
                        assert [r.method for r in origin.matching(origin.prefix + "shared")] == [
                            "POST",
                            "GET",
                        ]
                        payload = b"synthetic-get-body-" + bytes(range(256)) * 40
                        post_path = origin.prefix + "post-body-control"
                        assert await control.request("POST", url + post_path, payload=payload) == {
                            "status": 200
                        }
                        [post_record] = origin.matching(post_path)
                        assert post_record.body == payload
                        automatic_path = origin.prefix + "get-body-automatic"
                        assert await scoped.request(
                            "GET", url + automatic_path, payload=payload
                        ) == {"status": 200}
                        [automatic_record] = origin.matching(automatic_path)
                        assert automatic_record.body in (b"", payload)
                        path = origin.prefix + "get-body?synthetic=query"
                        assert await scoped.request(
                            "GET", url + path, payload=payload, content_length=True
                        ) == {"status": 200}
                        records = origin.matching(path)
                        assert len(records) == 1 and records[0].body == payload
                        evidence["get_body"] = {
                            "bytes": len(records[0].body),
                            "recorder_transfer_encoding": records[0].transfer_encoding,
                            "recorder_content_length": records[0].content_length,
                            "automatic_framing_bytes": len(automatic_record.body),
                            "post_control_bytes": len(post_record.body),
                            "post_control_transfer_encoding": post_record.transfer_encoding,
                            "note": "TLS terminates at the relay; recorder framing is after relay forwarding",
                        }
                    assert await backend.dispose(replace(key, thread_id=allowed)) is None

                for method in ("TRACE", "CONNECT", "PROPFIND", "X-CUSTOM"):
                    path = origin.prefix + "unsupported-" + method
                    response = await control.request(method, url + path)
                    expected = (
                        "HttpRequestDenied"
                        if method in {"TRACE", "CONNECT"}
                        else "HttpRequestMethodInvalid"
                    )
                    assert expected in str(response.get("error")), response
                    assert not origin.matching(path)
                evidence.update(allowed_cases=7, denied_cases=42, unrestricted_controls=7)
            finally:
                await backend.aclose()
                assert not backend._sandboxes
                assert all(
                    w.process.poll() is not None and not w._drainer.is_alive() for w in workers
                )

        asyncio.run(check())
    evidence["cleanup"] = "workers, pipes, recording listener and tunnel stopped"
    print("HTTPS_METHOD_EVIDENCE " + json.dumps(evidence, sort_keys=True))
