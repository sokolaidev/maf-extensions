"""Exercise the document kind against Docker or an explicitly configured ACAS group."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from document_fixtures import documents, pdf
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
    make_file_system_sink,
)
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox_document import make_document_tools


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
            DockerSandboxConfig(memory="1g", cpus=2, pids_limit=256)
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
    scope = "document-check-" + uuid4().hex
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
                _require(len(identifiers.split()) == 1, "expected one isolated document container")
                observed = json.loads(await asyncio.to_thread(_docker, "inspect", identifiers))[0]
                _require(observed["Image"] == image_id, "document image identity differs")
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
        current_thread_id=lambda: "document",
        list_files=lambda store: store.list(),
    )
    fixtures = documents()
    cases: list[tuple[str, bytes, dict[str, Any], dict[str, Any], str]] = []
    for name, content in fixtures.items():
        for format in ("text", "markdown"):
            cases.append((name, content, {}, {"format": format}, "extracted"))
    cases.extend(
        [
            (
                "fixture.pdf",
                fixtures["fixture.pdf"],
                {},
                {"format": "images", "pages": "2-20"},
                "extracted",
            ),
            ("fixture.pdf", pdf(), {"max_pages": 1}, {}, "refused"),
            ("fixture.rtf", fixtures["fixture.rtf"], {"max_chars": 4}, {}, "refused"),
            ("fixture.docx", fixtures["fixture.docx"][:100], {}, {}, "refused"),
        ]
    )
    reports = []
    with tempfile.TemporaryDirectory(prefix="document-check-") as temporary:
        output_root = directory or Path(temporary)
        try:
            for index, (name, content, options, arguments, expected) in enumerate(cases):
                output = output_root / str(index)
                store = _Store(name, content)
                [tool] = make_document_tools(
                    router,
                    store,
                    "document-check",
                    context,
                    make_file_system_sink(output),
                    image=image_id,
                    **options,
                )
                answer = await tool.func(file=name, **arguments)
                texts = [str(item.text) for item in answer]
                _require(texts[0] == COMPLETED_TEXT, f"case {index} incomplete: {texts}")
                _require(f"Result: {expected}" in texts, f"case {index} returned the wrong verdict")
                metadata = json.loads((output / "document.json").read_text("utf-8"))
                _require(metadata["verdict"] == expected, "metadata verdict differs")
                if expected == "extracted":
                    for filename in metadata["files"]:
                        data = (output / filename).read_bytes()
                        if filename.endswith(".png"):
                            _require(data[:8] == b"\x89PNG\r\n\x1a\n", "image is not PNG")
                            _require(
                                struct.unpack(">II", data[16:24]) == (192, 96), "wrong image size"
                            )
                        else:
                            _require(
                                b"Document fixture" in data, "expected document text is absent"
                            )
                _require(events[-1].unclean == 0, "document cleanup failed")
                reports.append(
                    {
                        "case": index,
                        "file": name,
                        "format": arguments.get("format", "text"),
                        "verdict": expected,
                        "seconds": events[-1].seconds,
                        "outputs": {
                            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in output.iterdir()
                        },
                    }
                )
        finally:
            purge = await router.dispose_scope(scope, "document")
            _require(not purge.undisposed, "document scope cleanup failed")
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
        _require(not remaining, "document containers remain")
    return {
        "backend": provider,
        "imageId": image_id,
        "sdkSource": "checkout",
        "checks": reports,
        "observations": observations,
        "containersRemaining": [],
        "acasVerified": provider == "acas",
    }


def main() -> None:
    """Retain measured qualification for the selected backend and image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("docker", "acas"), default="docker")
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(check(args.image, provider=args.backend, directory=args.output))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "qualification.json").write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
