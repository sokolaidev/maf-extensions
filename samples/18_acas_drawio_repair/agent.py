"""Repair a model's architecture XML on ACAS, read its stored diagram, and remove it.

See README.md for the prebuilt image and host credentials; the guest has no allowed egress hosts.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-acas",
#     "maf-sandbox-drawio",
#     "maf-sandbox>=0.41",
# ]
# ///

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from _scaffold import (
    MEASURED,
    conversation_id,
    installed_versions,
    quoted,
    require_env_vars,
    result_text,
)
from agent_framework import Agent, AgentFileStore, FileAccessProvider, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import (
    Artifact,
    Egress,
    LandedArtifact,
    SandboxLandingExists,
    SandboxObserver,
    SandboxRouter,
    ToolCallEnded,
)
from maf_sandbox.maf import list_no_files, make_caller_context, make_file_store_sink
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_drawio import drawio_sandbox_spec, make_drawio_tools

SCOPE = "samples"
AGENT_ID = "architecture-designer"
THREAD_ID = conversation_id("sample-18")
MAX_REPAIRS = 3
MAX_XML_BYTES = 64 * 1024
MISSING_VERTEX = "missing_database"
BROKEN_EDGE = "api_to_database"
VERTICES = {"web": "Web client", "api": "Orders API", "database": "Orders database"}
EDGES = {"web_to_api": ("web", "api"), BROKEN_EDGE: ("api", "database")}
XML_ATTRIBUTES = {
    "mxfile": "host agent version modified type etag compressed",
    "diagram": "id name",
    "mxGraphModel": "dx dy grid gridSize guides tooltips connect arrows fold page pageScale pageWidth pageHeight math shadow background",
    "root": "",
    "mxCell": "id value vertex edge parent source target style",
    "mxGeometry": "as x y width height relative",
    "Array": "as",
    "mxPoint": "as x y",
    "mxRectangle": "as x y width height",
}
STYLE_VALUES = {
    "rounded": "[01]",
    "html": "[01]",
    "curved": "[01]",
    "orthogonalLoop": "[01]",
    "endFill": "[01]",
    "startFill": "[01]",
    "dashed": "[01]",
    "shadow": "[01]",
    "whiteSpace": "wrap",
    "shape": "rectangle|ellipse|cylinder",
    "edgeStyle": "none|orthogonalEdgeStyle|elbowEdgeStyle",
    "startArrow": "none|classic|block|open|oval|diamond",
    "endArrow": "none|classic|block|open|oval|diamond",
    "fillColor": "#[0-9a-fA-F]{6}|none",
    "strokeColor": "#[0-9a-fA-F]{6}|none",
    "fontColor": "#[0-9a-fA-F]{6}",
    "fontSize": "[0-9]+",
    "fontStyle": "[0-7]",
    "align": "left|center|right",
    "verticalAlign": "top|middle|bottom",
    "jettySize": "auto|[0-9]+",
}
SANDBOX_VARS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
    "DRAWIO_SANDBOX_IMAGE",
)
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


def xml_source(text: str) -> str:
    """Bound model output and remove one optional Markdown fence."""
    text = text.strip()
    if text.startswith(("```xml\n", "```drawio\n", "```\n")) and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if len(text.encode("utf-8")) > MAX_XML_BYTES:
        raise ValueError("Sample XML exceeds 64 KiB")
    return text


def xml_document(text: str) -> ET.Element:
    """Read native XML for architecture checks without expanding declared entities."""
    text = xml_source(text)
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Sample XML cannot declare entities or a DTD")
    document = ET.fromstring(text)
    if document.tag not in {"mxfile", "mxGraphModel"}:
        raise ValueError("Expected native draw.io XML")
    return document


def architecture(xml: str) -> ET.Element:
    """Require the fixture's complete graph, so an empty diagram cannot count as a repair."""
    document = xml_document(xml)
    self_contained(document)
    models = (
        [document] if document.tag == "mxGraphModel" else document.findall("diagram/mxGraphModel")
    )
    if len(models) != 1:
        raise ValueError("Expected exactly one diagram page")
    root = models[0].find("root")
    if root is None:
        raise ValueError("Missing diagram root")
    cells = {cell.get("id"): cell for cell in root}
    if len(cells) != len(root) or set(cells) != {"0", "1", *VERTICES, *EDGES}:
        raise ValueError("Expected the fixture's unique cell IDs")
    if any(cell.tag != "mxCell" for cell in root) or cells["1"].get("parent") != "0":
        raise ValueError("Expected a flat mxCell diagram")
    for identifier, label in VERTICES.items():
        cell = cells[identifier]
        if (cell.get("vertex"), cell.get("parent"), cell.get("value")) != ("1", "1", label):
            raise ValueError(f"Missing component or label: {identifier}")
    for identifier, (source, target) in EDGES.items():
        cell = cells[identifier]
        if (cell.get("edge"), cell.get("parent"), cell.get("source"), cell.get("target")) != (
            "1",
            "1",
            source,
            target,
        ):
            raise ValueError(f"Incorrect architecture connection: {identifier}")
    return document


def self_contained(document: ET.Element) -> None:
    """Allow only the fixture's plain labels, native geometry and built-in styles."""
    for element in document.iter():
        allowed = XML_ATTRIBUTES.get(element.tag)
        if allowed is None or element.attrib.keys() - set(allowed.split()):
            raise ValueError("Sample XML must be self-contained: unsupported element or attribute")
        if (element.text or "").strip() or (element.tail or "").strip():
            raise ValueError("Sample XML must be self-contained: unexpected text content")
        if any(character in element.get("value", "") for character in "<>&"):
            raise ValueError("Sample XML must be self-contained: labels must be plain text")
        for part in filter(None, element.get("style", "").split(";")):
            name, _, value = part.partition("=")
            pattern = STYLE_VALUES.get(name)
            if pattern is None or re.fullmatch(pattern, value) is None:
                raise ValueError(f"Sample XML must be self-contained: unsupported style {name!r}")


def inject_error(xml: str) -> str:
    """Break one existing edge target without changing the architecture's other cells."""
    document = architecture(xml)
    edge = document.find(f".//mxCell[@id='{BROKEN_EDGE}']")
    if edge is None:
        raise ValueError("Missing edge to corrupt")
    edge.set("target", MISSING_VERTEX)
    return ET.tostring(document, encoding="unicode")


def measure(stage: str, **values: object) -> None:
    """Write host evidence as one JSON line, including escaped untrusted values."""
    print(f"{MEASURED}{json.dumps({'stage': stage, **values}, ensure_ascii=True)}")


class CallTimings(SandboxObserver):
    """Report core's complete call duration, including sandbox cleanup."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.calls: list[str] = []

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        with self._lock:
            self.calls.append(event.call)
            measure(
                "tool_call_ended",
                tool=event.tool,
                kind=event.kind,
                call=event.call,
                seconds=event.seconds,
                failure=event.failure,
                unclean=event.unclean,
            )


class StoredDiagrams:
    """Track attempted destinations before writing, including partial storage failures."""

    def __init__(self, store: AgentFileStore) -> None:
        self.store = store
        self.attempted: set[str] = set()
        self.delivered: list[tuple[LandedArtifact, Artifact]] = []
        landing = make_file_store_sink(store)

        async def deliver(artifact: Artifact) -> LandedArtifact:
            if not artifact.call_id or artifact.name != "diagram.drawio":
                raise ValueError("Expected a call-scoped diagram.drawio artifact")
            destination = f"{artifact.call_id}/{artifact.name}"
            if await store.file_exists(destination):
                raise FileExistsError("Refusing to replace an existing artifact")
            architecture(artifact.content.decode("utf-8"))
            self.attempted.add(destination)
            try:
                landed = await landing.deliver(artifact)
            except SandboxLandingExists:
                self.attempted.discard(destination)
                raise
            self.delivered.append((landed, artifact))
            return landed

        self.sink = replace(landing, deliver=deliver)

    async def cleanup(self) -> None:
        """Attempt every owned deletion and fail if any file remains or cannot be checked."""
        failures: list[Exception] = []
        for destination in sorted(self.attempted):
            try:
                async with asyncio.timeout(30):
                    await self.store.delete(destination)
                    if await self.store.file_exists(destination):
                        raise RuntimeError(f"Sample artifact remains: {destination}")
            except Exception as exc:
                failures.append(exc)
        measure("storage_cleanup", attempted=len(self.attempted), failures=len(failures))
        if failures:
            raise ExceptionGroup("Sample storage cleanup failed", failures)


async def validate_diagram(
    converter: Any, xml: str, timings: CallTimings, storage: StoredDiagrams
) -> str:
    """Bind a converter result to the observed call and the exact submitted XML."""
    count = len(timings.calls)
    result = result_text(await converter.invoke(arguments={"xml": xml}))
    if len(timings.calls) != count + 1:
        raise RuntimeError("Expected exactly one observed draw.io call")
    measure(
        "validation",
        call=timings.calls[-1],
        sha256=hashlib.sha256(xml.encode()).hexdigest(),
        diagnostic=result,
        delivered=len(storage.delivered),
    )
    return result


async def repair_diagram(
    ask: Callable[[str], Awaitable[str]],
    validate: Callable[[str], Awaitable[str]],
    read_back: Callable[[str, str], Awaitable[bool]],
    storage: StoredDiagrams,
    markdown: str,
) -> None:
    """Exercise rejection, bounded model repair, artifact delivery and file-access read-back."""
    authored = await ask(
        f"Create draw.io XML for this architecture. Return only XML.\n\n{markdown}"
    )
    broken = inject_error(authored)
    measure("authored", sha256=hashlib.sha256(authored.encode()).hexdigest())
    measure(
        "corrupted",
        edge=BROKEN_EDGE,
        target=MISSING_VERTEX,
        sha256=hashlib.sha256(broken.encode()).hexdigest(),
    )
    diagnostic = await validate(broken)
    expected = f"Cell '{BROKEN_EDGE}'.target must reference a vertex"
    if not diagnostic.startswith("Error:") or expected not in diagnostic or storage.attempted:
        raise RuntimeError("The deliberately broken edge was not rejected without delivery")
    measure("rejected", diagnostic=diagnostic, delivered=0)
    candidate = broken
    for attempt in range(1, MAX_REPAIRS + 1):
        repaired = await ask(
            "Repair this XML using the converter diagnostic and architecture below. "
            "Return only new native XML, preserving every required component and connection.\n\n"
            f"Architecture:\n{markdown}\n\nXML:\n{candidate}\n\nDiagnostic:\n{diagnostic}"
        )
        candidate = xml_source(repaired)
        measure(
            "repair",
            attempt=attempt,
            sha256=hashlib.sha256(candidate.encode()).hexdigest(),
            diagnostic=diagnostic,
        )
        count = len(storage.delivered)
        diagnostic = await validate(candidate)
        if diagnostic.startswith("Error:"):
            if len(storage.delivered) != count or storage.attempted:
                raise RuntimeError("A failed conversion attempted artifact delivery")
            measure("repair_rejected", attempt=attempt, diagnostic=diagnostic)
            continue
        if len(storage.delivered) != count + 1:
            raise RuntimeError("Converter success did not deliver exactly one artifact")
        landed, artifact = storage.delivered[-1]
        expected_path = f"{artifact.call_id}/diagram.drawio"
        if landed.handle != expected_path or diagnostic != landed.display:
            raise RuntimeError("Success did not identify this call's stored artifact")
        architecture(candidate)
        saved = await storage.store.read(expected_path)
        if saved is None or saved.encode("utf-8") != artifact.content:
            raise RuntimeError("Stored XML differs from the converter artifact")
        architecture(saved)
        if not await read_back(expected_path, saved):
            raise RuntimeError("file_access_read did not return the delivered XML")
        measure("saved_and_read", path=expected_path, bytes=len(artifact.content), attempt=attempt)
        return
    raise RuntimeError(f"The model did not repair the diagram within {MAX_REPAIRS} attempts")


def read_was_verified(reply: Any, path: str, expected: str) -> bool:
    """Match a file-access result to the requested path and the actual delivered content."""
    calls: set[str] = set()
    for message in reply.messages:
        for content in message.contents:
            if content.type == "function_call" and content.name == "file_access_read":
                arguments = content.arguments
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if isinstance(arguments, dict) and arguments.get("file_name") == path:
                    calls.add(content.call_id)
    return any(
        content.type == "function_result"
        and content.call_id in calls
        and result_text(content.result) == expected
        for message in reply.messages
        for content in message.contents
    )


def build_backend(env: Mapping[str, str]) -> AcasSandboxBackend:
    """Use an existing ACAS group and an image already imported into that group."""
    return AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=env["ACAS_SANDBOX_ENDPOINT"],
            subscription_id=env["ACAS_SANDBOX_SUBSCRIPTION_ID"],
            resource_group=env["ACAS_SANDBOX_RESOURCE_GROUP"],
            sandbox_group=env["ACAS_SANDBOX_GROUP"],
            registry=env.get("ACAS_SANDBOX_REGISTRY", ""),
        )
    )


async def purge_scope(router: SandboxRouter, thread_id: str) -> None:
    """Require confirmed disposal, including when per-call cleanup already removed the sandbox."""
    async with asyncio.timeout(120):
        purge = await router.dispose_scope(SCOPE, thread_id)
    measure("sandbox_cleanup", disposed=purge.disposed, complete=purge.undisposed is None)
    if purge.undisposed is not None:
        raise RuntimeError("ACAS sandbox disposal was not confirmed")


async def run() -> int:
    """Run the live model/ACAS path and unwind storage, sandbox and client resources."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2
    image = env["DRAWIO_SANDBOX_IMAGE"]
    spec = drawio_sandbox_spec(image)
    if spec.egress != Egress.CLOSED or spec.egress_allow:
        raise RuntimeError("This sample requires closed guest egress")
    markdown = Path(__file__).with_name("architecture.md").read_text(encoding="utf-8")
    async with AsyncExitStack() as cleanup:
        backend = build_backend({**os.environ, **env})
        cleanup.push_async_callback(backend.aclose)
        timings = CallTimings()
        router = SandboxRouter([backend], observer=timings)
        cleanup.push_async_callback(purge_scope, router, THREAD_ID)
        storage = StoredDiagrams(InMemoryAgentFileStore())
        cleanup.push_async_callback(storage.cleanup)
        credential = await cleanup.enter_async_context(DefaultAzureCredential())
        context = make_caller_context(list_no_files, lambda: SCOPE, lambda: THREAD_ID)
        [converter] = make_drawio_tools(
            router, AGENT_ID, context, storage.sink, image=image, direction="LR"
        )
        agent = Agent(
            client=OpenAIChatClient(
                model=env["AZURE_OPENAI_CHAT_MODEL"],
                azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
                credential=credential,
            ),
            name=AGENT_ID,
            instructions=(
                "Generate and repair native draw.io XML from the architecture provided. "
                "Return only XML for authoring and repair requests. When asked to read a saved "
                "diagram, call file_access_read with its exact path, then answer briefly."
            ),
            context_providers=[
                FileAccessProvider(
                    storage.store, disable_write_tools=True, disable_readonly_tool_approval=True
                )
            ],
        )
        session = agent.create_session()

        async def ask(prompt: str) -> str:
            response = await agent.run(prompt, session=session)
            print(quoted(response.text))
            return response.text

        async def validate(xml: str) -> str:
            return await validate_diagram(converter, xml, timings, storage)

        async def read_back(path: str, expected: str) -> bool:
            reply = await agent.run(
                f"Read the saved diagram with file_access_read: {path}. Do not recreate it.",
                session=session,
            )
            print(quoted(reply.text))
            return read_was_verified(reply, path, expected)

        measure("configuration", backend="acas", guest_egress="closed", allowed_hosts=[])
        async with asyncio.timeout(600):
            await repair_diagram(ask, validate, read_back, storage, markdown)
    measure("complete")
    return 0


if __name__ == "__main__":
    print(installed_versions())
    try:
        raise SystemExit(asyncio.run(run()))
    except Exception as exc:
        print(quoted(f"Sample failed: {exc}"), file=sys.stderr)
        raise SystemExit(1) from None
