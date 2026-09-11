"""Measure full Bicep calls and cleanup on Docker, at both cleanup rungs a host can be on."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

import pytest
from maf_sandbox import CallerContext, Cleanup, Egress, SandboxKey, SandboxRouter
from maf_sandbox.conformance import assert_nothing_left_behind
from maf_sandbox.testing import InMemoryStore

from maf_sandbox_bicep import bicep_sandbox_spec, make_bicep_tools

pytest.importorskip(
    "maf_sandbox_docker.conformance",
    exc_type=ImportError,
    reason="the live probe needs the sibling Docker wheel and its engine fingerprint subject",
)

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig  # noqa: E402
from maf_sandbox_docker.conformance import DockerFingerprintSubject  # noqa: E402

_IMAGE = os.environ.get("MAF_SANDBOX_BICEP_E2E_IMAGE", "")
_OBSERVER = os.environ.get("MAF_SANDBOX_DOCKER_OBSERVER_IMAGE", "")
_PROXY = os.environ.get("MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not _IMAGE or not _OBSERVER or not shutil.which("docker"),
    reason="needs Docker, MAF_SANDBOX_BICEP_E2E_IMAGE and MAF_SANDBOX_DOCKER_OBSERVER_IMAGE",
)

_MODULE = """module storage 'br/public:avm/res/storage/storage-account:0.31.0' = {
  name: 'storage'
  params: {
    name: 'teststorage'
  }
}
"""


@pytest.mark.parametrize("case", ["local", "diagnostics", "modules", "closed-modules", "cancelled"])
def test_validation_leaves_nothing_behind_and_reuses_the_sandbox(case: str, monkeypatch):
    if case == "modules" and not _PROXY:
        pytest.skip("module restore needs MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE")

    async def scenario():
        egress = Egress.ALLOWLIST if case == "modules" else Egress.CLOSED
        backend = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image=_PROXY))
        router = SandboxRouter(
            [backend], min_isolation=backend.isolation, min_cleanup=Cleanup.RECLAIM
        )
        spec = bicep_sandbox_spec(image=_IMAGE, egress=egress)
        key = SandboxKey(
            scope="bicep-confinement-" + uuid.uuid4().hex, thread_id="test", agent_dir="test"
        )
        source = (
            _MODULE if case in {"modules", "closed-modules"} else "output value string = 'hello'\n"
        )
        if case == "diagnostics":
            source = "output value string = missingValue\n"
        store = InMemoryStore(
            {"nested/main.bicep": source, "main.bicepparam": "using './nested/main.bicep'\n"}
        )
        context = CallerContext(
            current_scope=lambda: key.scope,
            current_thread_id=lambda: key.thread_id,
            list_files=InMemoryStore.list,
        )
        tool = make_bicep_tools(router, store, key.agent_dir, context, image=_IMAGE, egress=egress)[
            0
        ]
        try:
            assert router.effective_cleanup(spec) is Cleanup.RECLAIM
            # Adopt before measuring so the callback and the subject use the same instance.
            sandbox = await router.acquire(key, spec)
            instance = sandbox.instance_id
            started = asyncio.Event()
            if case == "cancelled":
                original_exec = type(sandbox).exec

                async def observed_exec(self, *args, **kwargs):
                    started.set()
                    return await original_exec(self, *args, **kwargs)

                monkeypatch.setattr(type(sandbox), "exec", observed_exec)

            async def call():
                if case == "cancelled":
                    started.clear()
                    pending = asyncio.create_task(
                        tool.func(files=["main.bicepparam", "nested/main.bicep"])
                    )
                    await started.wait()
                    for _ in range(2):
                        pending.cancel()
                        await asyncio.sleep(0)
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    assert (await router.acquire(key, spec)).instance_id == instance
                    return
                result = await tool.func(files=["main.bicepparam", "nested/main.bicep"])
                report = str(result[0].text)
                assert "Error:" not in report, report
                if case == "closed-modules":
                    assert "MODULE RESTORE FAILED" in report, report
                    assert "INCOMPLETE" in report, report
                    assert "BCP190" in report, report
                elif case == "diagnostics":
                    assert "MODULE RESTORE FAILED" not in report, report
                    assert "BCP057" in report, report
                else:
                    assert "MODULE RESTORE FAILED" not in report, report
                    assert "[error]" not in report, report
                    for name in ("main.bicepparam", "nested/main.bicep"):
                        for phase in ("build", "lint"):
                            expected = f"{phase}({name}): "
                            assert expected in report, report
                            if case != "modules":
                                assert expected + "no diagnostics" in report, report
                assert (await router.acquire(key, spec)).instance_id == instance

            for _ in range(2):
                subject = DockerFingerprintSubject(sandbox, observer_image=_OBSERVER)
                results = await assert_nothing_left_behind(subject, call)
                assert all(result.passed and not result.skipped for result in results)
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


async def _workload_containers(scope: str) -> frozenset[str]:
    """Every non-proxy container the daemon still holds for ``scope``, by engine ID.

    The daemon rather than the router: a router that lost an instance it failed to delete
    answers out of its own ledger and passes. The egress proxy carries the same key labels and
    is excluded, its lifetime being the backend's rather than the rung's.
    """
    listed = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        f"label=maf-sandbox.scope={scope}",
        "--format",
        '{{.ID}} {{.Label "maf-sandbox.role"}}',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(listed.communicate(), 60)
    assert listed.returncode == 0, stderr.decode()
    rows = [line.split(maxsplit=1) for line in stdout.decode().splitlines() if line.strip()]
    return frozenset(row[0] for row in rows if len(row) == 1 or row[1].strip() != "proxy")


@pytest.mark.parametrize("case", ["local", "diagnostics", "modules", "cancelled"])
def test_the_disposal_default_deletes_the_sandbox_each_call(case: str, monkeypatch):
    """Measure ``Cleanup.DISPOSE``, the rung a host that names no floor gets, against the engine.

    Each round acquires before it calls, so the instance looked for afterwards is one the
    daemon has already confirmed. ``cancelled`` is the raising call, and the case that carries
    the risk: a cancelled body's cleanup runs under a two-second grace rather than the reclaim
    timeout.
    """
    if case == "modules" and not _PROXY:
        pytest.skip("module restore needs MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE")

    async def scenario():
        egress = Egress.ALLOWLIST if case == "modules" else Egress.CLOSED
        backend = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image=_PROXY))
        # No `min_cleanup`: the floor a deployment gets when it names none.
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        spec = bicep_sandbox_spec(image=_IMAGE, egress=egress)
        key = SandboxKey(
            scope="bicep-disposal-" + uuid.uuid4().hex, thread_id="test", agent_dir="test"
        )
        source = _MODULE if case == "modules" else "output value string = 'hello'\n"
        if case == "diagnostics":
            source = "output value string = missingValue\n"
        store = InMemoryStore(
            {"nested/main.bicep": source, "main.bicepparam": "using './nested/main.bicep'\n"}
        )
        context = CallerContext(
            current_scope=lambda: key.scope,
            current_thread_id=lambda: key.thread_id,
            list_files=InMemoryStore.list,
        )
        tool = make_bicep_tools(router, store, key.agent_dir, context, image=_IMAGE, egress=egress)[
            0
        ]
        files = ["main.bicepparam", "nested/main.bicep"]
        started = asyncio.Event()

        async def call():
            if case == "cancelled":
                started.clear()
                pending = asyncio.create_task(tool.func(files=files))
                await started.wait()
                for _ in range(2):
                    pending.cancel()
                    await asyncio.sleep(0)
                with pytest.raises(asyncio.CancelledError):
                    await pending
                return
            report = str((await tool.func(files=files))[0].text)
            assert "Error:" not in report, report
            assert "MODULE RESTORE FAILED" not in report, report
            if case == "diagnostics":
                assert "BCP057" in report, report
            else:
                assert "[error]" not in report, report

        try:
            assert router.effective_cleanup(spec) is Cleanup.DISPOSE
            assert await _workload_containers(key.scope) == frozenset()
            served: list[str] = []
            for round_number in range(2):
                sandbox = await router.acquire(key, spec)
                instance = sandbox.instance_id
                assert instance not in served, served
                assert await _workload_containers(key.scope) == frozenset({instance})
                # `docker diff` reports a changed directory as well as an added file, so an
                # empty diff covers what the previous call wrote under the work directory.
                measured = await DockerFingerprintSubject(
                    sandbox, observer_image=_OBSERVER
                ).fingerprint()
                assert measured is not None
                assert measured.changed_paths == frozenset(), sorted(measured.changed_paths)
                if case == "cancelled" and round_number == 0:
                    original_exec = type(sandbox).exec

                    async def observed_exec(self, *args, **kwargs):
                        started.set()
                        return await original_exec(self, *args, **kwargs)

                    monkeypatch.setattr(type(sandbox), "exec", observed_exec)
                served.append(instance)
                await call()
                assert await _workload_containers(key.scope) == frozenset()
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())
