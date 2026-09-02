"""Live tests: the CodeAct kind against a real container running real Python.

Skipped unless the ``docker`` client is on ``PATH`` and ``MAF_SANDBOX_CODEACT_E2E_IMAGE`` names
an image with a ``python3`` in it. Both are set by the ``docker-e2e`` job in ``tests.yml``, so
this runs on **every pull request** — no model, no tokens, no billable anything. The tool body
is called directly, exactly as ``test_codeact_workload.py`` calls it offline.

**Why it exists (#397).** Until this file, every live exercise this kind ever got was driven by
a model: samples 03, 04, 06, 08 and 14. A sample proves the happy path a model happened to
produce, and most of the collection machinery here is not that path. `CodeactOutputs.MANIFEST`
in particular had never run anywhere but a fake — both file-channel samples take `DECLARED` and
say in prose why — so the mode was documented by two samples that decline to use it.

The offline suite is thorough and cannot disagree with itself: its sandbox answers what this
package believes about a guest. That is the gap. On the neighbouring backend the two bugs that
mattered (#139, #142) were both the package believing wrong.

**What a real guest adds over the fake**, concretely: the program is Python that actually runs,
so the manifest is bytes a real interpreter serialised into a file the collection then has to
find, weigh against `files_out`, and read back through the backend's own stat-and-read. The
fake supplies all four of those from a dict.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from typing import Any

import pytest
from maf_sandbox import (
    Artifact,
    CallerContext,
    Isolation,
    LandedArtifact,
    OutputSink,
    SandboxRouter,
    TransferLimits,
)

pytest.importorskip(
    "maf_sandbox_docker",
    exc_type=ImportError,
    reason="the e2e drives a real docker backend; the sibling wheel may be absent, or paired "
    "with a core below its own floor by the published-cores gate's --no-deps pass",
)

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig  # noqa: E402

from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools

_IMAGE = os.environ.get("MAF_SANDBOX_CODEACT_E2E_IMAGE")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or not _IMAGE,
    reason=(
        "needs the docker client on PATH and MAF_SANDBOX_CODEACT_E2E_IMAGE naming an image with "
        "python3 in it"
    ),
)


class _RecordingSink:
    """A host sink that records what it was handed, as the offline suite's does."""

    def __init__(self) -> None:
        self.delivered: list[Artifact] = []
        self.sink = OutputSink(deliver=self.deliver)

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        self.delivered.append(artifact)
        return LandedArtifact(name=artifact.name, display=f"saved {artifact.name}")

    @property
    def names(self) -> list[str]:
        return [artifact.name for artifact in self.delivered]

    @property
    def contents(self) -> dict[str, bytes]:
        return {artifact.name: artifact.content for artifact in self.delivered}


def _context(thread_id: str) -> CallerContext:
    return CallerContext(
        current_scope=lambda: "e2e",
        current_thread_id=lambda: thread_id,
        list_files=lambda: (),
    )


def _callable(tool):
    """The tool body, off whichever attribute the MAF decorator carries it on."""
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


def _items_in_a_container(
    code: str,
    *,
    mode: CodeactOutputs,
    sink: _RecordingSink,
    files_out: TransferLimits | None = None,
    withhold: bool = False,
    **call_kwargs: Any,
):
    """Build the tool over a real Docker backend, run ``code`` in it, and dispose.

    One container per call. They are free — the whole reason this suite can run on a pull
    request — and a shared one would let a program see what an earlier test wrote.
    """
    thread_id = f"thread-{uuid.uuid4()}"
    backend = DockerSandboxBackend(DockerSandboxConfig())
    extra: dict[str, Any] = {} if files_out is None else {"files_out": files_out}
    tools = make_codeact_tools(
        SandboxRouter([backend], min_isolation=backend.isolation),
        "data-analyst",
        _context(thread_id),
        image=_IMAGE,
        outputs=mode,
        output_sink=sink.sink,
        withhold_guest_output=withhold,
        **extra,
    )
    assert len(tools) == 1, f"expected one tool, got {len(tools)}"

    async def scenario():
        try:
            return await _callable(tools[0])(code=code, **call_kwargs)
        finally:
            await backend.dispose_scope("e2e", thread_id)

    return asyncio.run(scenario())


def _run_in_a_container(code: str, **kw: Any) -> str:
    """The run's own half of the answer, which is the whole of it unless the host withholds.

    A withholding tool answers with items: the run's text carrying no label, beside the
    standing route sentence labelled trusted. Returning the first keeps every test about what
    a run *reported* reading the same in both modes, exactly as the offline suite's `_run`
    does — `_items_in_a_container` is for the tests about the split itself.
    """
    answer = _items_in_a_container(code, **kw)
    return answer if isinstance(answer, str) else str(answer[0].text)


def test_the_backend_meets_the_floor_this_suite_assumes():
    """A premise, not a behaviour: every test below wires the router at the backend's own floor.

    Stated once here so a change to the isolation ladder fails with this sentence rather than
    as eight confusing refusals at attach.
    """
    assert DockerSandboxBackend(DockerSandboxConfig()).isolation is Isolation.CONTAINER


class TestManifestAgainstARealInterpreter:
    """The mode no sample uses and no live run had ever reached."""

    def test_a_program_that_names_its_own_outputs_lands_them(self):
        """The case the mode exists for: names knowable only after reading the input.

        The host asserts on names it never supplied — the program derives them from data it
        reads at run time — which is a stronger claim than a declared-outputs run can make,
        where the names came from the call.
        """
        sink = _RecordingSink()
        code = """
import csv, json, io

DATA = "region,amount\\nnorth,10\\nsouth,20\\nnorth,5\\n"
rows = list(csv.DictReader(io.StringIO(DATA)))

totals = {}
for row in rows:
    totals[row["region"]] = totals.get(row["region"], 0) + int(row["amount"])

produced = []
for region, total in sorted(totals.items()):
    name = region + ".txt"
    with open(name, "w", encoding="utf-8") as handle:
        handle.write(str(total))
    produced.append({"path": name})

with open("outputs.json", "w", encoding="utf-8") as handle:
    json.dump({"outputs": produced}, handle)

print("regions:", ",".join(sorted(totals)))
"""
        answer = _run_in_a_container(code, mode=CodeactOutputs.MANIFEST, sink=sink)

        assert "regions: north,south" in answer
        # The program chose these names from the data. Nothing in this test told it either one.
        assert sorted(sink.names) == ["north.txt", "south.txt"]
        assert sink.contents["north.txt"] == b"15"
        assert sink.contents["south.txt"] == b"20"
        # The manifest is machinery, not an artifact: it is read and must not be delivered.
        assert "outputs.json" not in sink.names

    def test_a_program_that_writes_no_manifest_saves_nothing_and_says_so(self):
        sink = _RecordingSink()
        code = """
with open("orphan.txt", "w", encoding="utf-8") as handle:
    handle.write("written but never listed")
print("done")
"""
        answer = _run_in_a_container(code, mode=CodeactOutputs.MANIFEST, sink=sink)

        assert sink.names == []
        assert "outputs.json" in answer, answer
        # The file exists in the guest and is simply not collected — the model has to be told
        # that, or it reads the empty result as the program having failed.
        assert "done" in answer

    def test_an_empty_manifest_is_reported_rather_than_passing_silently(self):
        sink = _RecordingSink()
        code = """
import json
with open("outputs.json", "w", encoding="utf-8") as handle:
    json.dump({"outputs": []}, handle)
print("nothing to save")
"""
        answer = _run_in_a_container(code, mode=CodeactOutputs.MANIFEST, sink=sink)

        assert sink.names == []
        assert "no files" in answer.lower() or "nothing was saved" in answer.lower(), answer

    def test_a_manifest_naming_a_file_that_was_never_written(self):
        """The guest's own claim, unchecked until collection tries to read it."""
        sink = _RecordingSink()
        code = """
import json
with open("real.txt", "w", encoding="utf-8") as handle:
    handle.write("this one exists")
with open("outputs.json", "w", encoding="utf-8") as handle:
    json.dump({"outputs": [{"path": "real.txt"}, {"path": "ghost.txt"}]}, handle)
print("listed two, wrote one")
"""
        answer = _run_in_a_container(code, mode=CodeactOutputs.MANIFEST, sink=sink)

        assert "real.txt" in sink.names
        assert "ghost.txt" not in sink.names
        assert "ghost.txt" in answer, answer

    #: Two artifacts and a manifest. Whether both land is the whole question below.
    _TWO_FILES_AND_A_MANIFEST = """
import json
for name in ("a.txt", "b.txt"):
    with open(name, "w", encoding="utf-8") as handle:
        handle.write(name)
with open("outputs.json", "w", encoding="utf-8") as handle:
    json.dump({"outputs": [{"path": "a.txt"}, {"path": "b.txt"}]}, handle)
print("wrote two")
"""

    def _with_slots(self, slots: int) -> tuple[_RecordingSink, str]:
        sink = _RecordingSink()
        answer = _run_in_a_container(
            self._TWO_FILES_AND_A_MANIFEST,
            mode=CodeactOutputs.MANIFEST,
            sink=sink,
            files_out=TransferLimits(
                max_bytes_per_file=1 << 20, max_total_bytes=1 << 20, max_files=slots
            ),
        )
        return sink, answer

    def test_two_artifacts_need_three_slots_because_the_manifest_takes_one(self):
        """The off-by-one a host reading `max_files` as "artifacts I allow" would make.

        Asserted as a *pair*, because three slots landing two artifacts proves nothing on its
        own — that passes whether or not the manifest is counted. Two slots is the case that
        discriminates: enough for both artifacts, not enough for both plus the manifest. It was
        written the vacuous way first, and measuring is what caught it.
        """
        squeezed, refusal = self._with_slots(2)
        assert squeezed.names == [], refusal
        assert "at most 1 per call" in refusal, refusal

        roomy, answer = self._with_slots(3)
        assert sorted(roomy.names) == ["a.txt", "b.txt"], answer


class TestDeclaredAgainstARealInterpreter:
    """The half MANIFEST cannot do, which no live run had watched either."""

    def test_a_declared_output_never_written_is_reported_by_name(self):
        """The diagnostic samples 08 and 14 exist to demonstrate, finally measured.

        Under MANIFEST the names are the guest's and settled after the fact, so nothing can be
        promised and missed. Here the model named it up front, so a file that never arrives is
        a fact the host can state — and this is the first time a real program has failed to
        write one.
        """
        sink = _RecordingSink()
        code = """
with open("present.txt", "w", encoding="utf-8") as handle:
    handle.write("here")
print("wrote one of the two")
"""
        answer = _run_in_a_container(
            code,
            mode=CodeactOutputs.DECLARED,
            sink=sink,
            outputs=["present.txt", "absent.txt"],
        )

        assert sink.names == ["present.txt"]
        assert "absent.txt" in answer, answer


class TestWithheldOutputAgainstARealInterpreter:
    """`withhold_guest_output` against a real guest, because the fake cannot disagree with it.

    Offline, the streams and the exit code are supplied from a dict by the same package that
    renders them. Here a real interpreter writes them, the backend decodes them, and the sizes
    are of whatever that produced — which is the one part of this mode's contract no in-process
    fake can put under strain.
    """

    #: Printed by the programs below, and asserted absent. A distinctive string so its arrival
    #: anywhere in a result is unambiguous rather than a substring coincidence.
    _SECRET = "THE-SECRET-IS-42"

    def test_a_real_programs_text_does_not_come_back(self):
        sink = _RecordingSink()
        answer = _run_in_a_container(
            "import sys\n"
            f"print('{self._SECRET}')\n"
            "print('to stderr', file=sys.stderr)\n"
            "open('answer.txt', 'w').write('12 resources')\n",
            mode=CodeactOutputs.DECLARED,
            sink=sink,
            withhold=True,
            outputs=["answer.txt"],
        )

        assert self._SECRET not in answer, answer
        assert "to stderr" not in answer, answer

    def test_the_same_program_shown_does_return_its_text(self):
        """The control. Without it, the absence above could mean the program never ran."""
        sink = _RecordingSink()
        answer = _run_in_a_container(
            "import sys\n"
            f"print('{self._SECRET}')\n"
            "print('to stderr', file=sys.stderr)\n"
            "open('answer.txt', 'w').write('12 resources')\n",
            mode=CodeactOutputs.DECLARED,
            sink=sink,
            outputs=["answer.txt"],
        )

        assert self._SECRET in answer, answer
        assert "to stderr" in answer, answer

    def test_the_shape_carries_the_exit_code_and_the_real_sizes(self):
        """The sizes are of what the backend actually decoded, which is the number the offline
        suite takes on trust."""
        sink = _RecordingSink()
        answer = _run_in_a_container(
            f"import sys\nprint('{self._SECRET}')\nprint('to stderr', file=sys.stderr)\n",
            mode=CodeactOutputs.DECLARED,
            sink=sink,
            withhold=True,
            outputs=[],
        )

        assert "exit code: 0" in answer, answer
        assert f"stdout: {len(self._SECRET) + 1} bytes" in answer, answer
        assert "stderr: 10 bytes" in answer, answer

    def test_a_declared_file_lands_and_is_named_by_the_declared_spelling(self):
        sink = _RecordingSink()
        answer = _run_in_a_container(
            "open('answer.txt', 'w').write('12 resources')\n",
            mode=CodeactOutputs.DECLARED,
            sink=sink,
            withhold=True,
            outputs=["answer.txt"],
        )

        assert sink.names == ["answer.txt"]
        assert sink.contents["answer.txt"] == b"12 resources"
        assert "- answer.txt" in answer, answer
        assert "saved answer.txt" not in answer, "the sink's display reached a withheld result"

    def test_a_failing_program_still_lands_what_it_wrote(self):
        """The collection rule this mode inverts, against a real non-zero exit: the shown path
        skips collection there, and withheld it is the only channel the model has left."""
        sink = _RecordingSink()
        answer = _run_in_a_container(
            "import sys\n"
            "open('why.txt', 'w').write('ValueError: bad row 7')\n"
            f"print('{self._SECRET}')\n"
            "sys.exit(1)\n",
            mode=CodeactOutputs.DECLARED,
            sink=sink,
            withhold=True,
            outputs=["why.txt"],
        )

        assert "exit code: 1" in answer, answer
        assert sink.names == ["why.txt"]
        assert sink.contents["why.txt"] == b"ValueError: bad row 7"
        assert self._SECRET not in answer, answer

    def test_the_route_is_its_own_trusted_item_against_a_real_run(self):
        """The split, end to end: a real container, a real exit code, and the one item a
        framework hiding untrusted content would leave the model."""
        answer = _items_in_a_container(
            "import sys\nprint('noise')\nsys.exit(1)\n",
            mode=CodeactOutputs.DECLARED,
            sink=_RecordingSink(),
            withhold=True,
            outputs=["why.txt"],
        )

        assert len(answer) == 2, answer
        assert "exit code: 1" in str(answer[0].text)
        assert (answer[0].additional_properties or {}).get("security_label") is None, (
            "the run's half must stay unlabelled, or it replaces the call's confidentiality"
        )
        assert (answer[1].additional_properties or {}).get("security_label") == {
            "integrity": "trusted",
            "confidentiality": "public",
        }

    def test_the_same_failure_shown_lands_nothing(self):
        """The contrast that makes the rule above a decision rather than an accident."""
        sink = _RecordingSink()
        _run_in_a_container(
            "import sys\nopen('why.txt', 'w').write('ValueError: bad row 7')\nsys.exit(1)\n",
            mode=CodeactOutputs.DECLARED,
            sink=sink,
            outputs=["why.txt"],
        )

        assert sink.names == []
