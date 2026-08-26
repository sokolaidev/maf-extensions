"""What sample 07's kind does with the call's own directory — and what it leaves behind.

The sample is a worked example of writing a kind, so the parts a reader would copy are worth
pinning. `test_sample_modules_import.py` proves the module imports; this suite drives the tool
it builds, against the in-process backend, with no container and no model.

Four claims, each of which the sample makes in prose and none of which its live run can show:

* the DOT source and the PNG are written **inside the call's own directory**, so two calls in
  one assistant message share no path — which is why the kind needs no lock;
* the **guest** makes that directory, before anything is written into it. `write_file` would
  create it as root while commands run as the image's `USER`, and a removal needs write
  permission on the directory it is emptying — so a host-created one can never be reclaimed on
  a hardened image (#680);
* the framework removes it when the body returns, so nothing the call wrote is readable by the
  next one — the sample's live check reads the same fact out of the guest, but only for a run
  that reclaimed successfully;
* the artifact lands as `diagram.png` however the guest spelled it, because the call-time
  declaration carries `name`.

The failure the live run cannot reach is staged here instead: a guest that cannot make the
directory. The sample's own image is root-owned, so nothing in a healthy run goes near it.

Async tests follow the repo convention: a synchronous `def test_*` driving one `asyncio.run`
rather than an async marker (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
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
    ExecResult,
    Isolation,
    SandboxRouter,
    make_file_system_sink,
)
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

if TYPE_CHECKING:
    from collections.abc import Sequence

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

    The fake's ``exec`` returns canned stdout and creates nothing, so a collection would have
    nothing to stat. Only ``dot`` is special-cased and only its ``-o`` argument is read;
    everything else — the recording, the reclaim, the store — is the fake's own behaviour.
    """

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        if not isinstance(command, str) and list(command[:1]) == ["dot"]:
            argv = list(command)
            self.contents[argv[argv.index("-o") + 1]] = _png(24, 16)
        return result


class _NoMkdir(_Renderer):
    """A guest that cannot create a directory under its working directory.

    What a hardened image does to a kind: `docker cp` writes as root, commands run as the
    image's ``USER``, and the workload cannot make a directory it would later be asked to
    remove (#680). The kind's own ``mkdir`` is where that is found out.
    """

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        if not isinstance(command, str) and list(command[:1]) == ["mkdir"]:
            self.commands.append((shlex.join(command), working_directory, timeout))
            return ExecResult(
                stdout="", stderr="mkdir: cannot create directory: Permission denied", exit_code=1
            )
        return await super().exec(command, working_directory=working_directory, timeout=timeout)


def _fn(tool):
    """The raw coroutine behind a MAF tool object, so a test drives it without a model."""
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


def _tools(sandbox: InProcessSandbox, out_dir: Path, **kwargs):
    """The sample's own factory, wired to the in-process backend instead of docker."""
    backend = InProcessSandboxBackend(
        sandbox,
        capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
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

    def test_two_calls_never_share_a_path(self, out_dir: Path):
        """The claim the missing lock rests on. Two calls, two directories, neither inside the
        other — so one render cannot overwrite the other's source or collect its image."""
        sandbox = _Renderer()
        _render(sandbox, out_dir)
        _render(sandbox, out_dir)

        first, second = (directory for directory, _, _ in sandbox.reclaims)
        assert first != second
        assert not first.startswith(f"{second}/")
        assert not second.startswith(f"{first}/")


class TestTheGuestMakesTheDirectoryBeforeAnythingIsWrittenIntoIt:
    """The load-bearing line, and the reason it is not left to `write_file`.

    `write_file` creates what it creates as root while commands run as the image's `USER`, and
    a removal needs write permission on the directory it is emptying — so a call directory the
    host created can never be reclaimed on a hardened image (#680).
    """

    def test_the_guest_is_asked_to_make_it(self, out_dir: Path):
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        call_directory, _, _ = sandbox.reclaims[0]
        made = [command for command, _, _ in sandbox.commands if command.startswith("mkdir ")]
        assert made == [f"mkdir -p {call_directory}"]

    def test_it_runs_from_a_directory_that_exists(self, out_dir: Path):
        """`work_dir` may not be there yet (#466), and a missing working directory fails the
        command before it starts — which is why the backend's own reclaim runs from `/` too."""
        sandbox = _Renderer()
        _render(sandbox, out_dir)

        where = [cwd for command, cwd, _ in sandbox.commands if command.startswith("mkdir ")]
        assert where == ["/"]

    def test_a_guest_that_cannot_make_it_is_refused_before_any_work(self, out_dir: Path):
        """The one place a workload can refuse over this. The reclaim runs after the body has
        returned, so by the time a removal is known to have failed the answer is already gone."""
        sandbox = _NoMkdir()
        reply = _render(sandbox, out_dir)

        assert "does not let the workload create" in reply
        assert sandbox.contents == {}, "the refusal must come before anything is written"
        assert not any(command.startswith("dot ") for command, _, _ in sandbox.commands)
        assert not out_dir.exists() or list(out_dir.iterdir()) == []

    def test_a_refused_call_leaves_nothing_for_the_framework_to_reclaim(self, out_dir: Path):
        """Refusing early is also what makes the failure harmless: nothing was written, so the
        directory the framework would have removed does not exist."""
        sandbox = _NoMkdir()
        _render(sandbox, out_dir)

        assert [path for path in sandbox.contents if path.startswith(f"{_WORK_DIR}/")] == []


class TestNothingSurvivesTheCall:
    def test_the_work_directory_is_empty_afterwards(self, out_dir: Path):
        """What the sample's live run reads out of the guest, asserted here without one."""
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
