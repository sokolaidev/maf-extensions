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


def result_text(result: Any) -> list[str]:
    """Keep bounded tool messages without serializing framework metadata."""
    items = result if isinstance(result, list) else [result]
    messages = []
    for item in items[:16]:
        value = item if isinstance(item, str) else getattr(item, "text", None)
        messages.append(value[:4096] if isinstance(value, str) else f"<{type(item).__name__}>")
    return messages


async def check(
    image: str, proxy_image: str | None = None, *, directory: Path | None = None
) -> dict[str, Any]:
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
    queries: list[tuple[str, dict[str, Any], dict[str, str]]] = [
        (
            category,
            {
                "sku": "Standard_D4s_v5",
                "region": "westeurope",
                "price_type": category,
                "include_savings_plan": category == "Consumption",
            },
            {"armSkuName": "Standard_D4s_v5", "region": "westeurope", "matched_by": "region"},
        )
        for category in ("Reservation", "Consumption")
    ]
    for name, service, product, location, family in (
        ("private-endpoint", "Virtual Network", "Virtual Network Private Link", "Global", None),
        ("network-zone", "Virtual Network", "Virtual Network Manager", "Zone 1", "network"),
        ("front-door-zone", "Azure Front Door Service", "Azure Front Door", "Zone 6", "front_door"),
        (
            "classic-zone",
            "Azure Front Door Service",
            "Azure Front Door Service",
            "Zone 1",
            "cdn_classic",
        ),
        ("dns-zone", "Azure DNS", "Azure DNS", "Zone 1", "dns"),
    ):
        predicate = f"serviceName eq '{service}' and productName eq '{product}'"
        if family is None:
            predicate += " and meterName eq 'Standard Private Endpoint'"
        else:
            # Isolate zone selection from separate Global base-fee meters.
            predicate += " and contains(location, 'Zone')"
        expected = {
            "serviceName": service,
            "productName": product,
            "region": location,
            "matched_by": "global" if family is None else "zone",
        }
        if family:
            expected.update(zone_family=family, zone=location)
        queries.append(
            (
                name,
                {"region": "westeurope", "price_type": "Consumption", "filter": predicate},
                expected,
            )
        )
    active_case = "pricing"
    arguments: dict[str, Any] = {}
    current_context: FunctionInvocationContext | None = None
    event_start = 0
    diagnostics: dict[str, Any] | None = None

    def record_failure(error: Exception, *, cleanup: bool = False) -> None:
        nonlocal diagnostics
        details = {"type": type(error).__name__, "message": str(error)[:4096]}
        if diagnostics is None:
            diagnostics = {
                "passed": False,
                "case": "scope-cleanup" if cleanup else active_case,
                "arguments": {} if cleanup else arguments,
                "imageId": image_id,
                "proxyImageId": proxy_id,
                "checkedAt": datetime.now(UTC).isoformat(),
                "error": details,
                "toolResult": result_text(current_context.result)
                if current_context is not None and not cleanup
                else [],
                "checks": cases,
                "events": [
                    {"seconds": event.seconds, "failure": event.failure, "unclean": event.unclean}
                    for event in events[event_start:]
                ]
                if not cleanup
                else [],
            }
        elif cleanup:
            diagnostics["cleanupError"] = details
        print("Azure pricing qualification failed: " + json.dumps(diagnostics), flush=True)
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "failure.json").write_text(
                json.dumps(diagnostics, indent=2) + "\n", "utf-8"
            )

    try:
        for name, arguments, expected in queries:
            active_case = name
            event_start = len(events)
            context = FunctionInvocationContext(
                function=tool,
                arguments=arguments,
            )
            current_context = context

            async def invoke() -> None:
                context.result = await tool.invoke(context=context)

            async def enforce() -> None:
                await policy.process(context, invoke)

            await tracker.process(context, enforce)
            items = context.result
            require(
                isinstance(items, list) and bool(items) and items[0].text == COMPLETED_TEXT,
                f"Pricing tool did not complete: {name}",
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
                bool(data["prices"]) and not data["truncated"],
                f"Expected complete narrow prices: {name}",
            )
            require(
                all(
                    row["priceType"] == arguments["price_type"]
                    and all(row.get(field) == value for field, value in expected.items())
                    and row["currencyCode"] == "USD"
                    and row["retailPrice"] >= 0
                    for row in data["prices"]
                ),
                f"Unexpected price identity or amount: {name}",
            )
            require(any(row["retailPrice"] > 0 for row in data["prices"]), "No positive price")
            if name == "Reservation":
                require(all(row.get("reservationTerm") for row in data["prices"]), "Lost terms")
            elif name == "Consumption":
                require(any(row.get("savingsPlan") for row in data["prices"]), "Lost savings plans")
            require(bool(events) and events[-1].unclean == 0, "Call cleanup failed")
            require(await asyncio.to_thread(remaining, scope) == ([], []), "Call resources remain")
            cases.append(
                {
                    "case": name,
                    "priceType": arguments["price_type"],
                    "expected": expected,
                    "rows": len(data["prices"]),
                    "integrity": "trusted",
                    "seconds": events[-1].seconds,
                }
            )

        active_case = "denial-probes"
        arguments = {}
        current_context = None
        event_start = len(events)
        key = SandboxKey(scope, "pricing", "pricing-check", "denial-probes")
        spec = replace(azure_pricing_sandbox_spec(PROBE_IMAGE), kind="azure-pricing-probe")
        sandbox = await backend.acquire(key, spec)
        try:
            for name, host, method in (
                ("unlisted-host", "example.com", "GET"),
                ("post-method", "prices.azure.com", "POST"),
            ):
                active_case = name
                arguments = {"host": host, "method": method}
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
        require(
            len(instances) == len(queries) and len(set(instances)) == len(queries),
            "Calls reused an instance",
        )
        require(await asyncio.to_thread(remaining, scope) == ([], []), "Probe resources remain")
    except Exception as error:
        record_failure(error)
        raise
    finally:
        try:
            purge = await router.dispose_scope(scope, "pricing")
            require(not purge.undisposed, "Pricing scope cleanup failed")
        except Exception as error:
            record_failure(error, cleanup=True)
            raise
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
    parser.add_argument("--diagnostics-directory", type=Path)
    args = parser.parse_args()
    result = asyncio.run(check(args.image, args.proxy_image, directory=args.diagnostics_directory))
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, "utf-8")
    print(text)


if __name__ == "__main__":
    main()
