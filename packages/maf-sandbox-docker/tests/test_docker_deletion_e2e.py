"""Opt-in adversarial qualification of Docker's deletion refusal."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import pytest
from maf_sandbox import Capability, IsolationScope, SandboxKey, SandboxSpec

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_CAPABILITY_IMAGE", "")
_WORK = "/audit-deletion/work"
_OUTSIDE = "/audit-deletion-outside"
pytestmark = pytest.mark.skipif(
    not _IMAGE or not shutil.which("docker"), reason="needs the capability qualification image"
)


@pytest.mark.parametrize("attack", ["parent-swap", "command-replacement"])
@pytest.mark.parametrize("operation", ["remove", "reclaim"])
def test_refusal_preserves_selected_and_outside_files(attack, operation):
    async def scenario():
        backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        key = SandboxKey("deletion-" + uuid.uuid4().hex, "thread", "agent", call_id="audit")
        spec = SandboxSpec(
            kind="deletion-refusal",
            image=_IMAGE,
            work_dir=_WORK,
            isolation_scope=IsolationScope.CALL,
            requires=frozenset({Capability.EXEC}),
        )
        container = None
        try:
            sandbox = await backend.acquire(key, spec)
            container = sandbox.instance_id

            async def guest(code):
                result = await sandbox.exec(
                    ["python3", "-c", code], working_directory="/", timeout=15
                )
                assert result.exit_code == 0, result.stderr
                return result.stdout

            identity = json.loads(
                await guest(
                    "import os,json; print(json.dumps({'uid':os.getuid(),"
                    "'status':dict(line.split(':',1) for line in open('/proc/self/status'))}))"
                )
            )
            assert identity["uid"] == 0
            for field in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb"):
                assert int(identity["status"][field], 16) == 0
            assert int(identity["status"]["NoNewPrivs"]) == 1
            await guest(
                "from pathlib import Path; "
                f"w=Path('{_WORK}'); (w/'victim').mkdir(); "
                "(w/'victim'/'inside').write_text('inside'); "
                f"o=Path('{_OUTSIDE}'); (o/'victim').mkdir(parents=True); "
                "(o/'victim'/'sentinel').write_text('outside')"
            )
            if attack == "parent-swap":
                await guest(
                    f"import os; os.chmod('{_WORK}',0o700); "
                    f"os.rename('{_WORK}','{_WORK}-original'); "
                    f"os.symlink('{_OUTSIDE}','{_WORK}')"
                )
                original_target = _WORK + "-original/victim/inside"
            else:
                await guest(
                    "import shutil; from pathlib import Path; "
                    "p=Path(shutil.which('rm')); p.unlink(); "
                    "p.write_text(chr(10).join(['#!/bin/sh',"
                    f"'/bin/unlink {_OUTSIDE}/victim/sentinel','exit 0',''])); p.chmod(0o755)"
                )
                original_target = _WORK + "/victim/inside"

            calls = []
            original_run = sandbox._run

            async def record(*args, **kwargs):
                calls.append(args)
                return await original_run(*args, **kwargs)

            sandbox._run = record
            if operation == "remove":
                with pytest.raises(NotImplementedError, match="FILES_DELETE"):
                    await sandbox.remove("victim", working_directory=_WORK, recursive=True)
            else:
                with pytest.raises(NotImplementedError, match="RECLAIM"):
                    await sandbox.reclaim(_WORK + "/victim", working_directory=_WORK, timeout=0)
            assert not calls
            preserved = json.loads(
                await guest(
                    "import json; from pathlib import Path; print(json.dumps(["
                    f"Path('{original_target}').read_text(), "
                    f"Path('{_OUTSIDE}/victim/sentinel').read_text()]))"
                )
            )
            assert preserved == ["inside", "outside"]
        finally:
            assert await backend.dispose(key, kind=spec.kind) is None
        if container:
            remaining = subprocess.run(
                ["docker", "ps", "-a", "--no-trunc", "--filter", f"id={container}", "-q"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert remaining.returncode == 0, remaining.stderr
            assert not remaining.stdout.strip()

    asyncio.run(scenario())
