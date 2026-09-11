"""Live tests against a real ``docker`` engine, through `maf-sandbox-docker`.

Skipped unless the ``docker`` client is on ``PATH`` and ``MAF_SANDBOX_DEEPAGENTS_E2E_IMAGE``
names an image with ``sh`` and ``python3`` — Deep Agents' derived file tools run Python in the
guest, and the derived-tools test here proves they do on this adapter. Set
``MAF_SANDBOX_DEEPAGENTS_E2E_BICEP_IMAGE`` to a build of ``images/bicep-sandbox`` as well and
the compiler round trip the sample makes runs too, without a model, beside the proof that
``write_file`` needs the interpreter that image lacks.

`maf-sandbox-docker` is a dev dependency of this workspace, not of the wheel: the adapter
speaks to the router and never imports a backend, and the offline suite proves that.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

import pytest
from maf_sandbox import Isolation, SandboxKey, SandboxRouter

from maf_sandbox_deepagents import MafSandbox, deepagents_spec

pytest.importorskip("maf_sandbox_docker")
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig  # noqa: E402

_IMAGE = os.environ.get("MAF_SANDBOX_DEEPAGENTS_E2E_IMAGE", "")
_BICEP_IMAGE = os.environ.get("MAF_SANDBOX_DEEPAGENTS_E2E_BICEP_IMAGE", "")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or not _IMAGE,
    reason="needs the docker client on PATH and MAF_SANDBOX_DEEPAGENTS_E2E_IMAGE naming an image",
)

_WORK = "/maf-sandbox/work"

#: A storage account missing its required `sku`: `bicep build` reports BCP035 on it.
_FLAWED_BICEP = """\
param storageAccountName string
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: resourceGroup().location
  kind: 'StorageV2'
}
"""


def _key(name: str) -> SandboxKey:
    return SandboxKey(
        scope="deepagents-e2e", thread_id=f"{name}-{uuid.uuid4().hex[:10]}", agent_dir="agent"
    )


def _adapter(image: str, name: str, **kwargs: int) -> MafSandbox:
    router = SandboxRouter(
        [DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER
    )
    return MafSandbox(router, _key(name), deepagents_spec(image), exec_timeout_seconds=60, **kwargs)


def test_execute_and_the_file_round_trip():
    adapter = _adapter(_IMAGE, "round-trip")

    async def scenario():
        try:
            hello = await adapter.aexecute("echo hello; echo oops >&2; exit 3")
            assert hello.exit_code == 3
            assert "hello" in hello.output
            assert "[stderr] oops" in hello.output

            (uploaded,) = await adapter.aupload_files([("in/data.bin", bytes(range(256)))])
            assert uploaded.error is None

            copied = await adapter.aexecute("cp in/data.bin out.bin && wc -c < out.bin")
            assert copied.exit_code == 0
            assert copied.output.strip() == "256"

            downloaded = await adapter.adownload_files(["out.bin", "absent.bin", "in"])
            assert downloaded[0].content == bytes(range(256))
            assert downloaded[1].error == "file_not_found"
            assert downloaded[2].error == "is_directory"

            # The sync surface, from inside a running loop: a fresh loop on a worker thread,
            # against a backend that already served this sandbox on this one.
            assert adapter.execute("cat out.bin | wc -c").output.strip() == "256"
        finally:
            closed = await adapter.aclose()
            assert closed is True

    asyncio.run(scenario())


def test_a_timeout_reports_and_the_next_command_starts_cold():
    adapter = _adapter(_IMAGE, "timeout")

    async def scenario():
        try:
            slow = await adapter.aexecute("sleep 30", timeout=2)
            assert slow.exit_code is None
            assert "2 seconds" in slow.output
            again = await adapter.aexecute("echo back")
            assert again.exit_code == 0
            assert again.output.strip() == "back"
        finally:
            await adapter.aclose()

    asyncio.run(scenario())


def test_paths_outside_the_base_go_through_the_shell_and_deep_agents_large_edits_work():
    """Deep Agents keeps offloaded history under `/conversation_history` and large-edit
    temporaries under `/tmp`; neither is under the base, so both take the shell road."""
    adapter = _adapter(_IMAGE, "outside")
    content = bytes(range(256)) * 800  # 200 KiB, five chunks

    async def scenario():
        try:
            (uploaded,) = await adapter.aupload_files([("/tmp/deepagents-e2e/blob.bin", content)])
            assert uploaded.error is None, uploaded
            counted = await adapter.aexecute("wc -c < /tmp/deepagents-e2e/blob.bin")
            assert counted.output.strip() == str(len(content))
            blob, folder, absent = await adapter.adownload_files(
                ["/tmp/deepagents-e2e/blob.bin", "/tmp/deepagents-e2e", "/tmp/deepagents-e2e/no"]
            )
            assert blob.content == content
            assert folder.error == "is_directory"
            assert absent.error == "file_not_found"

            # Deep Agents' own `edit` over 50 KB of payload uploads temporaries under `/tmp`
            # and replaces server-side; this is the derived tool the shell road exists for.
            big = f"{_WORK}/big.txt"
            written = await adapter.awrite(big, "a" * 60_000)
            assert written.error is None, written
            edited = await adapter.aedit(big, "a" * 60_000, "b" * 60_000)
            assert edited.error is None, edited
            (after,) = await adapter.adownload_files([big])
            assert after.content == b"b" * 60_000
        finally:
            closed = await adapter.aclose()
            assert closed is True

    asyncio.run(scenario())


def test_output_past_the_budget_is_dropped_and_the_sandbox_stays_usable():
    adapter = _adapter(_IMAGE, "budget", max_output_bytes=4096)

    async def scenario():
        try:
            flood = await adapter.aexecute("yes", timeout=30)
            assert flood.truncated is True
            assert flood.exit_code is None
            assert "4096 bytes" in flood.output
            again = await adapter.aexecute("echo back")
            assert again.output.strip() == "back"
        finally:
            closed = await adapter.aclose()
            assert closed is True

    asyncio.run(scenario())


def test_deep_agents_derived_file_tools_run_over_execute():
    """`write`, `read` and `ls` are Deep Agents' own, built on `execute` and `upload_files`."""
    adapter = _adapter(_IMAGE, "derived")
    try:
        written = adapter.write(f"{_WORK}/notes/todo.txt", "one\ntwo\n")
        assert written.error is None, written
        read = adapter.read(f"{_WORK}/notes/todo.txt")
        assert read.error is None, read
        assert read.file_data is not None
        assert "two" in read.file_data["content"]
        listed = adapter.ls(f"{_WORK}/notes")
        assert listed.error is None, listed
        assert listed.entries is not None
        assert any(entry["path"].endswith("todo.txt") for entry in listed.entries)
    finally:
        closed = adapter.close()
        assert closed is True


@pytest.mark.skipif(not _BICEP_IMAGE, reason="needs MAF_SANDBOX_DEEPAGENTS_E2E_BICEP_IMAGE")
def test_the_compiler_answers_through_execute_on_the_bicep_image():
    """What the sample asks a model to do, done here without one."""
    adapter = _adapter(_BICEP_IMAGE, "bicep")

    async def scenario():
        try:
            (uploaded,) = await adapter.aupload_files([("main.bicep", _FLAWED_BICEP.encode())])
            assert uploaded.error is None
            built = await adapter.aexecute("bicep build main.bicep")
            assert "BCP035" in built.output, built
            # Deep Agents' `write_file` runs a Python preflight through `execute` before it
            # uploads, so on this image it fails where the adapter's own upload above did not.
            written = await adapter.awrite(f"{_WORK}/other.bicep", "param p string\n")
            assert written.error is not None and "python3" in written.error, written
        finally:
            closed = await adapter.aclose()
            assert closed is True

    asyncio.run(scenario())
