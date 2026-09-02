"""Sample 07's kind, driven against the in-process backend with no container and no model.

What the sample claims in prose its live run cannot show — the reclaim is visible only in the
fake's own store. Async tests follow the repo convention: a synchronous `def test_*` driving
one `asyncio.run`, no pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import dataclasses
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

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        if isinstance(command, str) or list(command[:1]) != ["dot"]:
            return result
        argv = list(command)
        source = argv[argv.index("-Tpng") + 1]
        if source not in self.contents:
            return ExecResult(stdout="", stderr=f"dot: can't open {source}", exit_code=2)
        self.contents[argv[argv.index("-o") + 1]] = _png(24, 16)
        return result


def _fn(tool):
    """The raw coroutine behind a MAF tool object, so a test drives it without a model."""
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


def _tools(sandbox: InProcessSandbox, out_dir: Path, **kwargs):
    """The sample's own factory, wired to the in-process backend instead of docker."""
    backend = InProcessSandboxBackend(
        sandbox,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS, capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT}
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    return make_diagram_tools(
        router,
        "diagram-designer",
        make_caller_context(list_no_files, lambda: "samples", lambda: "07-test"),
        make_file_system_sink(out_dir),
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


class TestTheToolDeclaresNothingAboutItsResult:
    """`additional_properties` is a policy contract, and this tool's is empty.

    A declared `source_integrity` *replaces* the framework's input-label join rather than
    flooring it, so `"trusted"` here would tell a host's middleware to disregard that the
    result derives from the model's own DOT. `docs/sandbox/information-flow.md` is the rule,
    and both packaged kinds carry the same assertion.
    """

    def _properties(self, out_dir: Path) -> dict[str, object]:
        tools = _tools(_Renderer(), out_dir)
        assert len(tools) == 1, tools
        return dict(tools[0].additional_properties or {})

    def test_it_declares_no_source_integrity(self, out_dir: Path):
        """The library default is `"trusted"`, so an absent key here is a passed argument."""
        assert "source_integrity" not in self._properties(out_dir)

    def test_it_declares_nothing_at_all(self, out_dir: Path):
        """Not only the integrity key. A confidentiality cap added here would start gating
        calls in a host deployment, and nothing in this suite would report it as a failure."""
        assert self._properties(out_dir) == {}


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

        # Read before the reclaim removed them: the fake records every reclaimed directory, and
        # that directory is what the two files were written under.
        assert len(sandbox.reclaims) == 1
        call_directory, working_directory, _ = sandbox.reclaims[0]
        assert working_directory == _WORK_DIR
        assert call_directory.startswith(f"{_WORK_DIR}/")
        assert call_directory.count("/") == _WORK_DIR.count("/") + 1

    def test_two_concurrent_calls_never_share_a_path(self, out_dir: Path):
        """The claim the missing lock rests on, in the shape the lock guarded.

        One attached tool, both calls in flight at once. Sequential calls through separate
        tools would have passed against the locked version too, and prove nothing about it.
        """
        sandbox = _Renderer()
        tools = _tools(sandbox, out_dir)
        assert len(tools) == 1, tools
        render = _fn(tools[0])

        async def both() -> None:
            await asyncio.gather(render(dot=_DOT), render(dot=_DOT))

        asyncio.run(both())

        # The argv is the kind's own choice; the reclaims below are the framework's, and would
        # differ even from a kind that wrote both renders to one fixed path.
        rendered = [command for command, _, _ in sandbox.commands if command.startswith("dot ")]
        assert len(rendered) == 2
        assert rendered[0] != rendered[1]

        first, second = (directory for directory, _, _ in sandbox.reclaims)
        assert first != second
        assert not first.startswith(f"{second}/")
        assert not second.startswith(f"{first}/")


class TestNothingSurvivesTheCall:
    def test_the_work_directory_is_empty_afterwards(self, out_dir: Path):
        """Nothing the call wrote outlives it — which only the fake's store can show."""
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        assert [path for path in sandbox.contents if path.startswith(f"{_WORK_DIR}/")] == []

    def test_the_reclaim_is_the_call_directory_and_not_the_work_directory(self, out_dir: Path):
        """A reclaim of `work_dir` itself is what the library refuses, and it is the mistake a
        kind makes when it writes at fixed paths and tries to tidy up after itself."""
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        # Both halves, because "nothing was reclaimed" satisfies the second on its own — which
        # is exactly what a kind that writes at fixed paths does.
        assert [directory for directory, _, _ in sandbox.reclaims] not in ([], [_WORK_DIR])


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

    def test_the_model_is_told_where_it_went_and_not_what_it_is(self, out_dir: Path):
        """The sink's one line, and no run id in it."""
        sandbox = _Renderer()
        reply = _render(sandbox, out_dir)

        call_directory, _, _ = sandbox.reclaims[0]
        run_id = call_directory.rsplit("/", 1)[-1]
        assert "diagram.png" in reply
        assert run_id not in reply
