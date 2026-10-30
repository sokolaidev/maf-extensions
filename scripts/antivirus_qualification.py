"""Exercise offline antivirus scans through Docker or an explicitly configured ACAS group."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from maf_sandbox import (
    CallerContext,
    Isolation,
    ListedFile,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    ToolCallEnded,
)
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox_antivirus import (
    DatabaseIdentity,
    DatabaseUpdate,
    UpdateFile,
    directory_update_provider,
    make_antivirus_tools,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _docker(*args: str) -> str:
    return subprocess.check_output(
        ["docker", *args], text=True, encoding="utf-8", timeout=30
    ).strip()


class _Store:
    def __init__(self, name: str, content: bytes) -> None:
        self.name, self.content = name, content

    async def read(self, name: str) -> bytes | None:
        return self.content if name == self.name else None

    async def list(self) -> list[ListedFile]:
        return [ListedFile(self.name, SourceIntegrity.UNTRUSTED)]


async def check(
    image_id: str, *, provider: str = "docker", directory: Path | None = None
) -> dict[str, Any]:
    """Verify real output bytes, per-call instances and disposal without a model."""
    backend: SandboxBackend
    if provider == "docker":
        from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

        image_id = await asyncio.to_thread(
            _docker, "image", "inspect", image_id, "--format", "{{.Id}}"
        )
        backend = await DockerSandboxBackend.create(
            DockerSandboxConfig(memory="4g", cpus=2, pids_limit=256)
        )
    else:
        from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

        backend = AcasSandboxBackend(
            AcasSandboxConfig(
                endpoint=os.environ["ACAS_SANDBOX_ENDPOINT"],
                subscription_id=os.environ["ACAS_SANDBOX_SUBSCRIPTION_ID"],
                resource_group=os.environ["ACAS_SANDBOX_RESOURCE_GROUP"],
                sandbox_group=os.environ["ACAS_SANDBOX_GROUP"],
                registry=os.environ.get("ACAS_SANDBOX_REGISTRY", ""),
            )
        )
    scope = "antivirus-check-" + uuid4().hex
    events: list[ToolCallEnded] = []
    observations: list[dict[str, str]] = []

    class Observer(SandboxObserver):
        def tool_call_ended(self, event: ToolCallEnded) -> None:
            events.append(event)

    class Router(SandboxRouter):
        async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
            sandbox = await super().acquire(key, spec, **kwargs)
            if provider == "docker":
                identifiers = await asyncio.to_thread(
                    _docker, "ps", "-aq", "--filter", f"label=maf-sandbox.scope={scope}"
                )
                _require(len(identifiers.split()) == 1, "expected one isolated antivirus container")
                observed = json.loads(await asyncio.to_thread(_docker, "inspect", identifiers))[0]
                _require(observed["Image"] == image_id, "antivirus image identity differs")
                _require(observed["HostConfig"]["NetworkMode"] == "none", "egress is not closed")
                observations.append(
                    {"instance": observed["Id"], "image": image_id, "network": "none"}
                )
            else:
                observations.append({"instance": str(sandbox.instance_id), "image": image_id})
            return sandbox

    router = Router([backend], min_isolation=Isolation.CONTAINER, observer=Observer())
    context = CallerContext(
        current_scope=lambda: scope,
        current_thread_id=lambda: "antivirus",
        list_files=lambda store: store.list(),
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("expanded.txt", b"x" * 8192)
    eicar = (
        b"X5O!P%@AP[4"
        + bytes([92])
        + b"PZX54(P^)7CC)7}$"
        + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    )
    cases: list[tuple[str, bytes, dict[str, Any], str]] = [
        ("clean", b"harmless upload", {}, "clean"),
        ("eicar", eicar, {}, "infected"),
        ("expanded-limit", archive.getvalue(), {"max_filesize": 1024}, "refused"),
        ("stale", b"harmless", {"max_database_age_days": 1 / 86400}, "refused"),
    ]
    if provider == "docker":
        bytecode = await asyncio.to_thread(
            subprocess.check_output,
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                image_id,
                "cat",
                "/opt/maf-antivirus/seed/bytecode.cvd",
            ],
            timeout=60,
        )

        async def replacement(seed: tuple[DatabaseIdentity, ...]) -> DatabaseUpdate:
            return DatabaseUpdate(
                seed,
                tuple((item.name, item.version) for item in seed),
                (UpdateFile("bytecode.cvd", bytecode),),
            )

        async def corrupted(seed: tuple[DatabaseIdentity, ...]) -> DatabaseUpdate:
            return DatabaseUpdate(
                seed,
                tuple((item.name, item.version) for item in seed),
                (UpdateFile("bytecode.cvd", bytecode[:-1] + bytes([bytecode[-1] ^ 255])),),
            )

        cases.extend(
            [
                ("host-replacement", eicar, {"update_provider": replacement}, "infected"),
                ("corrupt-replacement", eicar, {"update_provider": corrupted}, "error"),
            ]
        )
    mirror = os.environ.get("MAF_ANTIVIRUS_UPDATE_DIRECTORY")
    mirror_updates: list[DatabaseUpdate] = []
    if mirror:
        mirror_provider = directory_update_provider(Path(mirror))

        async def from_mirror(seed: tuple[DatabaseIdentity, ...]) -> DatabaseUpdate:
            bundle = await mirror_provider(seed)
            mirror_updates.append(bundle)
            return bundle

        cases.append(
            (
                "host-update",
                eicar,
                {"update_provider": from_mirror},
                "infected",
            )
        )
    reports = []
    try:
        for label, content, options, expected in cases:
            print(f"Checking {label}", flush=True)
            store = _Store("upload", content)
            [tool] = make_antivirus_tools(
                router, store, "antivirus-check", context, image=image_id, **options
            )
            answer = await tool.func(file="upload")
            texts = [str(item.text) for item in answer]
            if expected == "error":
                _require(texts[0] != COMPLETED_TEXT, f"{label} unexpectedly completed: {texts}")
                _require(not any(text.startswith("Result:") for text in texts), "error has verdict")
            else:
                _require(texts[0] == COMPLETED_TEXT, f"{label} incomplete: {texts}")
                _require(
                    f"Result: {expected}" in texts, f"{label} returned the wrong verdict: {texts}"
                )
            if expected == "infected":
                _require(
                    any("Eicar" in text or "EICAR" in text for text in texts),
                    "EICAR signature absent",
                )
            if label != "stale" and expected != "error":
                _require(
                    "Input SHA-256: " + hashlib.sha256(content).hexdigest() in texts,
                    "input identity absent",
                )
            _require(events[-1].unclean == 0, "antivirus cleanup failed")
            reports.append(
                {"case": label, "verdict": expected, "seconds": events[-1].seconds, "result": texts}
            )
    finally:
        purge = await router.dispose_scope(scope, "antivirus")
        _require(not purge.undisposed, "antivirus scope cleanup failed")
        close = getattr(backend, "aclose", None)
        if close:
            await close()
    _require(len(events) == len(cases) == len(observations), "missing call evidence")
    _require(
        len({row["instance"] for row in observations}) == len(cases), "calls shared an instance"
    )
    if provider == "docker":
        remaining = await asyncio.to_thread(
            _docker, "ps", "-aq", "--filter", f"label=maf-sandbox.scope={scope}"
        )
        _require(not remaining, "antivirus containers remain")
    result = {
        "backend": provider,
        "imageId": image_id,
        "sdkSource": "checkout",
        "checks": reports,
        "observations": observations,
        "containersRemaining": [],
        "acasVerified": provider == "acas",
        "hostUpdatesVerified": any(bundle.files for bundle in mirror_updates),
        "hostDeltaVerified": any(
            item.name.endswith(".cdiff") for bundle in mirror_updates for item in bundle.files
        ),
        "mirrorUpdates": [bundle.manifest() for bundle in mirror_updates],
        "hostReplacementVerified": provider == "docker",
    }
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "qualification.json").write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    return result


def main() -> None:
    """Retain measured qualification for the selected backend and immutable image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("docker", "acas"), default="docker")
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check(args.image, provider=args.backend, directory=args.output))))


if __name__ == "__main__":
    main()
