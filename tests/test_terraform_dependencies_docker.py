"""Opt-in Docker adapter evidence for prepared provider mirrors and module bundles."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_terraform import make_terraform_tools

IMAGES = {
    engine: os.environ.get(f"MAF_{engine.upper()}_PREPARED_IMAGE", "")
    for engine in ("terraform", "opentofu")
}
ROOTS = {engine: os.environ.get(f"MAF_{engine.upper()}_PREPARED_DIR", "") for engine in IMAGES}
pytestmark = pytest.mark.skipif(
    not all(IMAGES.values()) or not all(ROOTS.values()),
    reason="needs both prepared images and directories",
)


@pytest.fixture(scope="module")
def unrestricted_receiver():
    image = IMAGES["terraform"]
    assert image is not None
    server = (
        "import socketserver\n"
        "class Handler(socketserver.BaseRequestHandler):\n"
        " def handle(self):\n"
        "  self.request.recv(4096)\n"
        "  self.request.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: 8\\r\\nConnection: close\\r\\n\\r\\naccepted')\n"
        "socketserver.ThreadingTCPServer(('0.0.0.0', 8080), Handler).serve_forever()\n"
    )
    created = subprocess.run(
        ["docker", "run", "-d", "--network", "bridge", image, "python3", "-I", "-c", server],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    instance = created.stdout.strip()
    try:
        inspection = subprocess.run(
            ["docker", "inspect", instance], capture_output=True, text=True, check=True, timeout=10
        )
        address = json.loads(inspection.stdout)[0]["NetworkSettings"]["Networks"]["bridge"][
            "IPAddress"
        ]
        probes = []
        for request in [
            b"GET /neighbor?secret=value HTTP/1.1\r\nX-Leak: secret\r\n\r\n",
            b"CONNECT shared.example:443 HTTP/1.1\r\n\r\n",
        ]:
            probe = (
                f"import socket; s=socket.create_connection(({address!r},8080),3); "
                f"s.sendall({request!r}); response=s.makefile('rb').read(); "
                "assert response.endswith(b'accepted')"
            )
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "bridge",
                    image,
                    "python3",
                    "-I",
                    "-c",
                    probe,
                ],
                capture_output=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr.decode()
            probes.append(probe)
        yield probes
    finally:
        subprocess.run(
            ["docker", "rm", "-f", instance], capture_output=True, check=True, timeout=15
        )


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize(
    "case",
    [
        "allowed",
        "missing-version",
        "missing-module",
        "wrong-lock",
        "correct-lock",
        "network-bypass",
    ],
)
def test_prepared_dependencies_in_closed_disposable_sandbox(
    engine, case, monkeypatch, unrestricted_receiver
):
    async def scenario():
        prepared = Path(ROOTS[engine])
        receipt = json.loads((prepared / "receipt.json").read_text())
        provider = receipt["providers"][0]
        module = receipt["modules"][0]
        files = {}
        for name, digest in module["files"].items():
            data = (prepared / "modules" / module["name"] / name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest
            files["modules/" + module["name"] + "/" + name] = data.decode()
        version = "0.0.1" if case == "missing-version" else provider["version"]
        source = provider["source"]
        module_path = "absent" if case == "missing-module" else module["name"]
        files["root/main.tf"] = (
            "terraform {\n  required_providers {\n"
            f'    random = {{ source = "{source}", version = "{version}" }}\n'
            '  }\n}\nmodule "approved" {\n'
            f'  source = "../modules/{module_path}"\n'
            "}\n"
        )
        if case == "correct-lock":
            # Generate a native lock in a disposable guest, including the unpacked h1 hash
            # needed by validation. A ZIP-only zh hash is insufficient after readonly init.
            executable = "tofu" if engine == "opentofu" else "terraform"
            script = (
                "import pathlib,sys,tempfile,subprocess\n"
                "with tempfile.TemporaryDirectory() as d:\n"
                " p=pathlib.Path(d); (p/'main.tf').write_text(sys.stdin.read())\n"
                " env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':d,"
                "'TF_CLI_CONFIG_FILE':'/opt/maf-terraform/terraform.rc','CHECKPOINT_DISABLE':'1'}\n"
                f" r=subprocess.run([{executable!r},'init','-backend=false','-input=false'],cwd=p,env=env,capture_output=True,timeout=20)\n"
                " assert r.returncode==0, 'fixture initialization failed'\n"
                " print((p/'.terraform.lock.hcl').read_text())\n"
            )
            generated = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-i",
                    IMAGES[engine],
                    "python3",
                    "-I",
                    "-c",
                    script,
                ],
                input=files["root/main.tf"].split('module "approved"')[0],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            files["root/.terraform.lock.hcl"] = generated.stdout
        elif case == "wrong-lock":
            # A zh:-only lock is unverifiable against the unpacked mirror, so the refusal
            # must come from a well-formed but wrong h1: hash.
            files["root/.terraform.lock.hcl"] = (
                f'provider "{source}" {{\n  version = "{version}"\n'
                f'  hashes = ["h1:{base64.b64encode(bytes(32)).decode()}"]\n}}\n'
            )
        store = InMemoryStore(files.copy())
        scope = "dependencies-1249-" + uuid.uuid4().hex
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "live",
            list_files=InMemoryStore.list,
        )
        backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        acquire = backend.acquire
        observed = []

        async def checked_acquire(key, spec):
            sandbox = await acquire(key, spec)
            observed.append(sandbox.instance_id)
            inspection = subprocess.run(
                ["docker", "inspect", sandbox.instance_id],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            assert json.loads(inspection.stdout)[0]["HostConfig"]["NetworkMode"] == "none"
            if case == "network-bypass":
                for command in unrestricted_receiver:
                    probe = await sandbox.exec(
                        ["/usr/local/bin/python3", "-I", "-c", command],
                        working_directory="/tmp",
                        timeout=5,
                    )
                    assert probe.exit_code != 0
            return sandbox

        monkeypatch.setattr(backend, "acquire", checked_acquire)
        tool = make_terraform_tools(
            router, store, "live", context, engine=engine, image=IMAGES[engine]
        )[0]
        try:
            result = await tool.func(files=list(files), root_module="root")
            text = "\n".join(item.text or "" for item in result)
            expected = (
                "Validation INCOMPLETE"
                if case in {"missing-version", "missing-module", "wrong-lock"}
                else "validation PASS"
            )
            assert expected in text, text
            assert store.files == files
            for instance in observed:
                inspected = subprocess.run(
                    ["docker", "inspect", instance], capture_output=True, timeout=10
                )
                assert inspected.returncode != 0
        finally:
            await router.dispose_scope(scope, "live")

    asyncio.run(scenario())
