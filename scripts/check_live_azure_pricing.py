"""Check the pinned pricing image through Docker's real egress proxy and FIDES middleware."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_framework import FunctionInvocationContext
from agent_framework.security import (
    LabelTrackingFunctionMiddleware,
    PolicyEnforcementFunctionMiddleware,
)
from maf_sandbox import (
    CallerContext,
    Isolation,
    Sandbox,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    ToolCallEnded,
)
from maf_sandbox.maf import COMPLETED_TEXT, list_no_files
from maf_sandbox_azure_pricing import azure_pricing_sandbox_spec, make_azure_pricing_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig, proxy_build_context

PROBE_IMAGE = "python:3.13-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962"
_PROBE = """import http.client, json, os, ssl, sys
from urllib.parse import urlsplit
proxy = urlsplit(os.environ['HTTPS_PROXY'])
client = http.client.HTTPSConnection(proxy.hostname, proxy.port,
    context=ssl.create_default_context(), timeout=10)
client.set_tunnel(sys.argv[1], 443)
try:
    client.request(sys.argv[2], '/api/retail/prices')
    response = client.getresponse()
    print(json.dumps({'denied': response.status == 403}))
except OSError as error:
    print(json.dumps({'denied': 'Tunnel connection failed: 403' in str(error)}))
finally:
    client.close()
"""


def require(condition: bool, message: str) -> None:
    """Keep qualification checks active under optimized Python."""
    if not condition:
        raise ValueError(message)


def docker(*args: str) -> str:
    """Read bounded Docker control output without inheriting terminal streams."""
    return subprocess.check_output(
        ["docker", *args], text=True, encoding="utf-8", timeout=60
    ).strip()


def remaining(scope: str) -> tuple[list[str], list[str]]:
    """Count stopped containers and internal networks as retained resources."""
    selector = f"label=maf-sandbox.scope={scope}"
    return (
        docker("ps", "-aq", "--filter", selector).split(),
        docker("network", "ls", "-q", "--filter", selector).split(),
    )


async def check(image: str, proxy_image: str | None = None) -> dict[str, Any]:
    """Measure actual public pricing, trust labels, egress denials and per-call cleanup."""
    if proxy_image is None:
        proxy_image = "maf-azure-pricing-proxy:" + uuid4().hex
        await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "build",
                "-t",
                proxy_image,
                str(proxy_build_context()),
            ],
            check=True,
            timeout=1200,
        )
    image_id = await asyncio.to_thread(docker, "image", "inspect", image, "--format", "{{.Id}}")
    proxy_id = await asyncio.to_thread(
        docker, "image", "inspect", proxy_image, "--format", "{{.Id}}"
    )
    backend = await DockerSandboxBackend.create(
        DockerSandboxConfig(
            egress_proxy_image=proxy_id,
            memory="1g",
            cpus=2,
        )
    )
    scope = "azure-pricing-check-" + uuid4().hex
    events: list[ToolCallEnded] = []
    instances: list[str] = []

    class Observer(SandboxObserver):
        def tool_call_ended(self, event: ToolCallEnded) -> None:
            events.append(event)

    class Router(SandboxRouter):
        async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
            sandbox = await super().acquire(key, spec, **kwargs)
            inspected = json.loads(await asyncio.to_thread(docker, "inspect", sandbox.instance_id))[
                0
            ]
            require(inspected["Image"] == image_id, "Pricing image identity differs")
            instances.append(inspected["Id"])
            return sandbox

    router = Router([backend], min_isolation=Isolation.CONTAINER, observer=Observer())
    [tool] = make_azure_pricing_tools(
        router,
        "pricing-check",
        CallerContext(lambda: scope, lambda: "pricing", list_no_files),
        image=image_id,
        egress_integrity=SourceIntegrity.TRUSTED,
        outbound_max_confidentiality="public",
        exec_timeout_seconds=90,
    )
    tool.additional_properties["confidentiality"] = "public"
    tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=False)
    policy = PolicyEnforcementFunctionMiddleware(block_on_violation=True)
    cases: list[dict[str, Any]] = []
    try:
        for category in ("Reservation", "Consumption"):
            context = FunctionInvocationContext(
                function=tool,
                arguments={
                    "sku": "Standard_D4s_v5",
                    "region": "westeurope",
                    "price_type": category,
                    "include_savings_plan": category == "Consumption",
                },
            )

            async def invoke() -> None:
                context.result = await tool.invoke(context=context)

            async def enforce() -> None:
                await policy.process(context, invoke)

            await tracker.process(context, enforce)
            items = context.result
            require(
                isinstance(items, list) and items[0].text == COMPLETED_TEXT,
                "Pricing tool did not complete",
            )
            reports = [item for item in items if item.text and item.text.startswith('{"prices":')]
            require(len(reports) == 1, "Expected one pricing report")
            report = reports[0]
            label = report.additional_properties.get("security_label", {})
            require(
                label.get("integrity") == "trusted" and label.get("confidentiality") == "public",
                "Price report was not promoted through framework call evidence",
            )
            data = json.loads(report.text)
            require(
                bool(data["prices"]) and not data["truncated"], "Expected complete narrow prices"
            )
            require(
                all(
                    row["priceType"] == category
                    and row["region"] == "westeurope"
                    and row["armSkuName"] == "Standard_D4s_v5"
                    and row["currencyCode"] == "USD"
                    and row["retailPrice"] > 0
                    for row in data["prices"]
                ),
                "Unexpected price identity or amount",
            )
            if category == "Reservation":
                require(all(row.get("reservationTerm") for row in data["prices"]), "Lost terms")
            else:
                require(any(row.get("savingsPlan") for row in data["prices"]), "Lost savings plans")
            require(bool(events) and events[-1].unclean == 0, "Call cleanup failed")
            require(await asyncio.to_thread(remaining, scope) == ([], []), "Call resources remain")
            cases.append(
                {
                    "priceType": category,
                    "rows": len(data["prices"]),
                    "integrity": "trusted",
                    "seconds": events[-1].seconds,
                }
            )

        key = SandboxKey(scope, "pricing", "pricing-check", "denial-probes")
        spec = replace(azure_pricing_sandbox_spec(PROBE_IMAGE), kind="azure-pricing-probe")
        sandbox = await backend.acquire(key, spec)
        try:
            for name, host, method in (
                ("unlisted-host", "example.com", "GET"),
                ("post-method", "prices.azure.com", "POST"),
            ):
                result = await sandbox.exec(
                    ["python3", "-I", "-c", _PROBE, host, method],
                    working_directory=".",
                    timeout=15,
                )
                require(
                    result.exit_code == 0 and json.loads(result.stdout_bytes) == {"denied": True},
                    f"Missing explicit proxy denial: {name}",
                )
                cases.append({"probe": name, "denied": True})
        finally:
            failure = await backend.dispose(key, kind=spec.kind, instance_id=sandbox.instance_id)
            require(failure is None, "Probe disposal failed")
        require(len(instances) == 2 and len(set(instances)) == 2, "Calls reused an instance")
        require(await asyncio.to_thread(remaining, scope) == ([], []), "Probe resources remain")
    finally:
        purge = await router.dispose_scope(scope, "pricing")
        require(not purge.undisposed, "Pricing scope cleanup failed")
    return {
        "backend": "docker",
        "imageId": image_id,
        "proxyImageId": proxy_id,
        "denialProbeImage": PROBE_IMAGE,
        "checkedAt": datetime.now(UTC).isoformat(),
        "checks": cases,
        "instances": instances,
        "containersRemaining": [],
        "networksRemaining": [],
        "sdkSource": "checkout",
        "livePricingVerified": True,
        "acasVerified": False,
        "wslcVerified": False,
        "productionVerified": False,
    }


def main() -> None:
    """Write local candidate-bound evidence; publishing remains a separate operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--proxy-image")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(check(args.image, args.proxy_image))
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, "utf-8")
    print(text)


if __name__ == "__main__":
    main()
