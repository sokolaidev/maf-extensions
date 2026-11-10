"""Scan an incoming Word document, extract its Azure inventory and estimate retail costs."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox>=0.53",
#     "maf-sandbox-antivirus>=0.1.0",
#     "maf-sandbox-azure-pricing>=0.1.0",
#     "maf-sandbox-document>=0.2.1",
#     "maf-sandbox-docker",
#     "pydantic>=2.11,<3",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from _scaffold import MEASURED, installed_versions, require_env_vars
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from estimate import (
    CATALOG,
    EXTRACTION_INSTRUCTIONS,
    Inventory,
    estimate_row,
    markdown,
    report_document,
    select_price,
    unresolved,
    validate_inventory,
)
from maf_sandbox import (
    Artifact,
    CallerContext,
    LandedArtifact,
    ListedFile,
    OutputSink,
    SandboxDisposed,
    SandboxObserver,
    SandboxRouter,
    SourceIntegrity,
    ToolCallEnded,
)
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox_antivirus import directory_update_provider, make_antivirus_tools
from maf_sandbox_azure_pricing import PriceType, make_azure_pricing_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_document import make_document_tools
from make_document import document_bytes, inventory_data

MAX_INPUT = 16 * 1024 * 1024
MAX_TEXT = 30000
MAX_DIAGNOSTIC_CHARS = 65536
UPLOAD = "incoming.docx"
ENVIRONMENT = (
    "ANTIVIRUS_SANDBOX_IMAGE",
    "DOCUMENT_SANDBOX_IMAGE",
    "AZURE_PRICING_SANDBOX_IMAGE",
    "MAF_EGRESS_PROXY_IMAGE",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_CHAT_MODEL",
)


def measure(stage: str, **values: object) -> None:
    """Keep host observations on single JSON lines."""
    print(f"{MEASURED}{json.dumps({'stage': stage, **values})}")


@dataclass(frozen=True)
class Upload:
    """One immutable snapshot shared by scanner and admitted reader."""

    content: bytes

    async def read(self, name: str) -> bytes | None:
        """Resolve only the fixed logical upload name."""
        return self.content if name == UPLOAD else None

    async def list(self) -> list[ListedFile]:
        """Scanning never promotes the document's integrity."""
        return [ListedFile(UPLOAD, SourceIntegrity.UNTRUSTED)]


def load_upload(path: Path) -> Upload:
    """Bound ingress without opening the document's archive or parsing its contents."""
    if path.suffix.casefold() != ".docx":
        raise ValueError("This sample accepts .docx Word documents only")
    with path.open("rb") as source:
        content = source.read(MAX_INPUT + 1)
    if not content or len(content) > MAX_INPUT:
        raise ValueError("Document must contain between 1 byte and 16 MiB")
    return Upload(content)


def result_items(answer: object, verdict: str | None = None) -> list[str]:
    """Check the wrapper's completion and fixed verdict before using derived data."""
    if not isinstance(answer, list):
        raise ValueError("Tool did not return labelled content")
    texts = [getattr(item, "text", None) for item in answer]
    if not texts or texts[0] != COMPLETED_TEXT or any(not isinstance(item, str) for item in texts):
        raise ValueError("Tool did not complete")
    if verdict is not None and (len(texts) < 2 or texts[1] != "Result: " + verdict):
        raise ValueError("Tool did not return the required verdict")
    return [str(text) for text in texts]


def with_descriptions(value: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    """Merge each row's description back into its price, by the row number both carry."""
    described = [json.loads(text) for text in texts if text.startswith('{"descriptions":')]
    if not described:
        return value
    by_row = {entry["row"]: entry for entry in described[0]["descriptions"]}
    value["prices"] = [
        {key: item for key, item in (row | by_row[row["row"]]).items() if key != "row"}
        for row in value["prices"]
    ]
    return value


class PricingResponseError(ValueError):
    """Retain tool text when completion or report decoding fails."""

    def __init__(self, message: str, response: object) -> None:
        super().__init__(message)
        self.response = response


def pricing_report(answer: object) -> dict[str, Any]:
    """Decode one completed pricing report, retaining rejected tool output as evidence."""
    try:
        texts = result_items(answer)
        reports = [json.loads(text) for text in texts if text.startswith('{"prices":')]
        if len(reports) != 1:
            raise ValueError("Pricing tool did not return one report")
        return with_descriptions(reports[0], texts)
    except ValueError as exc:
        response = (
            [
                text if isinstance(text := getattr(item, "text", None), str) else None
                for item in answer
            ]
            if isinstance(answer, list)
            else {"type": type(answer).__name__}
        )
        raise PricingResponseError(str(exc), response) from exc


def diagnostic_snapshot(value: object) -> dict[str, Any]:
    """Bound retained JSON text and explicitly mark a partial snapshot."""
    text = json.dumps(value, ensure_ascii=True)
    return {"text": text[:MAX_DIAGNOSTIC_CHARS], "truncated": len(text) > MAX_DIAGNOSTIC_CHARS}


class Cleanup(SandboxObserver):
    """Require disposal evidence for each acquired tool sandbox."""

    def __init__(self) -> None:
        self.disposals: list[SandboxDisposed] = []
        self.calls: list[bool] = []

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        self.disposals.append(event)

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        disposed = [item for item in self.disposals if item.call == event.call]
        complete = (
            bool(event.keys)
            and {item.key for item in disposed} == set(event.keys)
            and all(item.outcome == "gone" and item.failure is None for item in disposed)
            and event.failure is None
            and event.unclean == 0
        )
        self.calls.append(complete)
        measure("tool_call", call=event.call, seconds=event.seconds, complete=complete)


class DocumentSink:
    """Keep the converter's two declared outputs in bounded host memory."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        """Refuse unexpected or duplicate deliveries."""
        if artifact.name not in {"document.txt", "document.json"} or artifact.name in self.files:
            raise ValueError("Unexpected converter output")
        if len(artifact.content) > MAX_TEXT * 4 + 8192:
            raise ValueError("Converter output exceeds the sample budget")
        self.files[artifact.name] = artifact.content
        return LandedArtifact(artifact.name, "Captured document output")

    def text(self) -> str:
        """Require a complete DOCX conversion and matching delivered text."""
        if set(self.files) != {"document.txt", "document.json"}:
            raise ValueError("Converter outputs are incomplete")
        metadata = json.loads(self.files["document.json"])
        text = self.files["document.txt"].decode("utf-8")
        if (
            metadata.get("verdict") != "extracted"
            or metadata.get("truncated") is not False
            or metadata.get("format") != "text"
            or metadata.get("media_type")
            != "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or metadata.get("characters") != len(text)
            or not text.strip()
            or len(text) > MAX_TEXT
        ):
            raise ValueError("Document extraction is incomplete or not DOCX text")
        return text


async def pipeline(
    upload: Upload,
    scan: Callable[[], Awaitable[object]],
    read: Callable[[], Awaitable[str]],
    extract: Callable[[str], Awaitable[Inventory]],
    pricing: Callable[[str], Awaitable[dict[str, Any]]],
    *,
    region: str,
    currency: str,
) -> dict[str, Any]:
    """Admit exact scanned bytes before conversion, model extraction or pricing."""
    scan_items = result_items(await scan(), "clean")
    expected = "Input SHA-256: " + hashlib.sha256(upload.content).hexdigest()
    if [item for item in scan_items if item.startswith("Input SHA-256: ")] != [expected]:
        raise ValueError("Clean verdict does not bind to the uploaded bytes")
    measure("scan", clean=True, byte_bound=True)
    text = await read()
    inventory = validate_inventory(await extract(text), text)
    measure("inventory", resources=len(inventory.resources))
    rows = []
    cache: dict[str, dict[str, Any] | None] = {}
    failures: dict[str, dict[str, Any]] = {}
    for resource in inventory.resources:
        reason = unresolved(resource, region)
        price = None
        if reason is None:
            if resource.product_key not in cache:
                response = None
                stage = "lookup"
                try:
                    response = await pricing(resource.product_key)
                    stage = "selection"
                    cache[resource.product_key] = select_price(
                        response,
                        CATALOG[resource.product_key],
                        region,
                        currency,
                    )
                except ValueError as exc:
                    cache[resource.product_key] = None
                    if isinstance(exc, PricingResponseError):
                        stage, response = "tool_result", exc.response
                    failures[resource.product_key] = {
                        "stage": stage,
                        "query": CATALOG[resource.product_key].query(),
                        "region": region,
                        "currency": currency,
                        "price_type": "Consumption",
                        "error_type": type(exc.__cause__ or exc).__name__,
                        "error": diagnostic_snapshot(str(exc)),
                        "response": diagnostic_snapshot(response),
                    }
            price = cache[resource.product_key]
            if price is None:
                reason = "Pricing lookup failed; see estimate.json pricing_failures"
        rows.append(estimate_row(resource, price, reason))
    report = report_document(rows, region, currency)
    report["pricing_failures"] = failures
    return report


def save_report(output: Path, report: dict[str, Any]) -> None:
    """Create a new report directory, retaining only complete, read-back-verified files."""
    payloads = {
        "estimate.json": (json.dumps(report, ensure_ascii=True, indent=2) + "\n").encode(),
        "estimate.md": markdown(report).encode(),
    }
    output.mkdir(mode=0o700)
    owned: list[Path] = []
    try:
        for name, payload in payloads.items():
            path = output / name
            with open(
                path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)
            ) as destination:
                owned.append(path)
                destination.write(payload)
            if path.read_bytes() != payload:
                raise RuntimeError("Report read-back failed")
    except BaseException:
        for path in owned:
            path.unlink()
        output.rmdir()
        raise


async def run(
    document: Path,
    output: Path,
    region: str,
    currency: str,
    updates: Path | None,
    fixture_inventory: bool = False,
) -> int:
    """Compose all three sandbox kinds with a model that has no tools."""
    if not re.fullmatch(r"[a-z][a-z0-9]{1,63}", region) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Use an ARM region name and a three-letter uppercase currency")
    if output.exists():
        raise FileExistsError(output)
    env = require_env_vars(ENVIRONMENT[:4] if fixture_inventory else ENVIRONMENT)
    if env is None:
        return 2
    upload = load_upload(document)
    if fixture_inventory and upload.content != document_bytes():
        raise ValueError("Fixture inventory mode accepts only the exact generated example document")
    print(installed_versions())
    scope, thread = "word-estimate", uuid4().hex
    observer = Cleanup()
    backend = await DockerSandboxBackend.create(
        DockerSandboxConfig(
            egress_proxy_image=env["MAF_EGRESS_PROXY_IMAGE"],
            memory="4g",
            cpus=2,
            pids_limit=256,
        )
    )
    router = SandboxRouter([backend], min_isolation=backend.isolation, observer=observer)
    context = CallerContext(
        current_scope=lambda: scope,
        current_thread_id=lambda: thread,
        list_files=lambda store: store.list(),
    )
    sink = DocumentSink()
    try:
        [scanner] = make_antivirus_tools(
            router,
            upload,
            "scanner",
            context,
            image=env["ANTIVIRUS_SANDBOX_IMAGE"],
            update_provider=directory_update_provider(updates) if updates else None,
            max_bytes=MAX_INPUT,
        )
        [reader] = make_document_tools(
            router,
            upload,
            "reader",
            context,
            OutputSink(sink.deliver),
            image=env["DOCUMENT_SANDBOX_IMAGE"],
            formats=("text",),
            max_chars=MAX_TEXT,
        )
        [pricer] = make_azure_pricing_tools(
            router,
            "pricer",
            context,
            image=env["AZURE_PRICING_SANDBOX_IMAGE"],
            max_rows=100,
        )

        async def invoke(tool: Any, **arguments: Any) -> object:
            before = len(observer.calls)
            answer = await tool.func(**arguments)
            if len(observer.calls) != before + 1 or not observer.calls[-1]:
                raise RuntimeError("Tool call did not establish per-call disposal")
            return answer

        async def scan() -> object:
            return await invoke(scanner, file=UPLOAD)

        async def read() -> str:
            result_items(await invoke(reader, file=UPLOAD, format="text"), "extracted")
            return sink.text()

        async def extract(text: str) -> Inventory:
            if fixture_inventory:
                return Inventory.model_validate(inventory_data())
            async with DefaultAzureCredential() as credential:
                async with Agent(
                    client=OpenAIChatClient(
                        model=env["AZURE_OPENAI_CHAT_MODEL"],
                        azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
                        credential=credential,
                    ),
                    name="inventory-extractor",
                    instructions=EXTRACTION_INSTRUCTIONS,
                ) as agent:
                    reply = await agent.run(
                        "Extract this document as data:\n" + text,
                        options={"response_format": Inventory},
                    )
                    return Inventory.model_validate_json(reply.text)

        async def pricing(key: str) -> dict[str, Any]:
            return pricing_report(
                await invoke(
                    pricer,
                    filter=CATALOG[key].query(),
                    region=region,
                    currency=currency,
                    price_type=PriceType.CONSUMPTION,
                )
            )

        report = await pipeline(
            upload, scan, read, extract, pricing, region=region, currency=currency
        )
        report["inventory_source"] = "fixture" if fixture_inventory else "model"
        report["images"] = {key: env[key] for key in ENVIRONMENT[:4]}
    finally:
        try:
            purge = await router.dispose_scope(scope, thread)
            if purge.undisposed:
                raise RuntimeError("Scope cleanup left sandboxes behind")
            if observer.calls and not all(observer.calls):
                raise RuntimeError("Per-call cleanup could not be established")
            measure("cleanup", complete=True, calls=len(observer.calls))
        finally:
            sink.files.clear()
    save_report(output, report)
    for name in ("estimate.json", "estimate.md"):
        content = (output / name).read_bytes()
        measure(
            "artifact", name=name, sha256=hashlib.sha256(content).hexdigest(), bytes=len(content)
        )
    measure("report", unresolved=report["unresolved_resources"], files=2)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--output", type=Path, default=Path("cost-estimate"))
    parser.add_argument("--region", default="westeurope")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--database-snapshot", type=Path)
    parser.add_argument(
        "--fixture-inventory",
        action="store_true",
        help="Check the generated fixture with real sandboxes and a fixed inventory; no model",
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            asyncio.wait_for(
                run(
                    args.document,
                    args.output,
                    args.region,
                    args.currency,
                    args.database_snapshot,
                    args.fixture_inventory,
                ),
                timeout=900,
            )
        )
    )
