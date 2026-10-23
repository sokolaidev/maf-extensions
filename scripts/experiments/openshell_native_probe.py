"""Measure official OpenShell on native Linux; findings never become capability declarations."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from maf_sandbox import Capability, EntryKind, SandboxKey, SandboxTransferCapExceeded
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_storage_base_conformance,
    run_call_scope_probes,
    run_exec_probes,
    run_files_delete_probes,
    run_files_in_probes,
    run_files_out_probes,
    run_reach_probes,
)
from openshell_allocation_probe import Client, dispose_fixture, verify_workspace_absent
from openshell_conformance import Candidate
from openshell_files_probe import signed_gateway
from openshell_owned_exec import OutputLimitError
from openshell_owned_probe import docker_get, snapshot
from openshell_replay_probe import wait_gateway

PIN = "6648bd0c290efbc41ba131ee9831ee45cd431f94"
MEASURED = frozenset(
    {
        Capability.EXEC,
        Capability.FILES_IN,
        Capability.FILES_OUT,
        Capability.FILES_LIST,
        Capability.FILES_DELETE,
    }
)


def native_host(system: str, kernel: str, engine: dict) -> None:
    """Refuse Desktop/WSL evidence and non-Linux Docker engines before creating resources."""
    topology = " ".join(
        str(x) for x in (kernel, engine.get("KernelVersion"), engine.get("OperatingSystem"))
    ).lower()
    if (
        system != "Linux"
        or engine.get("OSType") != "linux"
        or any(marker in topology for marker in ("microsoft", "wsl", "linuxkit", "docker desktop"))
    ):
        raise RuntimeError("native Linux Docker required; Desktop/WSL is not qualification")


class Subject(PosixGuestSubject):
    """Plant hostile paths through guest exec, independently of the measured SFTP plane."""

    async def plant_file(self, path: str, content: bytes) -> None:
        code = (
            "import base64,pathlib; p=pathlib.Path("
            + repr(path)
            + "); p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(base64.b64decode("
            + repr(base64.b64encode(content).decode())
            + "))"
        )
        result = await self.sandbox.exec(
            ["python", "-c", code], working_directory="/sandbox", timeout=30
        )
        if result.exit_code:
            raise RuntimeError(
                "guest could not plant the independent file control: " + result.stderr
            )


class Fixture:
    """Own fresh never-reused names in a disposable, sole-controller workspace."""

    def __init__(self, client: Client, report: dict, report_path: Path):
        self.client, self.report, self.report_path = client, report, report_path
        self.owners: list[dict] = []
        self.disposed: set[str] = set()
        self.by_key: dict[SandboxKey, Candidate] = {}

    def save(self) -> None:
        self.report_path.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")

    async def acquire(self, base: str = "/sandbox", *, key: SandboxKey | None = None) -> Candidate:
        if key is None:
            key = SandboxKey("q1833", self.client.scope.workspace, "probe", str(uuid.uuid4()))
        if key in self.by_key:
            raise ValueError("fixture call keys must never be reused")

        def retain(owner):
            owner["key"] = asdict(key)
            self.owners.append(owner)
            self.report["owners"] = self.owners
            self.save()

        name = "s1797-" + uuid.uuid4().hex[:12]
        request = self.client.pb.CreateSandboxRequest.FromString(
            self.client.request(name, str(uuid.uuid4()))
        )
        request.spec.template.labels["spike.issue"] = "1833"
        self.report["policy_protobuf_hex"] = request.spec.policy.SerializeToString().hex()
        owner = await asyncio.to_thread(
            self.client.send, request.SerializeToString(), on_created=retain
        )
        observed = await asyncio.to_thread(snapshot, owner["sandbox_id"])
        if (
            len(observed["containers"]) != 2
            or not observed["processes"]
            or any(v is None for v in observed["processes"].values())
        ):
            raise RuntimeError("independent container/PID observer unavailable")

        async def dispose():
            await self.dispose(owner)

        candidate = Candidate(self.client, owner, dispose, base=base)
        self.by_key[key] = candidate
        if base != "/sandbox":
            result = await candidate.exec(
                ["mkdir", "-p", base], working_directory="/sandbox", timeout=30
            )
            if result.exit_code:
                raise RuntimeError("explicit base preparation failed")
        return candidate

    async def dispose(self, owner: dict) -> None:
        identity = owner["sandbox_id"]
        if identity in self.disposed:
            return
        current = await asyncio.to_thread(snapshot, identity)
        owner["containers"] = sorted(
            set(owner["containers"]) | {c["Id"] for c in current["containers"]}
        )
        owner["processes"].update(current["processes"])
        self.save()
        await asyncio.to_thread(dispose_fixture, self.client, owner)
        self.disposed.add(identity)
        self.report["disposed_ids"] = sorted(self.disposed)
        self.save()

    def subject(self, sandbox: Candidate, path: str) -> Subject:
        return Subject(sandbox, path, MEASURED, exec_cleanup_timeout=30)


def summarize(results) -> list[dict]:
    """Retain every failure and skip; a skipped probe is never a pass."""
    return [
        {"name": r.probe.name, "passed": r.passed, "failure": r.failure, "skipped": r.skipped}
        for r in results
    ]


async def file_races(candidate: Candidate) -> dict:
    """Swap a checked ancestor at an explicit barrier before the real native SFTP open."""
    code = "from pathlib import Path; p=Path('/sandbox/race'); (p/'work'/'parent').mkdir(parents=True); (p/'outside').mkdir(); (p/'outside'/'secret').write_bytes(b'outside-secret'); (p/'work'/'parent'/'secret').write_bytes(b'inside')"
    if (
        await candidate.exec(["python", "-c", code], working_directory="/sandbox", timeout=30)
    ).exit_code:
        raise RuntimeError("race controls failed")
    swaps = 0

    async def swap():
        nonlocal swaps
        swaps += 1
        code = "from pathlib import Path; p=Path('/sandbox/race/work/parent'); p.rename(p.with_name('saved')); p.symlink_to('../outside',target_is_directory=True)"
        if (
            await candidate.exec(["python", "-c", code], working_directory="/sandbox", timeout=30)
        ).exit_code:
            raise RuntimeError("replacement barrier failed")

    control = await candidate.read_file(
        "parent/secret", working_directory="/sandbox/race/work", max_bytes=32
    )
    if control != b"inside":
        raise RuntimeError("native race positive control failed")
    candidate.before_open = swap
    try:
        observed = await candidate.read_file(
            "parent/secret", working_directory="/sandbox/race/work", max_bytes=32
        )
    finally:
        candidate.before_open = None
    if swaps != 1 or observed not in (b"inside", b"outside-secret"):
        raise RuntimeError("race produced no attributable observation")
    restore = "from pathlib import Path; p=Path('/sandbox/race/work/parent'); p.unlink(); p.with_name('saved').rename(p)"
    if (
        await candidate.exec(["python", "-c", restore], working_directory="/sandbox", timeout=30)
    ).exit_code:
        raise RuntimeError("race control restoration failed")
    candidate.before_open = swap
    try:
        await candidate.write_file(
            "parent/secret", b"written-after-swap", working_directory="/sandbox/race/work"
        )
    finally:
        candidate.before_open = None
    readback = await candidate.exec(
        [
            "python",
            "-c",
            "from pathlib import Path; import os; os.write(1,Path('/sandbox/race/outside/secret').read_bytes())",
        ],
        working_directory="/sandbox",
        timeout=30,
    )
    if swaps != 2 or readback.exit_code or readback.stderr_bytes:
        raise RuntimeError("write race has no independent guest readback")
    return {
        "barrier_executed": True,
        "positive_control": True,
        "outside_bytes_returned": observed == b"outside-secret",
        "outside_bytes_overwritten": readback.stdout_bytes == b"written-after-swap",
        "atomic_no_follow": False,
    }


async def file_bounds(candidate: Candidate) -> dict:
    """Measure native transfer caps and refuse nonregular files before opening them."""
    candidate.output_limit = 1024
    await candidate.write_file("bounded", b"x" * 1024, working_directory="/sandbox")
    results = {
        "exact_cap": await candidate.read_file(
            "bounded", working_directory="/sandbox", max_bytes=1024
        )
        == b"x" * 1024
    }
    try:
        await candidate.read_file("bounded", working_directory="/sandbox", max_bytes=1023)
    except SandboxTransferCapExceeded:
        results["oversize_read_refused"] = True
    else:
        results["oversize_read_refused"] = False
    try:
        await candidate.write_file("rejected", b"x" * 1025, working_directory="/sandbox")
    except SandboxTransferCapExceeded:
        results["oversize_write_refused"] = (
            await candidate.stat_file("rejected", working_directory="/sandbox") is None
        )
    else:
        results["oversize_write_refused"] = False
    await candidate.write_file("growing", b"x", working_directory="/sandbox")

    async def grow():
        result = await candidate.exec(
            ["python", "-c", "from pathlib import Path; Path('growing').write_bytes(b'x'*1025)"],
            working_directory="/sandbox",
            timeout=30,
        )
        if result.exit_code:
            raise RuntimeError("file growth barrier failed")

    candidate.before_open = grow
    try:
        await candidate.read_file("growing", working_directory="/sandbox", max_bytes=1024)
    except SandboxTransferCapExceeded:
        results["growth_refused"] = True
    else:
        results["growth_refused"] = False
    finally:
        candidate.before_open = None
    result = await candidate.exec(["mkfifo", "pipe"], working_directory="/sandbox", timeout=30)
    metadata = await candidate.stat_file("pipe", working_directory="/sandbox")
    if result.exit_code or metadata is None or metadata.kind is not EntryKind.OTHER:
        raise RuntimeError("nonregular file control failed")
    try:
        await candidate.read_file("pipe", working_directory="/sandbox", max_bytes=1024)
    except OSError as exc:
        results["nonregular_refused"] = str(exc) == "only regular files may be read"
    else:
        results["nonregular_refused"] = False
    return results


async def execution_cases(fixture: Fixture) -> dict:
    """Measure byte bounds, fractional deadlines, cancellation and cleanup failure recovery."""
    results = {}
    for name in (
        "bytes",
        "natural_124",
        "cap_exact",
        "cap_over",
        "fractional_deadline",
        "cancellation",
        "cleanup_failure",
    ):
        sandbox = await fixture.acquire()
        sandbox.output_limit = 4096
        parent = "import subprocess,time; subprocess.Popen(['python','-c',\"import time; marker='q1833-child'; time.sleep(60)\"]); time.sleep(60)"
        code = {
            "bytes": "import os; os.write(1,b'\\x00\\xff'); os.write(2,b'\\xfe\\x00')",
            "natural_124": "raise SystemExit(124)",
            "cap_exact": "import os; os.write(1,b'a'*2048); os.write(2,b'b'*2048)",
            "cap_over": "import os,time; os.write(1,b'a'*2048); os.write(2,b'b'*2049); time.sleep(60)",
        }.get(name, parent)
        original_dispose = sandbox.dispose
        if name == "cleanup_failure":

            async def fail_cleanup():
                raise RuntimeError("injected cleanup failure")

            sandbox.dispose = fail_cleanup
        began = asyncio.get_running_loop().time()
        task = asyncio.create_task(
            sandbox.exec(
                ["python", "-c", code],
                working_directory="/sandbox",
                timeout=0.5 if name in ("fractional_deadline", "cleanup_failure") else 20,
            )
        )
        descendants = []
        if name in ("fractional_deadline", "cancellation", "cleanup_failure"):
            while not task.done() and asyncio.get_running_loop().time() - began < 2:
                observed = await asyncio.to_thread(snapshot, sandbox.instance_id)
                descendants = [row[0] for row in observed["top"] if "q1833-child" in row[-1]]
                if len(descendants) >= 2:
                    sandbox.owner["processes"].update(observed["processes"])
                    break
                await asyncio.sleep(0.01)
            if name == "cancellation":
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0.01)
        try:
            result = await task
            passed = (
                name == "bytes"
                and result.stdout_bytes == b"\x00\xff"
                and result.stderr_bytes == b"\xfe\x00"
                and result.exit_code == 0
                or name == "natural_124"
                and result.exit_code == 124
                or name == "cap_exact"
                and result.stdout_bytes == b"a" * 2048
                and result.stderr_bytes == b"b" * 2048
                and result.exit_code == 0
            )
            outcome = "exit"
        except (Exception, asyncio.CancelledError) as exc:
            outcome = type(exc).__name__
            passed = (
                name == "cap_over"
                and isinstance(exc, OutputLimitError)
                or name == "fractional_deadline"
                and isinstance(exc, TimeoutError)
                or name == "cancellation"
                and isinstance(exc, asyncio.CancelledError)
                or name == "cleanup_failure"
                and str(exc) == "injected cleanup failure"
            )
        elapsed = asyncio.get_running_loop().time() - began
        if name == "fractional_deadline":
            passed = passed and 0.25 <= elapsed < 35
        if name in ("fractional_deadline", "cancellation", "cleanup_failure"):
            passed = passed and len(descendants) >= 2
        retained = name == "cleanup_failure" and sandbox.instance_id not in fixture.disposed
        if retained:
            retained = bool((await asyncio.to_thread(snapshot, sandbox.instance_id))["containers"])
        sandbox.dispose = original_dispose
        await sandbox.dispose()
        results[name] = {
            "passed": bool(passed and (name != "cleanup_failure" or retained)),
            "outcome": outcome,
            "seconds": elapsed,
            "retained_after_cleanup_failure": retained,
            "observed_parent_child_pids": descendants,
            "physical_cleanup_verified": sandbox.instance_id in fixture.disposed,
        }
        fixture.report["execution"] = results
        fixture.save()
        if not results[name]["passed"]:
            raise RuntimeError("unexpected execution observation: " + name)
    return results


async def measure(fixture: Fixture) -> None:
    """Run shared suites on fresh instances, retaining failures separately from harness errors."""
    suites = fixture.report.setdefault("shared_probes", {})
    for label, run in (
        ("FILES_OUT", run_files_out_probes),
        ("FILES_IN", run_files_in_probes),
        ("FILES_DELETE", run_files_delete_probes),
        ("reach", run_reach_probes),
        ("EXEC", run_exec_probes),
    ):
        sandbox = await fixture.acquire()
        try:
            suites[label] = summarize(await run(fixture.subject(sandbox, "/sandbox/work")))
            fixture.save()
        finally:
            await sandbox.dispose()
    for base in ("/sandbox", "/sandbox/explicit"):
        sandbox = await fixture.acquire(base)
        try:
            await assert_storage_base_conformance(sandbox, MEASURED)
            suites["storage_base:" + base] = [
                {"name": "relative-base", "passed": True, "failure": None, "skipped": None}
            ]
        except AssertionError as exc:
            suites["storage_base:" + base] = [
                {"name": "relative-base", "passed": False, "failure": str(exc), "skipped": None}
            ]
        finally:
            await sandbox.dispose()
        fixture.save()
    first = await fixture.acquire()
    other: list[Candidate] = []

    async def acquire_another():
        second = await fixture.acquire()
        other.append(second)
        return fixture.subject(second, "/sandbox/work")

    async def dispose_other():
        for sandbox in other:
            await sandbox.dispose()

    try:
        suites["call_scope"] = summarize(
            await run_call_scope_probes(
                fixture.subject(first, "/sandbox/work"),
                acquire_another,
                first.dispose,
                dispose_other,
            )
        )
    finally:
        await first.dispose()
        await dispose_other()
    sandbox = await fixture.acquire()
    try:
        fixture.report["replacement_race"] = await file_races(sandbox)
    finally:
        await sandbox.dispose()
    fixture.save()
    sandbox = await fixture.acquire()
    try:
        fixture.report["file_bounds"] = await file_bounds(sandbox)
        fixture.save()
        if not all(fixture.report["file_bounds"].values()):
            raise RuntimeError("unexpected native transfer-bound observation")
    finally:
        await sandbox.dispose()
    await execution_cases(fixture)


def main() -> int:
    """Persist findings and exact provenance; refuse unsupported execution environments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    engine = docker_get("/info")
    if not isinstance(engine, dict):
        raise RuntimeError("Docker observer did not return engine metadata")
    native_host(platform.system(), platform.release(), engine)
    if importlib.metadata.version("openshell") != "0.1.2":
        raise RuntimeError("requires official SDK 0.1.2")
    image = docker_get("/images/" + args.image + "/json")
    if image is None or image["Id"] != args.image:
        raise ValueError("workload must be an existing immutable local image ID")
    if image["Config"].get("WorkingDir") != "/sandbox" or image["Config"].get("User") != "sandbox":
        raise ValueError("requires the unprivileged pinned /sandbox workload recipe")
    report = {
        "format": 1,
        "upstream_commit": PIN,
        "gateway_version": "0.1.2",
        "python": sys.version,
        "kernel": platform.release(),
        "docker": {
            key: engine.get(key)
            for key in (
                "ServerVersion",
                "KernelVersion",
                "OperatingSystem",
                "OSType",
                "Architecture",
                "SecurityOptions",
            )
        },
        "sdk": {
            name: importlib.metadata.version(name) for name in ("openshell", "grpcio", "paramiko")
        },
        "image": args.image,
        "image_config": image["Config"],
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "suite_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "declared_capabilities": [],
        "capability_assessment": {
            "EXEC": "blocked for adoption by lifecycle dependencies; shared measurements below",
            "FILES_IN": "unsupported for concurrent mutation: precheck/open is not atomic",
            "FILES_OUT": "unsupported for concurrent mutation: precheck/open is not atomic",
            "FILES_LIST": "unsupported for concurrent mutation: precheck/enumeration is not atomic",
            "FILES_DELETE": "unsupported for concurrent mutation: precheck/removal is not atomic",
            "SNAPSHOT": "unsupported",
            "HOST_TOOLS": "unsupported",
            "RUN_CODE": "unsupported",
        },
        "lifecycle": "blocked by #1817 and #1818; exclusive fixture only",
        "completed": False,
        "physical_cleanup_verified": False,
        "workspace_cleanup_verified": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    control = {}
    with signed_gateway(args.config, control=control) as issuer:
        workspace = "q1833-" + uuid.uuid4().hex[:12]
        client = Client(
            workspace, issuer.token("admin", role="spike-admin", scopes="openshell:all"), args.image
        )
        fixture = Fixture(client, report, args.report)
        created = False
        try:
            wait_gateway(client)
            client.rpc("CreateWorkspace", client.pb.CreateWorkspaceRequest(name=workspace))
            created = True
            fixture.save()
            asyncio.run(measure(fixture))
            report["completed"] = True
        finally:
            errors = []
            for owner in fixture.owners:
                try:
                    asyncio.run(fixture.dispose(owner))
                except Exception as exc:
                    errors.append(exc)
            report["physical_cleanup_verified"] = (
                not errors and bool(fixture.owners) and len(fixture.disposed) == len(fixture.owners)
            )
            if created and report["physical_cleanup_verified"]:
                try:
                    client.rpc(
                        "DeleteWorkspace",
                        client.pb.DeleteWorkspaceRequest(name=workspace, allow_missing=True),
                    )
                    report["workspace_cleanup_verified"] = verify_workspace_absent(
                        client, workspace
                    )
                except Exception as exc:
                    errors.append(exc)
            fixture.save()
            client.close()
            control["gateway"].stop()
            args.report.with_suffix(".gateway.log").write_bytes(control["log"].read_bytes())
            if errors:
                raise ExceptionGroup("native fixture cleanup incomplete", errors)
    print(json.dumps(report), flush=True)
    return int(
        not (
            report["completed"]
            and report["physical_cleanup_verified"]
            and report["workspace_cleanup_verified"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
