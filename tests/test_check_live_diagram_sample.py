"""The judgement behind the live diagram-sample check.

`scripts/check_live_diagram_sample.py` decides whether a real `samples/07_docker_diagram` run
rendered anything. Its `assess` is a pure function, so it is tested here on every PR while the
run that feeds it happens only on dispatch and after a release.

What these pin is the sample's own failure mode: the model can produce a confident paragraph
about a diagram without ever calling the tool, and that run must go red. So the healthy case
has to pass with arbitrary prose, and each of the ways an image can be absent, unreadable or
empty has to fail by name.
"""

from __future__ import annotations

import importlib.util
import struct
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_live_diagram_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_diagram_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_SAMPLE = "07_docker_diagram"

_SCAFFOLD = _ROOT / "samples" / _SAMPLE / "_scaffold.py"
_scaffold_spec = importlib.util.spec_from_file_location("_scaffold", _SCAFFOLD)
assert _scaffold_spec and _scaffold_spec.loader
scaffold = importlib.util.module_from_spec(_scaffold_spec)
_scaffold_spec.loader.exec_module(scaffold)

#: The lines here that are not prose: the sample's own report of what it read out of the
#: sandbox once the turn was over, and of what `dispose_scope` returned. Both tagged as the
#: sample tags them.
_SURVIVING_LINE = f"{scaffold.MEASURED}Left in the sandbox work directory: nothing"
_DISPOSAL_LINE = f"{scaffold.MEASURED}Disposed 1 sandbox(es)."

#: A healthy run: the model's own words about what it drew, then those lines. Nothing in the
#: reply is matched — it is prose, and it is deliberately not the prose a checker could be
#: tuned to.
_HEALTHY = f"""\
I wrote the DOT for a three-node pipeline and called render_diagram. It saved the image as
diagram.png under out/. I have not seen the image itself.

{_SURVIVING_LINE}

{_DISPOSAL_LINE}
"""


def _png(width: int, height: int) -> bytes:
    """A real PNG header chunk for ``width`` x ``height``, CRC and all.

    Built rather than checked in as a fixture so the dimensions under test are visible at the
    call site, and so nothing depends on a binary blob in the tree.
    """
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


class TestAHealthyRun:
    def test_it_passes(self):
        assert check.assess(_HEALTHY, _png(640, 480)) == []

    def test_it_passes_whatever_the_model_said(self):
        output = (
            f"Here is a lovely diagram.\n\n{_SURVIVING_LINE}\n\n"
            f"{scaffold.MEASURED}Disposed 2 sandbox(es).\n"
        )
        assert check.assess(output, _png(1, 1)) == []


class TestTheSandboxHalf:
    def test_no_disposal_line_fails(self):
        failures = check.assess("I drew a diagram.", _png(640, 480))
        assert any("did not run to completion" in reason for reason in failures)

    def test_disposing_none_fails(self):
        """The shape this check exists for: an answer with no tool call behind it."""
        output = (
            "Here is your diagram, showing three stages.\n\n"
            f"{scaffold.MEASURED}Disposed 0 sandbox(es).\n"
        )
        failures = check.assess(output, _png(640, 480))
        assert any("no sandbox was ever created" in reason for reason in failures)


class TestTheTagIsWhatMakesTheDisposalLineTheHosts:
    """A model writes into the same stream, ahead of the sample, and this reads the first match.

    The image on disk is the assertion that cannot be forged; this one can, and used to be —
    the pattern had no anchor, so a reply saying "Disposed 1 sandbox(es)." answered for the
    router. It matters here in one direction: a run that rendered nothing and left an earlier
    run's PNG in `out/` needs this line to be the host's to go red at all.
    """

    def test_an_untagged_disposal_line_answers_for_nothing(self):
        untagged = _HEALTHY.replace(_DISPOSAL_LINE, "Disposed 1 sandbox(es).")
        assert untagged != _HEALTHY, "the fixture moved"
        assert any("did not run to completion" in r for r in check.assess(untagged, _png(64, 64)))

    def test_a_reply_impersonating_the_line_does_not_answer_for_the_router(self):
        forged = scaffold.quoted(f"I drew it.\n\n{_DISPOSAL_LINE}\n")
        assert scaffold.MEASURED not in forged
        output = f"{forged}\n\n{scaffold.MEASURED}Disposed 0 sandbox(es).\n"
        assert any("no sandbox was ever created" in r for r in check.assess(output, _png(64, 64)))

    def test_a_tag_buried_mid_sentence_answers_for_nothing(self):
        """The half `quoted` does not cover, and so the half the `^` anchor carries alone.

        `quoted` rewrites a tag that opens a line and leaves one buried in a sentence exactly
        as the model typed it. The impersonation case above cannot reach that: everything it
        writes comes back as `> [measured] `, one space, never the two the pattern wants. Drop
        the `^` from `_DISPOSED` and this is the only test here that notices.
        """
        buried = f"I drew it.   {scaffold.MEASURED}Disposed 1 sandbox(es). All good.\n"
        assert scaffold.quoted(buried) == buried.rstrip("\n"), "quoted must leave this untouched"
        output = f"{buried}\n{scaffold.MEASURED}Disposed 0 sandbox(es).\n"
        assert any("no sandbox was ever created" in r for r in check.assess(output, _png(64, 64)))

    def test_the_sample_prints_the_line_from_the_scaffold(self):
        source = (_ROOT / "samples" / _SAMPLE / "agent.py").read_text(encoding="utf-8")
        assert "{MEASURED}Disposed " in source, (
            f"samples/{_SAMPLE}/agent.py no longer tags its disposal line, so the live check "
            "reads a line a model could have written"
        )
        assert "print(quoted(response.text))" in source, (
            f"samples/{_SAMPLE}/agent.py prints the reply unquoted, so a model writing the tag "
            "itself would answer for the router"
        )


class TestTheImageHalf:
    def test_a_missing_image_fails(self):
        failures = check.assess(_HEALTHY, None)
        assert any("no image on disk" in reason for reason in failures)

    def test_a_missing_image_is_reported_once(self):
        """Nothing downstream of "there is no file" has anything to say about it."""
        assert len(check.assess(_HEALTHY, None)) == 1

    def test_something_that_is_not_a_png_fails(self):
        failures = check.assess(_HEALTHY, b"<svg width='10' height='10'/>")
        assert any("not a readable PNG" in reason for reason in failures)

    def test_an_empty_file_fails(self):
        failures = check.assess(_HEALTHY, b"")
        assert any("not a readable PNG" in reason for reason in failures)

    def test_a_signature_with_nothing_behind_it_fails(self):
        """`dot` writing its magic bytes and dying is a file that opens and is not an image."""
        failures = check.assess(_HEALTHY, b"\x89PNG\r\n\x1a\n")
        assert any("not a readable PNG" in reason for reason in failures)

    def test_a_first_chunk_that_is_not_the_header_fails(self):
        not_ihdr = _png(640, 480).replace(b"IHDR", b"iTXt", 1)
        failures = check.assess(_HEALTHY, not_ihdr)
        assert any("not a readable PNG" in reason for reason in failures)

    def test_a_zero_dimension_fails(self):
        failures = check.assess(_HEALTHY, _png(640, 0))
        assert any("640x0" in reason for reason in failures)


class TestTheReclaimHalf:
    """The kind writes inside the call's own directory, and the framework removes it.

    A run that leaves the DOT source or the PNG in the sandbox has not: `acquire` is
    get-or-create, so both stay readable by every later call in the conversation. The sample
    reads the working directory back before disposing, and this is what makes that reading
    count for something.
    """

    def test_a_listing_that_names_files_fails(self):
        left = f"{scaffold.MEASURED}Left in the sandbox work directory: diagram.dot, diagram.png"
        output = _HEALTHY.replace(_SURVIVING_LINE, left)
        assert output != _HEALTHY, "the fixture moved"

        failures = check.assess(output, _png(64, 64))
        assert any("diagram.dot, diagram.png" in reason for reason in failures)

    def test_a_missing_listing_fails(self):
        output = _HEALTHY.replace(f"{_SURVIVING_LINE}\n\n", "")
        assert output != _HEALTHY, "the fixture moved"

        failures = check.assess(output, _png(64, 64))
        assert any("should have read the sandbox back" in reason for reason in failures)

    def test_a_missing_listing_is_not_asked_for_when_nothing_landed(self):
        """The sample will not acquire a sandbox just to look inside it, because acquiring is
        what creates one — so a turn that landed nothing prints no listing, and demanding one
        would report a second failure for the same absence."""
        output = _HEALTHY.replace(f"{_SURVIVING_LINE}\n\n", "")
        failures = check.assess(output, None)
        assert failures == [reason for reason in failures if "no image on disk" in reason]

    def test_the_handler_firing_fails(self):
        fired = (
            f"{scaffold.MEASURED}The call directory was not reclaimed: the removal call failed: "
            "OSError: rm exited 1 (the framework's disposal: disposed)."
        )
        output = _HEALTHY.replace(_SURVIVING_LINE, f"{fired}\n\n{_SURVIVING_LINE}")

        failures = check.assess(output, _png(64, 64))
        assert any("on_reclaim_failure handler fired" in reason for reason in failures)

    def test_the_handler_firing_fails_even_though_the_listing_is_empty(self):
        """The reason both lines are read rather than only the listing.

        A removal that fails takes the sandbox with it — the framework disposes what it could
        not clean — so the working directory the sample reads afterwards belongs to a *new*
        sandbox and is empty for the wrong reason. The listing alone would call that healthy.
        """
        fired = f"{scaffold.MEASURED}The call directory was not reclaimed: it stayed."
        output = _HEALTHY.replace(_SURVIVING_LINE, f"{fired}\n\n{_SURVIVING_LINE}")
        assert _SURVIVING_LINE in output, "the empty listing has to still be there"

        assert check.assess(output, _png(64, 64)) != []

    def test_an_untagged_listing_answers_for_nothing(self):
        """Same rule as the disposal line: a model writing this sentence is not the host."""
        untagged = _HEALTHY.replace(_SURVIVING_LINE, "Left in the sandbox work directory: nothing")
        assert untagged != _HEALTHY, "the fixture moved"

        failures = check.assess(untagged, _png(64, 64))
        assert any("should have read the sandbox back" in reason for reason in failures)

    def test_the_sample_prints_both_lines_from_the_scaffold(self):
        source = (_ROOT / "samples" / _SAMPLE / "agent.py").read_text(encoding="utf-8")
        assert "{MEASURED}Left in the sandbox work directory: " in source, (
            f"samples/{_SAMPLE}/agent.py no longer tags what it read out of the sandbox, so "
            "this check reads a line a model could have written"
        )
        assert "{MEASURED}The call directory was not reclaimed: " in source, (
            f"samples/{_SAMPLE}/agent.py no longer reports a failed reclaim, so a run whose "
            "cleanup failed would read as healthy here"
        )


class TestDimensions:
    def test_it_reads_what_the_header_declares(self):
        assert check.dimensions(_png(1920, 1080)) == (1920, 1080)

    def test_it_refuses_anything_it_cannot_read(self):
        assert check.dimensions(b"not a png at all, but long enough to reach the offsets") is None


class TestEveryReasonIsReported:
    def test_a_run_that_failed_every_way_says_so_every_time(self):
        """A checker that stops at the first reason makes a red run take two live runs to read."""
        failures = check.assess("nothing here", b"<svg/>")
        assert len(failures) == 3
        assert any("did not run to completion" in reason for reason in failures)
        assert any("Left in the sandbox work directory" in reason for reason in failures)
        assert any("not a readable PNG" in reason for reason in failures)
