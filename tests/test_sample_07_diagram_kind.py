"""Sample 07's kind, driven against the in-process backend with no container and no model.

These tests check path selection, artifact delivery and disposal through a simulated renderer.
They do not prove confinement of real Graphviz processes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shlex
import struct
import sys
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    Isolation,
    SandboxRouter,
    make_file_system_sink,
)
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InProcessSandbox,
    InProcessSandboxBackend,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from maf_sandbox import ExecResult

_SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "07_docker_diagram"
sys.path.insert(0, str(_SAMPLE))

from diagram_kind import diagram_sandbox_spec, make_diagram_tools  # noqa: E402

#: The kind's own working directory, as its spec states it. Read from the spec rather than
#: transcribed, so a sample that moves it does not leave this suite asserting the old one.
_WORK_DIR = diagram_sandbox_spec().work_dir

_IMAGE = "diagram-sandbox:test"
_DOT = "digraph { ingest -> transform -> load }"


def _png(width: int, height: int) -> bytes:
    """A real PNG header chunk, so what lands is readable rather than a marker string."""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


class _Renderer(InProcessSandbox):
    """An in-process sandbox where ``dot -Tpng <source> -o <output>`` really produces a file.

    It refuses a source that is not in the store, the way ``dot`` refuses one that is not on
    disk — without that, a kind that never wrote its source, or wrote it somewhere else, still
    collects an image and every test here stays green.
    """

    image_bytes = _png(24, 16)

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        if isinstance(command, str) or list(command[:1]) != ["dot"]:
            return result
        argv = list(command)
        cwd = self._working_directory(working_directory)
        source = f"{cwd}/{argv[argv.index('-Tpng') + 1]}"
        if source not in self.contents:
            return ExecResult(stdout="", stderr=f"dot: can't open {source}", exit_code=2)
        self.contents[f"{cwd}/{argv[argv.index('-o') + 1]}"] = self.image_bytes
        return result


def _fn(tool):
    """The raw coroutine behind a MAF tool object, so a test drives it without a model."""
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


_BACKENDS: dict[InProcessSandbox, InProcessSandboxBackend] = {}


def _guest_call_directories(sandbox: InProcessSandbox) -> list[str]:
    return [
        f"{cwd}/{shlex.split(command)[2]}".rsplit("/", 1)[0]
        for command, cwd, _ in sandbox.commands
        if command.startswith("dot ")
    ]


def _tools(
    sandbox: InProcessSandbox,
    out_dir: Path,
    **kwargs,
):
    """The sample's own factory and replacement policy, with an in-process backend."""
    backend = InProcessSandboxBackend(
        sandbox,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
        ),
    )
    _BACKENDS[sandbox] = backend
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    return make_diagram_tools(
        router,
        "diagram-designer",
        make_caller_context(list_no_files, lambda: "samples", lambda: "07-test"),
        make_file_system_sink(out_dir, existing="replace"),
        image=_IMAGE,
        **kwargs,
    )


def _render(sandbox: InProcessSandbox, out_dir: Path, dot: str = _DOT, **kwargs) -> str:
    """One `render_diagram` call, start to finish, and what it told the model."""
    tools = _tools(sandbox, out_dir, **kwargs)
    assert len(tools) == 1, tools
    return asyncio.run(_fn(tools[0])(dot=dot))


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "out"


class TestTheSpecSaysItLandsSomethingItCannotName:
    """The attach-time half: less specific than a literal path, and not weaker."""

    def test_it_admits_call_time_names(self):
        assert diagram_sandbox_spec().outputs_named_at_call_time is True

    def test_it_declares_no_fixed_output(self):
        """A leftover fixed declaration would be collected on every call, from a path no call
        writes — a `required=False` output that is silently never there."""
        assert diagram_sandbox_spec().declared_outputs == ()

    def test_it_still_requires_the_pull_surface(self):
        """`sandboxed_tool` refuses a spec that lands anything without this, so a backend
        without a pull surface is refused at attach rather than inside the sandbox."""
        assert Capability.FILES_OUT in diagram_sandbox_spec().requires


class TestTheToolDeclaresItsResultUntrusted:
    """`additional_properties` is a policy contract, and this tool's states its integrity.

    A declared `source_integrity` *replaces* the framework's input-label join rather than
    flooring it, so `"trusted"` here would tell a host's middleware to disregard that the
    result derives from the model's own DOT. `"untrusted"` is that same replacement used the
    safe way round, and it is what keeps the answer out of the host's `default_integrity`.
    `docs/sandbox/information-flow.md` is the rule, and both packaged kinds carry the same
    assertion.
    """

    def _properties(self, out_dir: Path) -> dict[str, object]:
        tools = _tools(_Renderer(), out_dir)
        assert len(tools) == 1, tools
        return dict(tools[0].additional_properties or {})

    def test_it_declares_untrusted(self, out_dir: Path):
        """The library default is `None`, so the key is here because it was passed."""
        assert self._properties(out_dir)["source_integrity"] == "untrusted"

    def test_it_declares_that_and_nothing_else(self, out_dir: Path):
        """An added confidentiality cap would gate host calls, so nothing else belongs here."""
        assert self._properties(out_dir) == {"source_integrity": "untrusted"}


class TestTheCallWritesInsideItsOwnDirectory:
    def test_the_renderer_was_given_paths_below_the_work_directory(self, out_dir: Path):
        """The `dot` command names both files, so the argv is where the choice is visible —
        `work_dir/diagram.dot` would be the fixed path this sample used to write."""
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        rendered = [command for command, _, _ in sandbox.commands if command.startswith("dot ")]
        assert len(rendered) == 1
        assert f"{_WORK_DIR}/diagram.dot" not in rendered[0]
        assert f"{_WORK_DIR}/diagram.png" not in rendered[0]

    def test_both_files_sit_under_one_directory_below_the_work_directory(self, out_dir: Path):
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        [guest_call_directory] = _guest_call_directories(sandbox)
        assert guest_call_directory.startswith(f"{_WORK_DIR}/")
        assert guest_call_directory.count("/") == _WORK_DIR.count("/") + 1

    def test_two_concurrent_calls_never_share_a_path(self, out_dir: Path):
        """Calls launched together still select different guest paths."""
        sandbox = _Renderer()
        tools = _tools(sandbox, out_dir)
        assert len(tools) == 1, tools
        render = _fn(tools[0])

        async def both() -> None:
            await asyncio.gather(render(dot=_DOT), render(dot=_DOT))

        asyncio.run(both())

        rendered = [command for command, _, _ in sandbox.commands if command.startswith("dot ")]
        assert len(rendered) == 2
        assert rendered[0] != rendered[1]

        guest_first, guest_second = _guest_call_directories(sandbox)
        assert guest_first != guest_second
        assert not guest_first.startswith(f"{guest_second}/")
        assert not guest_second.startswith(f"{guest_first}/")


class TestTheCallIsDisposed:
    def test_unproved_confinement_is_not_declared(self):
        assert diagram_sandbox_spec().confined_to_guest_call_path is False

    def test_the_backend_is_asked_to_dispose_the_kind(self, out_dir: Path):
        sandbox = _Renderer()
        _render(sandbox, out_dir)
        backend = _BACKENDS[sandbox]
        assert len(backend.disposed) == 2
        assert backend.disposed_kinds == [diagram_sandbox_spec().kind] * 2
        assert not sandbox.reclaims


class TestTheArtifactLandsUnderTheNameTheSampleChose:
    def test_it_lands_as_diagram_png(self, out_dir: Path):
        """`name` on the call-time declaration is what keeps the run id out of host storage."""
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        assert sorted(path.name for path in out_dir.iterdir()) == ["diagram.png"]

    def test_what_landed_is_the_rendered_bytes(self, out_dir: Path):
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        assert (out_dir / "diagram.png").read_bytes() == _png(24, 16)

    def test_a_second_render_replaces_the_first_image(self, out_dir: Path):
        """The sample keeps one landed name and the latest render's bytes."""
        sandbox = _Renderer()
        [tool] = _tools(sandbox, out_dir)
        render = _fn(tool)

        async def twice() -> None:
            assert "diagram.png" in await render(dot=_DOT)
            assert (out_dir / "diagram.png").read_bytes() == _png(24, 16)
            sandbox.image_bytes = _png(48, 32)
            reply = await render(dot="digraph { load -> report }")
            assert "diagram.png" in reply
            assert "Error:" not in reply

        asyncio.run(twice())
        assert (out_dir / "diagram.png").read_bytes() == _png(48, 32)
        assert sorted(path.name for path in out_dir.iterdir()) == ["diagram.png"]

    def test_the_model_is_told_where_it_went_and_not_what_it_is(self, out_dir: Path):
        """The sink's one line, and no run id in it."""
        sandbox = _Renderer()
        reply = _render(sandbox, out_dir)

        [guest_call_directory] = _guest_call_directories(sandbox)
        run_id = guest_call_directory.rsplit("/", 1)[-1]
        assert "diagram.png" in reply
        assert run_id not in reply
