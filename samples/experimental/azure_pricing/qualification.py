"""Opt-in live enforcement probes, using the pricing kind's unchanged specification."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import uuid4

from maf_sandbox import (
    CallerContext,
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    ToolCallEnded,
)
from maf_sandbox.bounded_exec import BoundedExec, SandboxExecOutputLimitExceeded
from maf_sandbox.maf import NOT_COMPLETED_TEXT, list_no_files
from maf_sandbox_azure_pricing import azure_pricing_sandbox_spec, make_azure_pricing_tools

HTTP_PROBE = """import json, ssl, sys, urllib.error, urllib.request
url = 'https://' + sys.argv[1] + '/api/retail/prices'
request = urllib.request.Request(url, method=sys.argv[2])
try:
    with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=20) as response:
        print(json.dumps({'status': response.status}))
except urllib.error.HTTPError as error:
    print(json.dumps({'status': error.code}))
except urllib.error.URLError as error:
    print(json.dumps({'denied': '403' in str(error.reason), 'reason': str(error.reason)[:512]}))
"""
DIRECT_PROBE = """import socket, ssl
established = False
try:
    with socket.create_connection(('1.1.1.1', 443), timeout=2) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname='one.one.one.one') as tls:
            established = True
            tls.sendall(b'GET / HTTP/1.1\\r\\nHost: one.one.one.one\\r\\nConnection: close\\r\\n\\r\\n')
            response = tls.recv(4096)
except ssl.SSLCertVerificationError:
    raise
except OSError:
    if established:
        raise
    print('blocked')
else:
    if b'403' in response.split(b'\\r\\n', 1)[0] and b'Blocked' in response:
        print('blocked')
    else:
        raise RuntimeError('Direct HTTPS traffic was not blocked: ' + repr(response[:256]))
"""


def denial_record(
    backend: str, records: Any, instance: str, host: str, method: str
) -> dict[str, Any] | None:
    """Match a denial from the host's proxy, not an origin's HTTP status."""
    if backend == "bubblewrap":
        for record in records:
            audit = record.get("audit", {})
            if (
                record.get("rejected_by") == "allowlist"
                and audit.get("action") == "reject"
                and audit.get("status_code") == 403
                and audit.get("host", "").split(":")[0] == host
                and audit.get("method") in ({method, "CONNECT"} if method == "GET" else {method})
            ):
                return record
    else:
        for record in records.get("blocked", []):
            if (
                record.get("sandbox_id") == instance
                and record.get("host", "").split(":")[0] == host
                and type(record.get("count")) is int
                and record["count"] > 0
            ):
                return record
    return None


class DeadlineProbe:
    """Observe a forced execution deadline while forwarding the sandbox's other surfaces."""

    seconds = 0.001

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.deadline: asyncio.Timeout | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sandbox, name)

    async def exec_bounded(
        self,
        command: str | Sequence[str],
        *,
        working_directory: str,
        timeout: float,
        max_output_bytes: int,
    ) -> ExecResult:
        if not isinstance(self.sandbox, BoundedExec):
            raise TypeError("Pricing requires BoundedExec")
        async with asyncio.timeout(self.seconds) as deadline:
            self.deadline = deadline
            return await self.sandbox.exec_bounded(
                command,
                working_directory=working_directory,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )

    def evidence(self) -> dict[str, object]:
        """Refuse generic failures or cancellation without this deadline's expiry."""
        if self.deadline is None or not self.deadline.expired():
            raise ValueError("Bounded execution did not reach its forced deadline")
        return {"started": True, "expired": True, "timeout_seconds": self.seconds}


async def probes(
    backend: SandboxBackend,
    image: str,
    verify_absent: Callable[[list[str]], Awaitable[None]],
    verify_denied: Callable[[Sandbox, str, str], Awaitable[dict[str, object]]],
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
        details: dict[str, object] = {}
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
                    raise ValueError(f"Disallowed {name} request was not denied: {value!r}")
                details["denial"] = await verify_denied(
                    sandbox,
                    "prices.azure.com" if name == "method" else "example.com",
                    "POST" if name == "method" else "GET",
                )
            elif name == "direct":
                result = await sandbox.exec_bounded(
                    ["/usr/bin/python3", "-c", DIRECT_PROBE],
                    working_directory=".",
                    timeout=10,
                    max_output_bytes=4096,
                )
                if result.exit_code != 0 or result.stdout.strip() != "blocked":
                    raise ValueError(
                        "Direct HTTPS was not refused: " + result.stdout + result.stderr
                    )
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
                **details,
            }
        )
    if len(set(instances)) != len(instances):
        raise ValueError("Probe sandbox identity was reused")
    results.extend(await tool_failures(backend, image, verify_absent))
    return results


async def tool_failures(
    backend: SandboxBackend,
    image: str,
    verify_absent: Callable[[list[str]], Awaitable[None]],
) -> list[dict[str, object]]:
    """Require actual pricing-call cleanup before the fallback scope purge can hide a leak."""
    results: list[dict[str, object]] = []
    instances: list[str] = []
    for case in ("tool_timeout", "tool_cancellation"):
        scope = "pricing-failure-" + uuid4().hex
        acquired = asyncio.Event()
        calls: list[ToolCallEnded] = []
        deadline_probe: DeadlineProbe | None = None

        class Observer(SandboxObserver):
            def tool_call_ended(self, event: ToolCallEnded) -> None:
                calls.append(event)

        class Router(SandboxRouter):
            async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
                nonlocal deadline_probe
                sandbox = await super().acquire(key, spec, **kwargs)
                if not key.call_id or sandbox.instance_id in instances:
                    raise ValueError("Failure call did not acquire a fresh call-scoped sandbox")
                instances.append(sandbox.instance_id)
                acquired.set()
                if case == "tool_timeout":
                    deadline_probe = DeadlineProbe(sandbox)
                    return cast(Sandbox, deadline_probe)
                return sandbox

        router = (
            Router([backend], min_isolation=Isolation.CONTAINER, observer=Observer())
            if backend.name == "bubblewrap"
            else Router([backend], observer=Observer())
        )
        [tool] = make_azure_pricing_tools(
            router,
            "pricing",
            CallerContext(lambda: scope, lambda: "failures", list_no_files),
            image=image,
            exec_timeout_seconds=90,
        )
        started = time.monotonic()
        task = asyncio.create_task(
            tool.func(sku="Standard_D4s_v5", region="westeurope", currency="USD")
        )
        try:
            details: dict[str, object] = {}
            if case == "tool_cancellation":
                ready = asyncio.create_task(acquired.wait())
                try:
                    done, _ = await asyncio.wait(
                        {task, ready}, timeout=180, return_when=asyncio.FIRST_COMPLETED
                    )
                    if ready not in done or task.done():
                        raise ValueError("Cancellation did not reach an active acquired call")
                finally:
                    ready.cancel()
                    await asyncio.gather(ready, return_exceptions=True)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise ValueError("Pricing call ignored cancellation")
            else:
                answer = await task
                if NOT_COMPLETED_TEXT not in [item.text for item in answer]:
                    raise ValueError("The forced pricing deadline did not return incomplete")
                if deadline_probe is None:
                    raise ValueError("Pricing did not enter bounded execution")
                details["execution"] = deadline_probe.evidence()
            if not acquired.is_set() or len(calls) != 1 or calls[0].unclean != 0:
                raise ValueError("Missing acquired-call cleanup evidence")
            await verify_absent(instances)
            results.append(
                {
                    "name": case,
                    "instance": instances[-1],
                    "seconds": time.monotonic() - started,
                    "disposed": True,
                    **details,
                }
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if (await router.dispose_scope(scope, "failures")).undisposed is not None:
                raise RuntimeError("Failure-call fallback purge failed")
    return results
