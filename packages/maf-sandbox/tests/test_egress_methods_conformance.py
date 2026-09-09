"""Method probes must distinguish confinement from a broken request or a rejecting origin."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field

import pytest

from maf_sandbox import Capability, ExecResult
from maf_sandbox.conformance import (
    ConformanceFailure,
    ExecEgressMethodsSubject,
    PosixGuestSubject,
    assert_egress_conformance,
    assert_egress_methods_conformance,
    run_egress_methods_probes,
)
from maf_sandbox.testing import InProcessSandbox

URL = "https://api.example.com/probe"
CAPABILITIES = frozenset({Capability.EGRESS_METHODS})


@dataclass
class Subject:
    replies: dict[str, bool | Exception]
    capabilities: frozenset[Capability] = CAPABILITIES
    calls: list[tuple[str, str, float]] = field(default_factory=list)

    async def http_reaches(self, method: str, url: str, *, timeout: float) -> bool:
        self.calls.append((method, url, timeout))
        reply = self.replies[method]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_native_subject_needs_no_exec_or_file_surface():
    scoped = Subject({"GET": True, "POST": False})
    control = Subject({"POST": True})
    results = asyncio.run(
        assert_egress_methods_conformance(scoped, control, allowed_url=URL, request_timeout=12)
    )
    assert len(results) == 3 and all(result.passed for result in results)
    assert scoped.calls == [("GET", URL, 12), ("POST", URL, 12)]
    assert control.calls == [("POST", URL, 12)]


@pytest.mark.parametrize(
    ("scoped_replies", "control_reply", "failure"),
    [
        ({"GET": True, "POST": True}, True, "a-scoped-post-is-refused"),
        ({"GET": False, "POST": False}, True, "a-scoped-get-is-reachable"),
        ({"GET": True, "POST": False}, False, "an-unscoped-post-is-reachable"),
        ({"GET": True, "POST": RuntimeError("client broke")}, True, "a-scoped-post-is-refused"),
    ],
)
def test_failures_cannot_pass_as_method_enforcement(scoped_replies, control_reply, failure):
    scoped, control = Subject(scoped_replies), Subject({"POST": control_reply})
    with pytest.raises(ConformanceFailure, match=failure):
        asyncio.run(assert_egress_methods_conformance(scoped, control, allowed_url=URL))


@pytest.mark.parametrize("which", ["scoped", "control"])
def test_a_non_declarer_is_refused_before_any_request(which: str):
    scoped, control = Subject({"GET": True, "POST": False}), Subject({"POST": True})
    subject = scoped if which == "scoped" else control
    subject.capabilities = frozenset()
    with pytest.raises(ValueError, match="EGRESS_METHODS"):
        asyncio.run(run_egress_methods_probes(scoped, control, allowed_url=URL))
    assert not scoped.calls and not control.calls


class CurlSandbox(InProcessSandbox):
    def __init__(self, result: ExecResult):
        super().__init__()
        self.result = result
        self.http_commands: list[object] = []

    async def exec(self, command, *, working_directory=None, timeout=None):
        self.http_commands.append(command)
        return self.result


@pytest.mark.parametrize(
    ("status", "exit_code", "reaches"),
    [("200", 0, True), ("204", 0, True), ("403", 0, False), ("000", 7, False), ("200", 28, False)],
)
def test_exec_subject_reads_completed_status_and_quotes_the_method(status, exit_code, reaches):
    sandbox = CurlSandbox(ExecResult(stdout=status, stderr="", exit_code=exit_code))
    subject = ExecEgressMethodsSubject(sandbox, CAPABILITIES)
    assert asyncio.run(subject.http_reaches("get", URL + "?a=1&b=2", timeout=30)) is reaches
    assert sandbox.http_commands == [
        [
            "sh",
            "-c",
            "curl -s -o /dev/null -w '%{http_code}' --max-time 25.000 -X get '" + URL + "?a=1&b=2'",
        ]
    ]


def test_missing_curl_is_a_harness_failure():
    subject = ExecEgressMethodsSubject(
        CurlSandbox(ExecResult(stdout="", stderr="curl: not found", exit_code=127)), CAPABILITIES
    )
    with pytest.raises(RuntimeError, match="curl"):
        asyncio.run(subject.http_reaches("POST", URL, timeout=30))


class TimedCurlSandbox(InProcessSandbox):
    def __init__(self, *, response_delay: float, deny_post: bool = False):
        super().__init__()
        self.response_delay = response_delay
        self.deny_post = deny_post
        self.outer_timeouts: list[float] = []

    async def exec(self, command, *, working_directory=None, timeout=None):
        assert timeout is not None
        self.outer_timeouts.append(timeout)
        args = shlex.split(command[-1])
        inner_timeout = float(args[args.index("--max-time") + 1])
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        blocked = args[-1].endswith("/denied") or (self.deny_post and method == "POST")
        if blocked:
            if inner_timeout >= timeout:
                raise TimeoutError("exec expired before curl could report the denial")
            return ExecResult(stdout="000", exit_code=28)
        if self.response_delay >= min(inner_timeout, timeout):
            return ExecResult(stdout="000", exit_code=28)
        return ExecResult(stdout="200", exit_code=0)


@pytest.mark.parametrize(("timeout", "response_delay"), [(1.0, 0.0), (12.0, 0.0), (60.0, 30.0)])
@pytest.mark.parametrize("methods", [False, True])
def test_configured_timeout_allows_curl_to_report_denials(timeout, response_delay, methods):
    sandbox = TimedCurlSandbox(response_delay=response_delay, deny_post=methods)
    if methods:
        control = TimedCurlSandbox(response_delay=response_delay)
        asyncio.run(
            assert_egress_methods_conformance(
                ExecEgressMethodsSubject(sandbox, CAPABILITIES),
                ExecEgressMethodsSubject(control, CAPABILITIES),
                allowed_url=URL,
                request_timeout=timeout,
            )
        )
        assert control.outer_timeouts == [timeout]
    else:
        asyncio.run(
            assert_egress_conformance(
                PosixGuestSubject(sandbox, "/", frozenset({Capability.EXEC})),
                allowed_url=URL,
                denied_url=URL + "/denied",
                exec_timeout=timeout,
            )
        )
    assert sandbox.outer_timeouts == [timeout, timeout]


@pytest.mark.parametrize("timeout", [0.0, -1.0, 0.5, float("nan"), float("inf")])
def test_curl_timeout_refuses_unsupported_bounds_before_exec(timeout):
    sandbox = CurlSandbox(ExecResult(stdout="200", exit_code=0))
    subject = ExecEgressMethodsSubject(sandbox, CAPABILITIES)
    with pytest.raises(ValueError, match="finite and at least 1 second"):
        asyncio.run(subject.http_reaches("GET", URL, timeout=timeout))
    assert not sandbox.http_commands
