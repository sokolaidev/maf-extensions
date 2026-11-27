"""Fixed file-channel programs for qualification on two independent execution hosts."""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from maf_sandbox import (
    Capability,
    Cleanup,
    ProgramRequirements,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    TransferLimits,
)
from maf_sandbox_mxc import MxcCoordinatorBinding, MxcSandboxBackend, MxcSandboxConfig
from maf_sandbox_mxc.coordinator_binding import RemoteCatalog
from maf_sandbox_mxc.files.request import Request
from maf_sandbox_mxc.remote_api import Client
from maf_sandbox_mxc.remote_authority import Authority
from maf_sandbox_mxc.shared_store import ScratchLimits

KEY = SandboxKey("qualification", "handoff", "agent")
PROGRAM = ProgramRequirements(max_program_bytes=65536)
SPEC = SandboxSpec(
    kind="file-router",
    work_dir=None,
    requires=frozenset({Capability.FILES_OUT}),
    files_out=TransferLimits(8 << 20, 8 << 20, 1),
    program=PROGRAM,
)
SIZE = 7 << 20
SEED = f"counter = 41\nwith open('proof.bin','wb') as f:\n f.write(b'x' * {SIZE})\nprint(counter)"
NEXT = "counter += 1\nprint(counter)"


@contextmanager
def phase(name: str):
    """Report fixed qualification stages without recording transport or guest data."""
    start = time.monotonic()
    print(f"MXC next: {name} started", flush=True)
    try:
        yield
    except Exception as error:
        cause = error.__cause__
        detail = f" from {type(cause).__name__}" if cause is not None else ""
        print(
            f"MXC next: {name} failed after {time.monotonic() - start:.2f}s "
            f"({type(error).__name__}{detail})",
            flush=True,
        )
        raise
    print(f"MXC next: {name} completed after {time.monotonic() - start:.2f}s", flush=True)


class NextCallClient(Client):
    """Observe the replacement worker's real coordinator calls in the qualification job."""

    def call(self, operation, session, **arguments):
        if operation == "status":
            with phase("status"):
                return super().call(operation, session, **arguments)
        return super().call(operation, session, **arguments)

    def call_result(self, *args, **kwargs):
        with phase("admission"):
            return super().call_result(*args, **kwargs)

    def restore(self, *args, **kwargs):
        with phase("restore"):
            return super().restore(*args, **kwargs)

    def publish(self, *args, **kwargs):
        with phase("publication"):
            return super().publish(*args, **kwargs)


def config(root: Path, bundle: Path, profile: dict, limits, scratch) -> MxcSandboxConfig:
    """Bind producer-verified file assets to the worker's generation root."""
    return MxcSandboxConfig(
        bundle / "helper",
        bundle / "startup",
        root,
        "cross-runner-files",
        profile["helper"],
        profile["startup_tree"],
        limits,
        ScratchLimits(scratch.bytes, scratch.entries, scratch.total_bytes),
        persistent=True,
    )


async def exercise(worker, operation: str, host) -> dict:
    """Run a fixed program and collect its file through the admitted router channel."""
    cfg = config(
        host.root,
        worker.bundle,
        worker.peers[worker.role]["profile"],
        host.config.limits,
        host.scratch,
    )
    client = worker.transport
    if operation == "next":
        client = NextCallClient(client.address, client.context, client.hostname)
    backend = MxcSandboxBackend(
        cfg,
        coordinator=MxcCoordinatorBinding(
            client, host, KEY, SPEC, worker.peers[worker.role]["profile"]
        ),
    )
    router = SandboxRouter([backend], min_cleanup=Cleanup.RECLAIM)
    try:
        admission = await router.enter_call(KEY, SPEC, owner=operation)
        try:
            sandbox = await router.acquire(KEY, SPEC, _admission=admission)
            if admission.channel is None:
                raise RuntimeError("file router has no program channel")
            call_id = "next" if operation == "next" else "seed"
            result = await admission.channel.run(
                sandbox,
                NEXT if call_id == "next" else SEED,
                requirements=PROGRAM,
                guest_call_path=call_id,
                timeout=90,
            )
            if result.exit_code or result.stderr_bytes:
                raise RuntimeError("file program failed")
            evidence = {
                "stdout": result.stdout_bytes.decode(),
                "fresh": getattr(sandbox, "freshly_created", None),
            }
            if call_id == "seed":
                data = await sandbox.read_file(
                    "proof.bin", working_directory="/workspace/calls/seed", max_bytes=SIZE
                )
                if data != b"x" * SIZE:
                    raise RuntimeError("replayed file differs")
                evidence.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            return evidence
        finally:
            await router.release_call(KEY, SPEC.kind, owner=operation)
    finally:
        await backend.aclose()


def stale_publish(worker, host) -> None:
    """Submit A's original call identity after its generation has been fenced."""
    cfg = config(
        host.root,
        worker.bundle,
        worker.peers[worker.role]["profile"],
        host.config.limits,
        host.scratch,
    )
    request = Request(SEED.encode(), (), (), cfg.files, cfg.workspace, "seed")
    with TemporaryDirectory(dir=worker.root) as temporary:
        candidate = Path(temporary)
        (candidate / "index.json").write_bytes(b"stale")
        worker.transport.publish(
            host.session,
            Authority._encode(host.owner),
            "seed",
            RemoteCatalog._request(request),
            candidate,
            b"stale",
            result_bytes=host.config.limits.result_bytes,
        )
    raise RuntimeError("fenced worker published a checkpoint")
