"""Opt-in live enforcement probes, using the pricing kind's unchanged specification."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from maf_sandbox import SandboxBackend, SandboxKey
from maf_sandbox.bounded_exec import BoundedExec, SandboxExecOutputLimitExceeded
from maf_sandbox_azure_pricing import azure_pricing_sandbox_spec

HTTP_PROBE = """import json, ssl, sys, urllib.error, urllib.request
url = 'https://' + sys.argv[1] + '/api/retail/prices'
request = urllib.request.Request(url, method=sys.argv[2])
try:
    with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=20) as response:
        print(json.dumps({'status': response.status}))
except urllib.error.HTTPError as error:
    print(json.dumps({'status': error.code}))
except urllib.error.URLError as error:
    print(json.dumps({'denied': '403' in str(error.reason)}))
"""
DIRECT_PROBE = """import socket
try:
    socket.create_connection(('1.1.1.1', 443), timeout=2).close()
except OSError:
    print('blocked')
else:
    raise RuntimeError('Direct networking bypassed the proxy')
"""


async def probes(
    backend: SandboxBackend,
    image: str,
    verify_absent: Callable[[list[str]], Awaitable[None]],
) -> list[dict[str, object]]:
    """Verify denied traffic, output overflow, timeout and cancellation with disposal."""
    scope = "pricing-probes-" + uuid4().hex
    spec = azure_pricing_sandbox_spec(image=image)
    results: list[dict[str, object]] = []
    instances: list[str] = []
    for name in ("method", "destination", "direct", "output", "timeout", "cancellation"):
        key = SandboxKey(scope, "probes", "pricing", call_id=name)
        sandbox = await backend.acquire(key, spec)
        instances.append(sandbox.instance_id)
        started = time.monotonic()
        try:
            if not isinstance(sandbox, BoundedExec):
                raise TypeError("Pricing requires BoundedExec")
            if name in ("method", "destination"):
                result = await sandbox.exec_bounded(
                    [
                        "/usr/bin/python3",
                        "-c",
                        HTTP_PROBE,
                        "prices.azure.com" if name == "method" else "example.com",
                        "POST" if name == "method" else "GET",
                    ],
                    working_directory=".",
                    timeout=30,
                    max_output_bytes=4096,
                )
                if result.exit_code != 0:
                    raise ValueError("HTTP probe failed: " + result.stderr)
                value = json.loads(result.stdout)
                if value.get("status") != 403 and value.get("denied") is not True:
                    raise ValueError("Disallowed HTTP request was not denied")
            elif name == "direct":
                result = await sandbox.exec_bounded(
                    ["/usr/bin/python3", "-c", DIRECT_PROBE],
                    working_directory=".",
                    timeout=10,
                    max_output_bytes=4096,
                )
                if result.exit_code != 0 or result.stdout.strip() != "blocked":
                    raise ValueError("Direct networking was not refused")
            elif name == "output":
                try:
                    await sandbox.exec_bounded(
                        ["/usr/bin/python3", "-c", "print('x' * 100000)"],
                        working_directory=".",
                        timeout=10,
                        max_output_bytes=128,
                    )
                except SandboxExecOutputLimitExceeded:
                    pass
                else:
                    raise ValueError("Output budget was not enforced")
            else:
                task = asyncio.create_task(
                    sandbox.exec_bounded(
                        ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
                        working_directory=".",
                        timeout=1 if name == "timeout" else 30,
                        max_output_bytes=4096,
                    )
                )
                if name == "cancellation":
                    await asyncio.sleep(0.5)
                    task.cancel()
                try:
                    await task
                except (TimeoutError, asyncio.CancelledError) as error:
                    expected = TimeoutError if name == "timeout" else asyncio.CancelledError
                    if not isinstance(error, expected):
                        raise
                else:
                    raise ValueError("Execution did not stop")
        finally:
            failure = await backend.dispose(key, kind=spec.kind, instance_id=sandbox.instance_id)
            if failure is not None:
                raise RuntimeError("Probe disposal failed: " + str(failure))
            await verify_absent(instances)
        results.append(
            {
                "name": name,
                "instance": sandbox.instance_id,
                "seconds": time.monotonic() - started,
                "disposed": True,
            }
        )
    if len(set(instances)) != len(instances):
        raise ValueError("Probe sandbox identity was reused")
    return results
