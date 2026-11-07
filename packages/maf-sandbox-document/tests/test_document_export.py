"""Document export refusals, isolated routing, bounded delivery and disposal."""

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import TypedDict

import pytest
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    DisposalFailure,
    Egress,
    ExecResult,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxKey,
    SandboxRouter,
)
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_document import ExportOptions, document_export_sandbox_spec, export_pdf
from maf_sandbox_document._export_contract import MAXIMA, validate_svg

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
PDF = b"%PDF-1.7\nfixture"


class Routing(TypedDict):
    router: SandboxRouter
    key: SandboxKey
    image: str


class Renderer(InProcessSandbox):
    def __init__(self, *, mutation=None, exit_code=0, wait=False):
        super().__init__()
        self.mutation, self.exit_code, self.wait = mutation, exit_code, wait
        self.started = asyncio.Event()
        self.requests = []

    async def exec(self, command, *, working_directory, timeout):
        self.started.set()
        if self.wait:
            await asyncio.Event().wait()
        assert command == ["python3", "-I", "/opt/maf-document-export/render.py"]
        root = self._working_directory(working_directory)
        self.requests.append(json.loads(self.contents[f"{root}/request.json"]))
        metadata = dict(
            schema_version=1,
            pages=1,
            bytes=len(PDF),
            sha256=hashlib.sha256(PDF).hexdigest(),
            network_requests=[],
        )
        metadata.update(self.mutation or {})
        self.contents[f"{root}/document.pdf"] = PDF
        self.contents[f"{root}/export.json"] = json.dumps(metadata).encode()
        return ExecResult(exit_code=self.exit_code, stderr="guard refusal")


def setup(sandbox=None):
    sandbox = sandbox or Renderer()
    backend = InProcessSandboxBackend(
        sandbox,
        isolation=Isolation.CONTAINER,
        sandbox_per_key=True,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset({IsolationScope.CALL}),
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    return (
        sandbox,
        backend,
        Routing(
            router=router,
            key=SandboxKey("host-scope", "host-thread", "exporter"),
            image="document-export:test",
        ),
    )


def test_spec_requires_explicit_separate_image_and_closed_call_isolation():
    spec = document_export_sandbox_spec("document-export:test")
    assert spec.kind == "document"
    assert spec.image == "document-export:test"
    assert spec.egress is Egress.CLOSED
    assert spec.isolation_scope is IsolationScope.CALL
    with pytest.raises(ValueError):
        document_export_sandbox_spec("")


@pytest.mark.parametrize("name", MAXIMA)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, 10**12])
def test_options_refuse_invalid_bounds(name, value):
    with pytest.raises(ValueError, match=name):
        ExportOptions(**{name: value})


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 301])
def test_deadline_is_bounded(value):
    with pytest.raises(ValueError, match="deadline"):
        ExportOptions(deadline_seconds=value)


@pytest.mark.parametrize("options", [{"page_size": "A3"}, {"title": "x" * 1025}, {"date": None}])
def test_metadata_options(options):
    with pytest.raises(ValueError):
        ExportOptions(**options)


@pytest.mark.parametrize(
    "content",
    [
        b"not svg",
        b'<html xmlns="http://www.w3.org/1999/xhtml"/>',
        b'<!DOCTYPE svg [<!ENTITY x "hello">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><use href="https://example.invalid/a.svg"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.invalid";</style></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(https://example.invalid)"/></svg>',
    ],
)
def test_active_or_non_svg_is_refused(content):
    with pytest.raises(ValueError):
        validate_svg(content)


def test_passive_svg_local_references_are_allowed():
    validate_svg(SVG)
    validate_svg(
        b'<svg xmlns="http://www.w3.org/2000/svg"><use href="#shape"/><path fill="url(#gradient)"/></svg>'
    )


@pytest.mark.parametrize(
    "markdown,assets,options",
    [
        ("too long", {}, ExportOptions(max_markdown_bytes=1)),
        ("ok", {"a.svg": SVG, "b.svg": SVG}, ExportOptions(max_assets=1)),
        ("ok", {"a.svg": SVG}, ExportOptions(max_asset_bytes=1)),
        ("ok", {"../a.svg": SVG}, ExportOptions()),
        ("ok", {"a.png": SVG}, ExportOptions()),
        ("ok", {"a.svg": b"not SVG"}, ExportOptions()),
        ("ok", {"a.svg": "not bytes"}, ExportOptions()),
    ],
)
def test_invalid_input_refused_before_acquisition(markdown, assets, options):
    _, backend, routing = setup()
    with pytest.raises(ValueError):
        asyncio.run(export_pdf(markdown, assets, options, **routing))
    assert not backend.keys


def test_returns_verified_bytes_and_disposes_fresh_calls():
    renderer, backend, routing = setup()

    async def run():
        for _ in range(2):
            assert await export_pdf("# Report", {"a.svg": SVG}, **routing) == PDF
            backend._handed_out = False
            backend.sandbox = Renderer()

    asyncio.run(run())
    assert len(backend.disposed) == 2
    keys = backend.keys
    assert len({key.call_id for key in keys}) == 2
    assert all(key.scope == "host-scope" for key in keys)
    assert renderer.requests[0]["markdown"] == "# Report"


@pytest.mark.parametrize(
    "mutation",
    [
        {"pages": 101},
        {"pages": True},
        {"sha256": "wrong"},
        {"bytes": 0},
        {"network_requests": ["https://example.invalid"]},
        {"schema_version": 2},
    ],
)
def test_output_validation_still_disposes(mutation):
    _, backend, routing = setup(Renderer(mutation=mutation))
    with pytest.raises(ValueError):
        asyncio.run(export_pdf("report", {}, **routing))
    assert backend.disposed


@pytest.mark.parametrize("exit_code,error", [(2, ValueError), (1, RuntimeError)])
def test_guest_refusal_or_failure_still_disposes(exit_code, error):
    _, backend, routing = setup(Renderer(exit_code=exit_code))
    with pytest.raises(error):
        asyncio.run(export_pdf("report", {}, **routing))
    assert backend.disposed


def test_timeout_disposes_without_returning_partial_bytes():
    _, backend, routing = setup(Renderer(wait=True))
    with pytest.raises(TimeoutError):
        asyncio.run(export_pdf("report", {}, ExportOptions(deadline_seconds=0.05), **routing))
    assert backend.disposed


def test_cancellation_disposes():
    async def run():
        renderer, backend, routing = setup(Renderer(wait=True))
        task = asyncio.create_task(export_pdf("report", {}, **routing))
        await renderer.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.disposed

    asyncio.run(run())


def test_failed_disposal_prevents_byte_delivery():
    _, backend, routing = setup()
    backend.dispose_failure = DisposalFailure("unknown", "could not dispose")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        asyncio.run(export_pdf("report", {}, **routing))
    assert backend.disposed


def test_failed_acquisition_is_swept():
    _, backend, routing = setup()
    backend.acquire_error = RuntimeError("create failed")
    with pytest.raises(RuntimeError, match="create failed"):
        asyncio.run(export_pdf("report", {}, **routing))
    assert backend.disposed
