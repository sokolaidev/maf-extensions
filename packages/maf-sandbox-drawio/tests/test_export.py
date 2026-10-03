"""Export policy, exact artifact declarations and authorized stored-file reads."""

import asyncio
import dataclasses
import json

import pytest
from maf_sandbox import (
    Artifact,
    Capability,
    ExecResult,
    Isolation,
    IsolationScope,
    LandedArtifact,
    ListedFile,
    OsFamily,
    OutputSink,
    SandboxRouter,
    make_file_system_sink,
)
from maf_sandbox.maf import make_caller_context
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend
from test_tool import attach, completed, items, said, verdict

from maf_sandbox_drawio import (
    DrawioExport,
    drawio_sandbox_spec,
    make_drawio_export_tools,
    make_drawio_tools,
)
from maf_sandbox_drawio._tool import _export_outputs


@pytest.mark.parametrize(
    "options",
    [
        {"formats": ()},
        {"formats": ("pdf",)},
        {"formats": ("png", "png")},
        {"formats": ["png"]},
        {"pages": ()},
        {"pages": (0,)},
        {"pages": (True,)},
        {"pages": (9,)},
        {"pages": (1, 1)},
        {"scale": float("nan")},
        {"scale": True},
        {"scale": 0},
        {"scale": 5},
        {"jpeg_quality": 0},
        {"jpeg_quality": True},
        {"transparent": 1},
    ],
)
def test_invalid_configuration(options):
    with pytest.raises((TypeError, ValueError)):
        DrawioExport(**options)


def test_spec_retains_closed_egress_and_expands_only_opt_in_limits():
    ordinary = drawio_sandbox_spec()
    exported = drawio_sandbox_spec(export=DrawioExport())
    assert ordinary.egress == exported.egress == "closed"
    assert ordinary.files_out.max_files == 1
    assert exported.files_out.max_files == 25
    assert ordinary.requires == exported.requires
    assert ordinary.isolation_scope is IsolationScope.CONVERSATION
    assert exported.isolation_scope is IsolationScope.CALL


def test_manifest_must_match_exact_requested_pages_and_formats():
    config = DrawioExport(formats=("jpg", "svg"), pages=(2,))
    data = json.dumps({"pages": 3, "files": ["diagram-2.jpg", "diagram-2.svg"]}).encode()
    outputs = _export_outputs(data, config, "call")
    assert [item.media_type for item in outputs] == ["image/jpeg", "image/svg+xml"]
    assert outputs[0].path == "call/diagram-2.jpg"
    for invalid in (
        {"pages": True, "files": []},
        {"pages": 1, "files": ["diagram-2.jpg"]},
        {"pages": 3, "files": ["../../secret", "diagram-2.svg"]},
        {"pages": 3, "files": ["diagram-2.jpg", "diagram-2.svg"], "extra": 1},
        {"pages": 3, "files": ["diagram-2.jpg"]},
        [],
    ):
        with pytest.raises(ValueError):
            _export_outputs(json.dumps(invalid).encode(), config, "call")


def test_unlisted_reference_is_not_read_or_acquired(tmp_path):
    class Store:
        async def read(self, name):
            pytest.fail("An unlisted file must not be read")

    async def listing(store):
        return [ListedFile("allowed.drawio", None)]

    backend = InProcessSandboxBackend(
        InProcessSandbox(),
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset(IsolationScope),
        ),
        sandbox_per_key=True,
    )
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    [tool] = make_drawio_export_tools(
        router,
        "test",
        make_caller_context(listing, lambda: "scope", lambda: "thread"),
        make_file_system_sink(tmp_path),
        Store(),
        export=DrawioExport(),
    )
    answer = asyncio.run(tool.func(file="../allowed.drawio"))
    assert any("exactly one visible" in (item.text or "") for item in answer)
    assert not backend.keys


class ExportSandbox(InProcessSandbox):
    missing = False

    async def exec(self, command, *, working_directory, timeout):
        await super().exec(command, working_directory=working_directory, timeout=timeout)
        options = json.loads(
            await self.read_file("export.json", working_directory=working_directory, max_bytes=4096)
        )
        assert options["formats"] == ["png", "jpg", "svg"]
        payloads = {
            "diagram.drawio": b"<mxfile/>",
            "diagram-1.png": b"\x89PNG\r\n\x1a\n\x00\xff",
            "diagram-1.jpg": b"\xff\xd8\x00\xff\xd9",
            "diagram-1.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>',
            "exports.json": json.dumps(
                {"pages": 1, "files": ["diagram-1.png", "diagram-1.jpg", "diagram-1.svg"]}
            ).encode(),
        }
        for name, data in payloads.items():
            if self.missing and name == "diagram-1.svg":
                continue
            self.contents[f"{self._working_directory(working_directory)}/{name}"] = data
        return ExecResult(stdout="", stderr="", exit_code=0)


def test_binary_export_delivery_and_labels(tmp_path):
    delivered = []

    async def deliver(artifact: Artifact):
        delivered.append(artifact)
        return LandedArtifact(name=artifact.name, display=artifact.name)

    tool, backend = attach(
        ExportSandbox(),
        tmp_path,
        sink=OutputSink(deliver),
        export=DrawioExport(formats=("png", "jpg", "svg")),
    )
    answer = items(tool)
    assert completed(answer) and verdict(answer) == "created"
    assert [a.media_type for a in delivered] == [
        "application/xml",
        "image/png",
        "image/jpeg",
        "image/svg+xml",
    ]
    assert delivered[1].content.endswith(b"\x00\xff")
    assert all(
        item.additional_properties["security_label"]["integrity"] == "untrusted"
        for item in answer[2:]
    )
    assert backend.disposed


def test_missing_export_prevents_all_sink_delivery(tmp_path):
    sandbox = ExportSandbox()
    sandbox.missing = True
    tool, backend = attach(
        sandbox, tmp_path / "outputs", export=DrawioExport(formats=("png", "jpg", "svg"))
    )
    answer = items(tool)
    assert not completed(answer)
    assert "collection or delivery of draw.io exports failed" in said(answer)
    assert not (tmp_path / "outputs").exists()
    assert backend.disposed


def test_partial_sink_failure_is_incomplete(tmp_path):
    delivered = []

    async def deliver(artifact: Artifact):
        if delivered:
            raise OSError("Sink unavailable")
        delivered.append(artifact.name)
        return LandedArtifact(name=artifact.name, display=artifact.name)

    tool, backend = attach(
        ExportSandbox(),
        tmp_path,
        sink=OutputSink(deliver),
        export=DrawioExport(formats=("png", "jpg", "svg")),
    )
    answer = items(tool)
    assert not completed(answer)
    assert "collection or delivery of draw.io exports failed" in said(answer)
    assert delivered == ["diagram.drawio"]
    assert backend.disposed


@pytest.mark.parametrize("manifest", [None, b"not json", b'{"pages":1,"files":[]}'])
def test_bad_export_manifest_reports_export_collection_failure(tmp_path, manifest):
    class BadManifest(ExportSandbox):
        async def exec(self, command, *, working_directory, timeout):
            result = await super().exec(
                command, working_directory=working_directory, timeout=timeout
            )
            path = f"{self._working_directory(working_directory)}/exports.json"
            if manifest is None:
                del self.contents[path]
            else:
                self.contents[path] = manifest
            return result

    tool, backend = attach(
        BadManifest(), tmp_path / "outputs", export=DrawioExport(formats=("png", "jpg", "svg"))
    )
    answer = items(tool)
    assert not completed(answer)
    assert "collection or delivery of draw.io exports failed" in said(answer)
    assert not (tmp_path / "outputs").exists()
    assert backend.disposed


def test_stored_export_reads_exact_reference_without_model_xml(tmp_path):
    source = '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>'
    reads = []

    class Store:
        async def read(self, name):
            reads.append(name)
            return source

    async def listing(store):
        return [ListedFile("saved diagram.drawio", None)]

    sandbox = ExportSandbox()
    backend = InProcessSandboxBackend(
        sandbox,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset(IsolationScope),
        ),
        sandbox_per_key=True,
    )
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    [tool] = make_drawio_export_tools(
        router,
        "test",
        make_caller_context(listing, lambda: "scope", lambda: "thread"),
        make_file_system_sink(tmp_path),
        Store(),
        export=DrawioExport(formats=("png", "jpg", "svg")),
    )
    answer = asyncio.run(tool.func(file="saved diagram.drawio"))
    assert completed(answer) and verdict(answer) == "created"
    assert reads == ["saved diagram.drawio"]
    command, directory, _ = sandbox.commands[0]
    assert "--require-layout" in command
    assert sandbox.contents[f"{directory}/input.xml"] == source.encode()
    assert backend.disposed


@pytest.mark.parametrize("first_export", [False, True])
@pytest.mark.parametrize("stored", [False, True])
def test_overlapping_runtime_profiles_get_their_requested_images(tmp_path, first_export, stored):
    async def run():
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        config = DrawioExport(formats=("png", "jpg", "svg"))

        class ProfileSandbox(ExportSandbox):
            def __init__(self, image):
                super().__init__()
                self.image = image

            async def exec(self, command, *, working_directory, timeout):
                xml = await self.read_file(
                    "input.xml", working_directory=working_directory, max_bytes=4096
                )
                second = b"export-b" in xml
                expected = "export-b" if second else "export-a" if first_export else "light"
                if second:
                    second_started.set()
                else:
                    first_started.set()
                    await asyncio.wait_for(second_started.wait(), timeout=5)
                if self.image != expected:
                    return ExecResult(stdout="", stderr="Wrong runtime image", exit_code=3)
                if second or first_export:
                    return await super().exec(
                        command, working_directory=working_directory, timeout=timeout
                    )
                await InProcessSandbox.exec(
                    self, command, working_directory=working_directory, timeout=timeout
                )
                directory = self._working_directory(working_directory)
                self.contents[f"{directory}/diagram.drawio"] = b"<mxfile/>"
                return ExecResult(stdout="", stderr="", exit_code=0)

        class ProfileBackend(InProcessSandboxBackend):
            def __init__(self):
                super().__init__(
                    declarations=dataclasses.replace(
                        FAKE_BACKEND_DECLARATIONS,
                        capabilities=FAKE_BACKEND_DECLARATIONS.capabilities
                        | {Capability.FILES_OUT},
                        os_families=frozenset({OsFamily.POSIX}),
                        isolation_scopes=frozenset(IsolationScope),
                    )
                )
                self.profiles = {}

            async def acquire(self, key, spec):
                identity = (key, spec.kind)
                if identity not in self.profiles:
                    self.profiles[identity] = ProfileSandbox(spec.image)
                self.sandbox = self.profiles[identity]
                return await super().acquire(key, spec)

        class Store:
            async def read(self, name):
                return '<mxfile name="export-b"/>'

        async def listing(store):
            return [ListedFile("saved.drawio", None)]

        backend = ProfileBackend()
        router = SandboxRouter([backend], min_isolation=Isolation.NONE)
        context = make_caller_context(listing, lambda: "scope", lambda: "thread")
        [first] = make_drawio_tools(
            router,
            "test",
            context,
            make_file_system_sink(tmp_path / "first"),
            image="export-a" if first_export else "light",
            export=config if first_export else None,
        )
        args = (router, "test", context, make_file_system_sink(tmp_path / "second"))
        [second] = (
            make_drawio_export_tools(*args, Store(), image="export-b", export=config)
            if stored
            else make_drawio_tools(*args, image="export-b", export=config)
        )
        task = asyncio.create_task(first.func(xml='<mxfile name="first"/>'))
        await asyncio.wait_for(first_started.wait(), timeout=5)
        try:
            answer = (
                await second.func(file="saved.drawio")
                if stored
                else await second.func(xml='<mxfile name="export-b"/>')
            )
        finally:
            second_started.set()
        initial = await task
        assert completed(initial) and verdict(initial) == "created"
        assert completed(answer) and verdict(answer) == "created"
        assert len(set(backend.keys)) == 2
        assert set(backend.disposed) == set(backend.keys)

    asyncio.run(run())
