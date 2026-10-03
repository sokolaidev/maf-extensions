"""Bounded, opt-in native-channel research; this does not declare HOST_TOOLS support."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import hashlib
import importlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from maf_sandbox import (
    HostToolCalled,
    HostToolRegistry,
    HostToolRun,
    SandboxKey,
    SandboxObserver,
    SandboxSpec,
    TransferLimits,
    guest_run_layout,
    host_tool_calls_over_exec,
    host_tool_shim,
    sandbox_tool,
)
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_hyperlight import HyperlightSandboxConfig
from maf_sandbox_hyperlight._process import Worker

PACKAGES = (
    "hyperlight-sandbox",
    "hyperlight-sandbox-backend-wasm",
    "hyperlight-sandbox-python-guest",
)
WIRE_LIMIT = 512 * 1024
APPLICATION_LIMIT = 8 * 1024
FIRST_EOF_REQUEST_VALUE_BYTES = 16_200
CONTEXT = contextvars.ContextVar("probe_run", default="unbound")
SHARED_PROGRAM = """import maf_host_tools as h
print(h.echo(value={"nested": [1, True, None, "é"]}))
try:
 h.call("missing")
except h.HostToolError:
 print("refused")"""

FACADE = """
import sys, json
maf_host_tools = type(sys)("maf_host_tools")
class HostToolError(RuntimeError):
    pass
def _maf_call(name, **arguments):
    payload = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)
    response = json.loads(call_tool("maf_dispatch", payload=payload))
    if "value" in response:
        return response["value"]
    raise HostToolError(response["refusal"])
maf_host_tools.call = _maf_call
maf_host_tools.HostToolError = HostToolError
def _maf_echo(**arguments):
    return _maf_call("echo", **arguments)
maf_host_tools.echo = _maf_echo
sys.modules["maf_host_tools"] = maf_host_tools
"""


def encode(message: dict[str, Any]) -> bytes:
    """Bound the actual IPC bytes, including escaping and the delimiter."""
    raw = (json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    if len(raw) > WIRE_LIMIT:
        raise ValueError("IPC envelope exceeds the probe limit")
    return raw


def decode(raw: bytes) -> dict[str, Any]:
    """Require a complete bounded JSON object."""
    if len(raw) > WIRE_LIMIT or not raw.endswith(b"\n"):
        raise ValueError("incomplete or oversized IPC envelope")
    value = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("IPC envelope must be an object")
    return value


def reject_constant(value: str) -> Any:
    """Reject JSON extensions that cannot cross the strict transport."""
    raise ValueError(f"non-JSON constant: {value}")


def worker_main() -> None:
    """Own the native sandbox on one thread after the parent establishes containment."""
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0) as channel:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

        def send(message: dict[str, Any]) -> None:
            channel.write(encode(message))

        def receive() -> dict[str, Any]:
            return decode(sys.stdin.buffer.readline(WIRE_LIMIT + 1))

        initial = receive()
        assert initial["op"] == "init"
        assert all(version(package) == "0.7.0" for package in PACKAGES)
        sdk = importlib.import_module("hyperlight_sandbox")
        sandbox = sdk.Sandbox(
            backend="wasm", module="python_guest.path", heap_size="400Mi", stack_size="200Mi"
        )
        generation: str | None = None
        sequence = 0

        def bridge(payload: str) -> str:
            nonlocal sequence
            if generation is None:
                raise RuntimeError("no live run")
            sequence += 1
            send({"op": "callback", "run": generation, "seq": sequence, "payload": payload})
            answer = receive()
            if answer.get("run") != generation or answer.get("seq") != sequence:
                raise RuntimeError("stale callback response")
            response = answer["response"]
            assert isinstance(response, str)
            send({"op": "prepared", "run": generation, "seq": sequence})
            if answer.get("fail_before_return"):
                raise RuntimeError("synthetic failure before native marshalling")
            return response

        sandbox.register_tool("maf_dispatch", bridge)
        warm = sandbox.run(FACADE)
        if warm.exit_code != 0:
            send({"op": "failed_init", "stderr": warm.stderr})
            return
        baseline = sandbox.snapshot()
        send({"op": "ready", "versions": {name: version(name) for name in PACKAGES}})
        while True:
            message = receive()
            operation = message["op"]
            if operation == "late_registration":
                try:
                    sandbox.register_tool("late", lambda: 1)
                except Exception as error:  # noqa: BLE001
                    send({"op": "late_registration", "error": type(error).__name__})
                else:
                    send({"op": "late_registration", "error": None})
                continue
            if operation == "unbound":
                result = sandbox.run('call_tool("maf_dispatch", payload="{}")')
            else:
                assert operation == "run"
                sandbox.restore(baseline)
                generation, sequence = message["run"], 0
                try:
                    result = sandbox.run(message["code"])
                finally:
                    generation = None
            send(
                {
                    "op": "result",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                }
            )


class ProbeWorker(Worker):
    """Reuse the adapter's memory and owner-death boundary with a research-only worker."""

    @staticmethod
    def command() -> list[str]:
        return [sys.executable, "-I", "-u", str(Path(__file__).resolve()), "--worker"]

    async def exchange(
        self, message: dict[str, Any], service: Any, timeout: float
    ) -> dict[str, Any]:
        """Bridge synchronous worker I/O to the active event loop within one deadline."""
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + timeout

        def transfer() -> dict[str, Any]:
            self._input.write(encode(message))
            self._input.flush()
            while True:
                raw = self._output.readline(WIRE_LIMIT + 1)
                if not raw:
                    raise EOFError("worker closed callback pipe")
                received = decode(raw)
                if received["op"] not in {"callback", "prepared"}:
                    return received
                future = asyncio.run_coroutine_threadsafe(service(received), loop)
                try:
                    reply = future.result(timeout=max(0.001, deadline - time.monotonic()))
                except BaseException:
                    future.cancel()
                    raise
                if reply is not None:
                    self._input.write(encode(reply))
                    self._input.flush()

        task = asyncio.create_task(asyncio.to_thread(transfer))
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(task)
        except BaseException:
            self.close()
            with contextlib.suppress(Exception):
                await task
            raise


class Policy:
    """Synthetic host tools behind the real policy path; transport evidence stays separate."""

    def __init__(
        self, run_id: str, *, cap: int = 16, response_bytes: int = APPLICATION_LIMIT
    ) -> None:
        self.events: list[dict[str, Any]] = []
        self.stopped = asyncio.Event()
        self.release = asyncio.Event()
        self.pending: set[asyncio.Task[Any]] = set()
        self.sequence = 0
        self.run_id = run_id
        self.timeout = 0.25
        self.raw_response: str | None = None
        self.fail_before_return = False
        self.reject_generation = False
        self.request_limit = APPLICATION_LIMIT

        events = self.events

        class Observer(SandboxObserver):
            def host_tool_called(self, event: HostToolCalled) -> None:
                events.append(
                    {
                        "stage": "core_observation",
                        "outcome": event.outcome,
                        "bytes": event.response_bytes,
                    }
                )

        @contextlib.contextmanager
        def observe(run: HostToolRun, name: object):
            self.events.append({"stage": "policy_enter", "run": run.run_id, "name": name})
            try:
                yield
            finally:
                self.events.append({"stage": "policy_exit", "context": CONTEXT.get()})

        registry = HostToolRegistry(
            require_declared=True,
            max_host_tool_calls_per_run=cap,
            response_limits=TransferLimits(
                max_files=cap,
                max_bytes_per_file=response_bytes,
                max_total_bytes=cap * response_bytes,
            ),
            host_tool_calls_observer=observe,
            observer=Observer(),
        )

        @sandbox_tool(source=None, sink=None, identity=None)
        async def echo(value: Any) -> Any:
            return value

        @sandbox_tool(source=None, sink=None, identity=None)
        async def payload(size: int, character: str = "x") -> str:
            return character * size

        @sandbox_tool(source=None, sink=None, identity=None)
        async def wait(stubborn: bool = False) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if stubborn:
                    await self.release.wait()
                raise
            finally:
                self.stopped.set()
            return "completed"

        for tool in (echo, payload, wait):
            registry.register(tool)
        registry.aggregate()
        self.registry = registry
        self.run = HostToolRun(registry, run_id=run_id, key=SandboxKey("probe", "thread", "agent"))

    async def service(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Refuse stale messages before policy dispatch and bound cooperative cancellation."""
        if message["op"] == "prepared":
            if message["run"] != self.run_id or message["seq"] != self.sequence:
                raise ValueError("stale prepared response")
            self.events.append({"stage": "worker_prepared", "seq": message["seq"]})
            return None
        if (
            self.reject_generation
            or message["run"] != self.run_id
            or message["seq"] != self.sequence + 1
        ):
            raise ValueError("stale or duplicate callback")
        self.sequence += 1
        size = len(message["payload"].encode("utf-8"))
        self.events.append({"stage": "request_bytes", "utf8": size})
        if size > self.request_limit:
            return {
                "run": message["run"],
                "seq": message["seq"],
                "response": '{"refusal":"Error: request exceeds transport limit"}',
            }
        request = json.loads(message["payload"], parse_constant=reject_constant)
        token = CONTEXT.set(self.run_id)
        prepared: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def publish(result: Any) -> None:
            prepared.set_result(result)
            # The pinned native SDK exposes no trusted per-response acceptance hook.
            await asyncio.Event().wait()

        try:
            task = asyncio.create_task(
                self.run.call(
                    request["name"], request["arguments"], framing_bytes=32, publish=publish
                )
            )
            self.pending.add(task)
            done, _ = await asyncio.wait(
                {task, prepared}, timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=self.timeout)
                if not done:
                    raise TimeoutError("host callback did not stop within cleanup budget")
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                response = '{"refusal":"Error: host tool timed out; effects may have occurred"}'
                self.events.append({"stage": "timeout_refusal", "stopped": self.stopped.is_set()})
            else:
                result = prepared.result() if prepared.done() else task.result()
                response = (
                    '{"value":' + result.value_json + "}"
                    if result.value_json is not None
                    else json.dumps({"refusal": result.refusal}, ensure_ascii=False)
                )
                self.events.append({"stage": "core_prepared", "ok": result.ok})
            if task.done():
                self.pending.discard(task)
            if self.raw_response is not None:
                response = self.raw_response
            reply = {
                "run": message["run"],
                "seq": message["seq"],
                "response": response,
                "fail_before_return": self.fail_before_return,
            }
            self.events.append(
                {
                    "stage": "response_bytes",
                    "utf8": len(response.encode("utf-8")),
                    "native_json_utf8_estimate": len(
                        json.dumps(response, ensure_ascii=False).encode("utf-8")
                    ),
                    "ipc_bytes": len(encode(reply)),
                }
            )
            return reply
        finally:
            CONTEXT.reset(token)

    async def cleanup(self) -> None:
        """Release synthetic stubborn tasks only after the worker is retired."""
        self.run.close()
        self.release.set()
        for task in self.pending:
            task.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
        self.pending.clear()


async def probe_case(name: str, code: str, **options: Any) -> dict[str, Any]:
    """Run a fresh contained native guest and preserve bounded observations."""
    worker = ProbeWorker(
        HyperlightSandboxConfig(linux_cgroup_root=os.environ.get("MAF_HYPERLIGHT_CGROUP_ROOT"))
    )
    policy = Policy(
        name,
        cap=options.get("cap", 16),
        response_bytes=options.get("response_bytes", APPLICATION_LIMIT),
    )
    policies = [policy]
    policy.raw_response = options.get("raw_response")
    policy.fail_before_return = options.get("fail_before_return", False)
    policy.reject_generation = options.get("reject_generation", False)
    policy.request_limit = options.get("request_limit", APPLICATION_LIMIT)
    report: dict[str, Any] = {
        "case": name,
        "bypasses_response_policy": policy.raw_response is not None,
    }
    started = time.monotonic()
    try:
        ready = await worker.exchange({"op": "init"}, policy.service, 30)
        if ready["op"] != "ready":
            raise RuntimeError(f"native initialization failed: {ready}")
        report["initialized"] = True
        running = asyncio.create_task(
            worker.exchange(
                {"op": "run", "run": name, "code": code}, policy.service, options.get("timeout", 5)
            )
        )
        if options.get("cancel"):
            await asyncio.sleep(0.25)
            running.cancel()
        try:
            report["result"] = await running
        except asyncio.CancelledError:
            report["cancelled"] = True
        if options.get("reuse"):
            second = Policy(name + "-next")
            policies.append(second)
            report["reuse_events"] = second.events
            report["reuse"] = await worker.exchange(
                {"op": "run", "run": second.run_id, "code": code}, second.service, 5
            )
            report["unbound"] = await worker.exchange({"op": "unbound"}, second.service, 5)
            report["late_registration"] = await worker.exchange(
                {"op": "late_registration"}, second.service, 5
            )
    except Exception as error:  # noqa: BLE001
        report["error"] = type(error).__name__
        report["error_detail"] = str(error)[:500]
    finally:
        report["worker_exit_before_cleanup"] = worker.process.poll()
        worker.close()
        report["worker_stderr"] = worker._stderr.decode("utf-8", errors="replace")
        for active in policies:
            await active.cleanup()
        report["reaped"] = worker.process.poll() is not None and not worker._drainer.is_alive()
        report["events"] = policy.events
        report["seconds"] = round(time.monotonic() - started, 3)
    return report


async def docker_comparison() -> dict[str, Any]:
    """Execute the same guest API program through the shipped Docker file transport."""
    backend = DockerSandboxBackend(DockerSandboxConfig())
    key = SandboxKey("channel-probe-" + uuid.uuid4().hex, "thread", "agent")
    spec = SandboxSpec(kind="channel-probe", image="python:3.13-slim", work_dir="/maf-sandbox/work")
    policy = Policy("docker-shared-api")
    layout = guest_run_layout("/maf-sandbox/work/probe")
    report: dict[str, Any] = {"case": "docker-shared-api"}
    try:
        sandbox = await backend.acquire(key, spec)
        assert spec.work_dir is not None
        await sandbox.write_file(layout.program, SHARED_PROGRAM, working_directory=spec.work_dir)
        await sandbox.write_file(
            layout.shim, host_tool_shim({"echo"}), working_directory=spec.work_dir
        )
        result = await host_tool_calls_over_exec(sandbox, policy.run, layout, timeout=30)
        report["result"] = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
    except Exception as error:  # noqa: BLE001
        report["error"] = type(error).__name__
        report["error_detail"] = str(error)[:500]
    finally:
        failure = await backend.dispose(key)
        report["reaped"] = failure is None
        report["events"] = policy.events
    return report


def case_specs() -> list[tuple[str, str, dict[str, Any]]]:
    """Measure useful contract cases before probing unsafe native envelope boundaries."""
    cases: list[tuple[str, str, dict[str, Any]]] = [
        (
            "shared-api",
            SHARED_PROGRAM,
            {"reuse": True},
        ),
        (
            "profile",
            'import sys, json\nprint(sys.version)\nfor name in ["json", "math", "re", "sys", "types", "asyncio", "datetime", "statistics", "pickle", "__future__", "threading", "socket", "os"]:\n try:\n  __import__(name)\n  print(name + ":yes")\n except ImportError:\n  print(name + ":no")',
            {},
        ),
        (
            "call-cap",
            'import maf_host_tools as h\nprint(h.echo(value=1))\ntry:\n h.echo(value=2)\nexcept h.HostToolError:\n print("capped")',
            {"cap": 1},
        ),
        (
            "response-cap",
            'import maf_host_tools as h\ntry:\n h.call("payload", size=9000)\nexcept h.HostToolError:\n print("capped")\nprint(h.echo(value="alive"))',
            {},
        ),
        (
            "callback-timeout",
            'import maf_host_tools as h\ntry:\n h.call("wait")\nexcept h.HostToolError:\n print("timeout")\nprint(h.echo(value="alive"))',
            {},
        ),
        ("stubborn-callback", 'import maf_host_tools as h\nh.call("wait", stubborn=True)', {}),
        ("program-timeout", "while True:\n pass", {"timeout": 0.5}),
        ("program-cancel", "while True:\n pass", {"cancel": True}),
        (
            "request-cap",
            'import maf_host_tools as h\ntry:\n h.echo(value="x"*9000)\nexcept h.HostToolError:\n print("capped")',
            {},
        ),
        (
            "invalid-arguments",
            'import maf_host_tools as h\ntry:\n h.echo(unknown=1)\nexcept h.HostToolError:\n print("refused")',
            {},
        ),
        (
            "escaping",
            'import maf_host_tools as h\ns="é\\"\\\\\\n"*100\nprint(h.echo(value=s)==s)',
            {},
        ),
        (
            "stale-generation",
            "import maf_host_tools as h\nh.echo(value=1)",
            {"reject_generation": True},
        ),
        (
            "handoff-failure",
            "import maf_host_tools as h\nh.echo(value=1)",
            {"fail_before_return": True},
        ),
    ]
    for size in (8000, 16300, 20000, 64000, 256000, 400000):
        cases.append(
            (
                f"native-response-{size}",
                'print(len(call_tool("maf_dispatch", payload=\'{"name":"echo","arguments":{"value":0}}\')))',
                {"raw_response": "x" * size},
            )
        )
    for size in (8000, 12000, 16000, 16100, 16200, 16300, 20000, 64000, 256000, 400000):
        cases.append(
            (
                f"native-request-{size}",
                'import maf_host_tools as h\ntry:\n h.echo(value="x"*'
                + str(size)
                + ')\nexcept h.HostToolError:\n print("response-capped")\nelse:\n print("accepted")',
                {"request_limit": WIRE_LIMIT},
            )
        )
    cases.append(
        (
            "native-refusal-64000",
            "import maf_host_tools as h\ntry:\n h.echo(value=0)\nexcept h.HostToolError as error:\n print(len(str(error)))",
            {"raw_response": json.dumps({"refusal": "x" * 64000})},
        )
    )
    return cases


async def run_probes(selected: list[str], docker: bool) -> list[dict[str, Any]]:
    """Collect each bounded result; availability failures remain failed evidence."""
    reports = []
    for name, code, options in case_specs():
        if selected and name not in selected:
            continue
        report = await probe_case(name, code, **options)
        reports.append(report)
        print(json.dumps(report, ensure_ascii=True), flush=True)
    if docker:
        reports.append(await docker_comparison())
    return reports


def validate_reports(reports: list[dict[str, Any]]) -> list[str]:
    """Keep missing prerequisites and unexpected failures from becoming passing evidence."""
    failures = []
    expected_stdout = {
        "shared-api": "{'nested': [1, True, None, 'é']}\nrefused\n",
        "docker-shared-api": "{'nested': [1, True, None, 'é']}\nrefused\n",
        "call-cap": "1\ncapped\n",
        "response-cap": "capped\nalive\n",
        "callback-timeout": "timeout\nalive\n",
        "request-cap": "capped\n",
        "invalid-arguments": "refused\n",
        "escaping": "True\n",
        "native-refusal-64000": "64000\n",
    }
    for report in reports:
        name = report["case"]
        result = report.get("result", {})
        events = report.get("events", [])
        valid = report.get("reaped") is True
        if name in {"stubborn-callback", "program-timeout"}:
            valid &= report.get("error") == "TimeoutError" and report.get("initialized") is True
            if name == "stubborn-callback":
                valid &= "cleanup budget" in report.get("error_detail", "")
        elif name == "program-cancel":
            valid &= report.get("cancelled") is True and report.get("initialized") is True
        elif name == "stale-generation":
            valid &= report.get("error") == "ValueError" and not events
            valid &= report.get("initialized") is True
        elif name == "handoff-failure":
            valid &= result.get("exit_code") == 1
            valid &= "synthetic failure before native marshalling" in result.get("stderr", "")
            valid &= any(e.get("outcome") == "delivery_uncertain" for e in events)
            valid &= not any(e.get("outcome") == "delivered" for e in events)
            valid &= any(e.get("stage") == "worker_prepared" for e in events)
        elif name.startswith("native-request-") and report.get("error") == "EOFError":
            valid &= report.get("initialized") is True and not events
            valid &= int(name.removeprefix("native-request-")) >= FIRST_EOF_REQUEST_VALUE_BYTES
        else:
            valid &= "error" not in report and result.get("exit_code") == 0
            expected = expected_stdout.get(name)
            if name.startswith("native-response-"):
                expected = name.removeprefix("native-response-") + "\n"
            elif name.startswith("native-request-"):
                size = int(name.removeprefix("native-request-"))
                expected = "accepted\n" if size <= 8000 else "response-capped\n"
                valid &= any(
                    e.get("utf8", 0) >= size for e in events if e["stage"] == "request_bytes"
                )
            if expected is not None:
                valid &= result.get("stdout") == expected
            if name == "shared-api":
                valid &= report.get("reuse", {}).get("stdout") == expected
                valid &= report.get("reuse", {}).get("exit_code") == 0
                valid &= report.get("unbound", {}).get("exit_code") == 1
                valid &= "no live run" in report.get("unbound", {}).get("stderr", "")
                valid &= report.get("late_registration", {}).get("error") == "RuntimeError"
                valid &= any(
                    e.get("context") == name + "-next" for e in report.get("reuse_events", [])
                )
            elif name == "callback-timeout":
                valid &= any(
                    e.get("stage") == "timeout_refusal" and e.get("stopped") for e in events
                )
            elif name == "request-cap":
                valid &= not any(e["stage"] == "policy_enter" for e in events)
            elif name == "profile":
                valid &= "json:yes\n" in result.get("stdout", "")
        if not valid:
            failures.append(name)
    return failures


def main() -> None:
    """Require explicit opt-in and save a machine-readable research report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--case", action="append", choices=[name for name, _, _ in case_specs()], default=[]
    )
    parser.add_argument(
        "--docker", action="store_true", help="compare the shared API on local Docker"
    )
    args = parser.parse_args()
    if args.worker:
        worker_main()
        return
    if not args.live or args.output is None:
        parser.error("use --live --output PATH to run native guests")
    if args.output.exists():
        parser.error("output already exists; choose a fresh evidence file")
    versions = {name: version(name) for name in PACKAGES}
    if set(versions.values()) != {"0.7.0"}:
        parser.error("the prototype requires the exactly matched 0.7.0 trio")
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "versions": versions,
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "probe_sha256_lf_utf8": hashlib.sha256(
            Path(__file__).read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest(),
        "cases": asyncio.run(run_probes(args.case, args.docker)),
        "production_capability": False,
    }
    report["unexpected_results"] = validate_reports(report["cases"])
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    if report["unexpected_results"]:
        raise SystemExit("unexpected probe results: " + ", ".join(report["unexpected_results"]))


if __name__ == "__main__":
    main()
