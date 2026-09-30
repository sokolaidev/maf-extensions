"""Opt-in full Bicep tool calls against the prepared image with Docker networking closed."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from maf_sandbox import CallerContext, Cleanup, Egress, SandboxRouter
from maf_sandbox.maf import COMPLETED_TEXT, NOT_COMPLETED_TEXT
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_bicep import make_bicep_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

IMAGE = os.environ.get("MAF_BICEP_PREPARED_IMAGE", "")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not IMAGE, reason="needs MAF_BICEP_PREPARED_IMAGE and Docker")
NETWORK = """module network 'br/public:avm/res/network/virtual-network:0.7.2' = {
  name: 'network'
  params: {
    name: 'example-network'
    addressPrefixes: ['10.0.0.0/16']
  }
}
"""
STORAGE = """module storage 'br/public:avm/res/storage/storage-account:0.31.0' = {
  name: 'storage'
  params: {
    name: 'examplestorage'
  }
}
"""


@pytest.mark.parametrize(
    "case", ["network", "storage", "types", "unbaked", "no-config", "host-policy"]
)
def test_prepared_modules_validate_in_closed_reused_sandbox(case, monkeypatch):
    async def scenario():
        source = STORAGE if case == "storage" else NETWORK
        if case == "types":
            source = source.replace("name: 'example-network'", "name: 42\n    unknownInput: true")
        elif case == "unbaked":
            source = source.replace(":0.7.2", ":0.0.0")
        config = json.loads((ROOT / "images/bicep-sandbox/prepared.bicepconfig.json").read_bytes())
        if case == "host-policy":
            config["analyzers"]["core"]["rules"]["no-unused-params"]["level"] = "off"
            source += "param unused string = 'unused'\n"
        store = InMemoryStore(
            {"nested/main.bicep": source, "main.bicepparam": "using './nested/main.bicep'\n"}
        )
        scope = "bicep-prepared-" + uuid.uuid4().hex
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "live",
            list_files=InMemoryStore.list,
        )
        backend = DockerSandboxBackend(DockerSandboxConfig())
        router = SandboxRouter(
            [backend], min_isolation=backend.isolation, min_cleanup=Cleanup.RECLAIM
        )
        acquire = router.acquire
        instances = []

        async def checked_acquire(key, spec, **kwargs):
            sandbox = await acquire(key, spec, **kwargs)
            inspection = subprocess.run(
                ["docker", "inspect", sandbox.instance_id],
                capture_output=True,
                check=True,
                timeout=10,
            )
            assert json.loads(inspection.stdout)[0]["HostConfig"]["NetworkMode"] == "none"
            instances.append(sandbox.instance_id)
            return sandbox

        monkeypatch.setattr(router, "acquire", checked_acquire)
        tool = make_bicep_tools(
            router,
            store,
            "live",
            context,
            image=IMAGE,
            egress=Egress.CLOSED,
            config=None if case == "no-config" else json.dumps(config),
        )[0]
        try:
            for _ in range(2):
                result = await tool.func(files=["main.bicepparam", "nested/main.bicep"])
                text = "\n".join(item.text or "" for item in result)
                assert "Could not download available module versions" not in text
                if case in ("unbaked", "no-config"):
                    assert NOT_COMPLETED_TEXT in text, text
                    assert "MODULE RESTORE FAILED" in text and "BCP190" in text, text
                else:
                    assert COMPLETED_TEXT in text and NOT_COMPLETED_TEXT not in text, text
                    assert "MODULE RESTORE FAILED" not in text, text
                    if case == "types":
                        assert "BCP036" in text and "BCP037" in text, text
                        assert "Result: invalid" in text, text
                    else:
                        assert "Result: valid" in text, text
                        assert "no-unused-params" not in text, text
            assert len(set(instances)) == 1
        finally:
            await router.dispose_scope(scope, "live")

    asyncio.run(scenario())
