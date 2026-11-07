"""Qualify Markdown export, PDF contents, resource denial and disposal on Docker."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from maf_sandbox import Sandbox, SandboxKey, SandboxRouter, SandboxSpec
from maf_sandbox_document import ExportOptions, export_pdf
from pypdf import PdfReader

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="60" viewBox="0 0 240 60">'
    b'<rect width="240" height="60" fill="#eaf0f4"/>'
    b'<text x="10" y="35" font-family="DejaVu Sans" font-size="18">Supplied SVG</text></svg>'
)
FIXTURE = (
    "# Export fixture\n\nA report with **bold**, ~~struck~~ and emoji 😀.\n\n"
    "| One | Two | Three | Four | Five | Six |\n|---|---|---|---|---|---|\n"
    "| wide table content | second column | third column | fourth column | fifth column | sixth column |\n\n"
    "```python\n"
    + "print('long code line "
    + "abcdefghij" * 30
    + "')\n"
    + "\n".join(f"line_{i} = {i}" for i in range(70))
    + "\n```\n\n```mermaid\nflowchart LR\n  A[Start report] --> B[Finish report]\n```\n\n"
    "![Supplied diagram](diagram.svg)\n\n"
    + "\n\n".join(f"## Section {i}\n\nPagination fixture paragraph {i}." for i in range(25))
)


def require(condition: bool, message: str) -> None:
    """Fail qualification without depending on interpreter assertion settings."""
    if not condition:
        raise ValueError(message)


def docker(*args: str) -> str:
    """Read bounded local engine evidence."""
    return subprocess.check_output(
        ["docker", *args], text=True, encoding="utf-8", timeout=30
    ).strip()


def inspect_pdf(data: bytes) -> dict[str, Any]:
    """Require real pagination, embedded text/monospace fonts and fixture content."""
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() for page in reader.pages)
    fonts: set[str] = set()
    for page in reader.pages:
        resources: Any = page["/Resources"]
        for reference in resources.get("/Font", {}).values():
            font = reference.get_object()
            for item in font.get("/DescendantFonts", [font]):
                item = item.get_object()
                descriptor = item.get("/FontDescriptor")
                if descriptor is not None:
                    descriptor = descriptor.get_object()
                    require(
                        any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")),
                        "font is not embedded",
                    )
                    fonts.add(str(descriptor["/FontName"]))
    require(len(reader.pages) > 1, "fixture did not paginate")
    for expected in (
        "Export fixture",
        "wide table content",
        "line_69",
        "Start report",
        "Finish report",
        "Supplied SVG",
        "Page 1 of",
    ):
        require(expected in text, f"missing extracted text: {expected}")
    require(any("DejaVuSans" in name for name in fonts), "text font missing")
    require(any("DejaVuSansMono" in name for name in fonts), "monospace font missing")
    return {"pages": len(reader.pages), "fonts": sorted(fonts), "text": text}


async def check(image_id: str, *, directory: Path | None = None) -> dict[str, Any]:
    """Exercise the public host API using a single immutable, closed-network image."""
    from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

    image_id = await asyncio.to_thread(docker, "image", "inspect", image_id, "--format", "{{.Id}}")
    backend = await DockerSandboxBackend.create(
        DockerSandboxConfig(memory="2g", cpus=2, pids_limit=512)
    )
    scope = "document-export-check-" + uuid4().hex
    observations: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    sandboxes: dict[SandboxKey, Sandbox] = {}

    class Router(SandboxRouter):
        async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
            sandbox = await super().acquire(key, spec, **kwargs)
            sandboxes[key] = sandbox
            identifiers = await asyncio.to_thread(
                docker, "ps", "-aq", "--filter", f"label=maf-sandbox.scope={scope}"
            )
            require(len(identifiers.split()) == 1, "expected one export container")
            observed = json.loads(await asyncio.to_thread(docker, "inspect", identifiers))[0]
            require(observed["Image"] == image_id, "image identity differs")
            require(observed["HostConfig"]["NetworkMode"] == "none", "egress is not closed")
            observations.append({"instance": observed["Id"], "image": image_id, "network": "none"})
            return sandbox

        async def dispose_kind(
            self, key: SandboxKey, kind: str, *, instance_id: str | None = None, timeout: float
        ) -> bool:
            try:
                sandbox = sandboxes.get(key)
                if sandbox is not None:
                    data = await sandbox.read_file(
                        "export.json",
                        working_directory=f"/maf-sandbox/work/{key.call_id}",
                        max_bytes=4096,
                    )
                    metadata.append(json.loads(data))
            except FileNotFoundError:
                pass
            finally:
                cleaned = await super().dispose_kind(
                    key, kind, instance_id=instance_id, timeout=timeout
                )
            return cleaned

    router = Router([backend])
    key = SandboxKey(scope, "export", "qualification")
    evidence: dict[str, Any] = {}
    try:
        for size in ("A4", "Letter"):
            data = await export_pdf(
                FIXTURE,
                {"diagram.svg": SVG},
                ExportOptions(
                    title="Qualification report",
                    date="2026-11-07",
                    page_size=size,
                    deadline_seconds=120,
                ),
                router=router,
                key=key,
                image=image_id,
            )
            content = inspect_pdf(data)
            require(metadata[-1]["pages"] == content["pages"], "page evidence differs")
            require(
                metadata[-1]["mermaid_rendered"] == 1 and metadata[-1]["mermaid_fallbacks"] == 0,
                "flowchart was not rendered",
            )
            require(metadata[-1]["network_requests"] == [], "page attempted network access")
            evidence[size] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "pages": content["pages"],
                "fonts": content["fonts"],
                "renderer": metadata[-1],
            }
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"fixture-{size}.pdf").write_bytes(data)
                (directory / f"fixture-{size}.txt").write_text(content["text"], "utf-8")
        fallback = await export_pdf(
            "```mermaid\nnot a valid diagram\n```", {}, router=router, key=key, image=image_id
        )
        fallback_text = "".join(
            page.extract_text() for page in PdfReader(io.BytesIO(fallback)).pages
        )
        require(
            "Diagram could not be rendered" in fallback_text
            and "not a valid diagram" in fallback_text,
            "Mermaid fallback is missing",
        )
        require(metadata[-1]["mermaid_fallbacks"] == 1, "fallback evidence is missing")
        for markdown, assets, options in (
            (FIXTURE, {"diagram.svg": SVG}, ExportOptions(max_pages=1, deadline_seconds=120)),
            ("![remote](https://example.invalid/image.svg)", {}, ExportOptions()),
        ):
            try:
                await export_pdf(markdown, assets, options, router=router, key=key, image=image_id)
            except ValueError:
                pass
            else:
                raise ValueError("expected export refusal")
    finally:
        purge = await router.dispose_scope(scope, "export")
        require(not purge.undisposed, "scope cleanup failed")
    remaining = await asyncio.to_thread(
        docker, "ps", "-aq", "--filter", f"label=maf-sandbox.scope={scope}"
    )
    require(not remaining, "export containers remain")
    require(
        len({row["instance"] for row in observations}) == 5,
        "calls did not receive five fresh instances",
    )
    return {
        "backend": "docker",
        "imageId": image_id,
        "checks": evidence,
        "mermaidFallbackVerified": True,
        "refusalsVerified": True,
        "observations": observations,
        "containersRemaining": [],
    }


def main() -> None:
    """Retain exact-image qualification and fixture PDFs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(check(args.image, directory=args.output))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "qualification.json").write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
