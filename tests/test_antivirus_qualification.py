"""Qualification must refresh old seeds without weakening its refusal probes."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from maf_sandbox import ScopePurge, ToolCallEnded
from maf_sandbox_antivirus import DatabaseIdentity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import antivirus_qualification as qualification  # noqa: E402


@pytest.mark.parametrize("provider", ["docker", "acas"])
@pytest.mark.parametrize("mirror", [False, True])
def test_qualification_refreshes_old_seed_in_every_freshness_dependent_case(
    monkeypatch, tmp_path, provider, mirror
):
    seed = tuple(DatabaseIdentity(name, 1, "a" * 64) for name in ("main", "daily", "bytecode"))
    bytecode = b"seed bytecode"
    if mirror:
        for name in ("main", "daily", "bytecode"):
            (tmp_path / f"{name}.cvd").write_bytes(
                b"ClamAV-VDB:date:2:1:90:hash:signature:builder:123"
            )
        monkeypatch.setenv("MAF_ANTIVIRUS_UPDATE_DIRECTORY", str(tmp_path))
    else:
        monkeypatch.delenv("MAF_ANTIVIRUS_UPDATE_DIRECTORY", raising=False)
    for field in ("ENDPOINT", "SUBSCRIPTION_ID", "RESOURCE_GROUP", "GROUP"):
        monkeypatch.setenv(f"ACAS_SANDBOX_{field}", "fixture")
    calls = []
    native_commands = []

    class Sandbox:
        instance_id = "fixture-instance"

        async def write_file(self, *args, **kwargs):
            pass

        async def exec(self, command, **kwargs):
            native_commands.append(command)
            return SimpleNamespace(
                exit_code=0,
                stdout=json.dumps(
                    [
                        f"{name}:{case}"
                        for name in ("main", "daily", "bytecode")
                        for case in ("valid-chain", "tampered-signature", "wrong-version")
                    ]
                ),
                stderr="",
            )

    class Router:
        def __init__(self, *args, observer, **kwargs):
            self.observer = observer
            self.instances = 0

        async def acquire(self, *args, **kwargs):
            self.instances += 1
            sandbox = Sandbox()
            sandbox.instance_id = str(self.instances)
            return sandbox

        async def dispose(self, *args):
            pass

        async def dispose_scope(self, *args):
            return ScopePurge()

    async def backend(*args, **kwargs):
        return SimpleNamespace()

    import maf_sandbox_acas
    import maf_sandbox_docker

    monkeypatch.setattr(maf_sandbox_docker.DockerSandboxBackend, "create", backend)
    monkeypatch.setattr(maf_sandbox_acas, "AcasSandboxBackend", lambda *a: SimpleNamespace())
    monkeypatch.setattr(qualification, "SandboxRouter", Router)
    monkeypatch.setattr(qualification.subprocess, "check_output", lambda *a, **kw: bytecode)
    inspections = []

    def docker(*args):
        if args[:2] == ("image", "inspect"):
            return "sha256:fixture"
        if args[0] == "inspect":
            inspections.append(args)
            return json.dumps(
                [
                    {
                        "Image": "sha256:fixture",
                        "Id": str(len(inspections)),
                        "HostConfig": {"NetworkMode": "none"},
                    }
                ]
            )
        return "container" if len(inspections) < len(calls) + 1 else ""

    monkeypatch.setattr(qualification, "_docker", docker)

    def tools(router, store, agent, context, **options):
        async def scan(**kwargs):
            index = len(calls)
            label = (
                (
                    "clean",
                    "eicar",
                    "expanded-limit",
                    "stale",
                    "host-replacement",
                    "corrupt-replacement",
                    "host-update",
                )[index]
                if provider == "docker"
                else ("clean", "eicar", "expanded-limit", "stale", "host-update")[index]
            )
            calls.append(label)
            update_provider = options.get("update_provider")
            needs_freshness = label not in ("stale", "corrupt-replacement")
            if mirror and needs_freshness:
                assert update_provider is not None, f"{label} used stale seed"
                bundle = await update_provider(seed)
                assert dict(bundle.targets) == dict(main=2, daily=2, bytecode=2)
                assert {file.name for file in bundle.files} == {
                    "main.cvd",
                    "daily.cvd",
                    "bytecode.cvd",
                }
                if label == "host-replacement":
                    assert (
                        next(file.data for file in bundle.files if file.name == "bytecode.cvd")
                        == (tmp_path / "bytecode.cvd").read_bytes()
                    )
            elif label in ("stale", "clean", "eicar", "expanded-limit"):
                assert update_provider is None
            if label == "corrupt-replacement":
                assert update_provider is not None
                bundle = await update_provider(seed)
                assert dict(bundle.targets)["daily"] == 1
                assert bundle.files[0].data != bytecode
            await router.acquire(None, None)
            router.observer.tool_call_ended(
                ToolCallEnded(
                    tool="scan_file",
                    kind="antivirus",
                    keys=(),
                    seconds=1,
                    failure=None,
                    unclean=0,
                    call=str(index),
                )
            )
            verdict = {"clean": "clean", "expanded-limit": "refused", "stale": "refused"}.get(
                label, "infected"
            )
            texts = [
                qualification.COMPLETED_TEXT,
                f"Result: {verdict}",
                "Input SHA-256: " + hashlib.sha256(store.content).hexdigest(),
            ]
            if label == "corrupt-replacement":
                texts = ["error"]
            return [SimpleNamespace(text=text) for text in texts]

        return [SimpleNamespace(func=scan)]

    monkeypatch.setattr(qualification, "make_antivirus_tools", tools)
    result = asyncio.run(qualification.check("sha256:fixture", provider=provider))
    assert len(result["checks"]) == (6 if provider == "docker" else 4) + int(mirror)
    assert len(native_commands) == 1
    assert len(result["signedCdiffChecks"]) == 9


@pytest.mark.parametrize("exit_code,stdout", [(1, "[]"), (0, "[]")])
def test_native_failure_or_incomplete_evidence_blocks_qualification(monkeypatch, exit_code, stdout):
    disposed = []

    class Sandbox:
        async def write_file(self, *args, **kwargs):
            pass

        async def exec(self, *args, **kwargs):
            return SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr="native failure")

    class Router:
        async def acquire(self, *args):
            return Sandbox()

        async def dispose(self, key):
            disposed.append(key)

    with pytest.raises(ValueError, match="signed CDIFF"):
        asyncio.run(
            qualification._signed_cdiff_checks(
                cast(qualification.SandboxRouter, Router()), "test", "sha256:fixture"
            )
        )
    assert len(disposed) == 1
