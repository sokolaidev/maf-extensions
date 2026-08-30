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

import ast
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

#: The one line here that is not prose: the sample's own report of what `dispose_scope`
#: returned, tagged as the sample tags it.
_DISPOSAL_LINE = f"{scaffold.MEASURED}Disposed 1 sandbox(es)."

#: The other measured line, from the handler the sample wires on the router. Nought is what a
#: healthy run reports, and it is the framework's own count rather than a probe of the guest.
_RECLAIM_LINE = f"{scaffold.MEASURED}Reclaim failures this turn: 0"

#: A healthy run: the model's own words about what it drew, then that line. Nothing in the
#: reply is matched — it is prose, and it is deliberately not the prose a checker could be
#: tuned to.
_HEALTHY = f"""\
I wrote the DOT for a three-node pipeline and called render_diagram. It saved the image as
diagram.png under out/. I have not seen the image itself.

{_RECLAIM_LINE}
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
            f"Here is a lovely diagram.\n\n{_RECLAIM_LINE}\n"
            f"{scaffold.MEASURED}Disposed 2 sandbox(es).\n"
        )
        assert check.assess(output, _png(1, 1)) == []


class TestTheReclaimHalf:
    """What the sample vouches for about the call directories it made.

    Sample 07 wires `ReclaimConfig.on_failure` and counts what it was told, so this line is the
    framework's own answer rather than a probe of the guest.
    """

    def test_a_missing_line_fails(self):
        """The `finally` not reached at all: nothing vouches for the directories."""
        output = _HEALTHY.replace(f"{_RECLAIM_LINE}\n", "")
        failures = check.assess(output, _png(640, 480))
        assert any("nothing vouches for the call directories" in reason for reason in failures)

    def test_a_nonzero_count_fails(self):
        """The failure this exists for: a call the framework could not clean."""
        output = _HEALTHY.replace(
            _RECLAIM_LINE, f"{scaffold.MEASURED}Reclaim failures this turn: 1"
        )
        failures = check.assess(output, _png(640, 480))
        assert any("could not be reclaimed" in reason for reason in failures)

    def test_the_tag_is_what_makes_it_the_frameworks(self):
        """An untagged line is the model's prose, and answers for nothing."""
        untagged = _HEALTHY.replace(_RECLAIM_LINE, "Reclaim failures this turn: 0")
        failures = check.assess(untagged, _png(640, 480))
        assert any("nothing vouches for the call directories" in reason for reason in failures)

    def test_a_failed_reclaim_is_not_read_as_a_turn_that_never_ran(self):
        """The two measurements interact, and only one reading of the pair is right.

        This sample disposes what it could not clean, so a call whose reclaim failed loses its
        sandbox there rather than at `dispose_scope` — and the final purge honestly counts
        nought. The count is proof a call *did* hold one, so it cannot also mean the model
        never called the tool.
        """
        output = _HEALTHY.replace(
            _RECLAIM_LINE, f"{scaffold.MEASURED}Reclaim failures this turn: 1"
        ).replace(_DISPOSAL_LINE, f"{scaffold.MEASURED}Disposed 0 sandbox(es).")
        failures = check.assess(output, _png(640, 480))
        assert any("could not be reclaimed" in reason for reason in failures)
        assert not any("no sandbox was ever created" in reason for reason in failures), (
            "a failed reclaim was diagnosed as a turn that never called the tool"
        )
        assert len(failures) == 1

    def test_the_sample_prints_the_line_from_the_scaffold(self):
        """The producer half. Without it the wording above pins only this file to itself.

        Everything else here feeds `assess` a string this module wrote, so a rename on either
        side of the contract stays green until the sample runs live.
        """
        source = (_ROOT / "samples" / _SAMPLE / "agent.py").read_text(encoding="utf-8")
        assert "{MEASURED}Reclaim failures this turn: " in source, (
            f"samples/{_SAMPLE}/agent.py no longer tags its reclaim-failure count the way "
            "`_RECLAIM_FAILURES` reads it, so the live check vouches for nothing"
        )

    def test_the_handler_is_wired_to_the_router(self):
        """The half a healthy run cannot show, because nought is what it reports either way.

        Detach `on_failure` and the count still prints nought forever: this suite, and the
        live check it backs, stay green over a sample that vouches for nothing. Only the
        source says whether the number came from the framework or from an empty list.
        """
        source = (_ROOT / "samples" / _SAMPLE / "agent.py").read_text(encoding="utf-8")
        assert "on_failure=note_reclaim_failure" in source, (
            f"samples/{_SAMPLE}/agent.py no longer passes its handler to `ReclaimConfig`, so "
            "the count it prints is an empty list rather than the framework's answer"
        )
        assert "reclaim_failures.append(failure)" in source, (
            f"samples/{_SAMPLE}/agent.py no longer records what the handler was told, so the "
            "count it prints is nought whatever the framework reported"
        )

    def test_the_handler_records_before_it_prints(self):
        """Order is load-bearing here, because the framework swallows what this raises.

        `on_reclaim_failure` runs inside an `except Exception` that logs and continues, so a
        `print` that threw — a closed stderr, a guest path the console cannot encode — would
        skip the append and leave the count at nought. The check reads that count, so the run
        would go green over the failure it exists to report.
        """
        tree = ast.parse((_ROOT / "samples" / _SAMPLE / "agent.py").read_text(encoding="utf-8"))
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "note_reclaim_failure"
        )
        dumped = [ast.dump(statement) for statement in handler.body]
        records = next(i for i, s in enumerate(dumped) if "reclaim_failures" in s)
        prints = next(i for i, s in enumerate(dumped) if "'print'" in s)
        assert records < prints, (
            f"samples/{_SAMPLE}/agent.py records the failure after printing it, so a print "
            "that raises loses the count the live check reads"
        )

    def test_cleanup_runs_before_anything_reports_it(self):
        """The `finally` is the only thing that takes the container down, so it goes first.

        A `print` on a stream that has gone raises, and an exception in a `finally` abandons
        the rest of it. Reporting ahead of `dispose_scope` therefore risks trading the whole
        sandbox for a line of output about it.
        """
        tree = ast.parse((_ROOT / "samples" / _SAMPLE / "agent.py").read_text(encoding="utf-8"))
        run = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
        )
        block = next(handler.finalbody for handler in ast.walk(run) if isinstance(handler, ast.Try))
        dumped = [ast.dump(statement) for statement in block]
        disposes = next(i for i, s in enumerate(dumped) if "dispose_scope" in s)
        reports = next(i for i, s in enumerate(dumped) if "'print'" in s)
        assert disposes < reports, (
            f"samples/{_SAMPLE}/agent.py reports before it disposes, so a print that raises "
            "leaves the container running"
        )

    def test_the_sample_states_its_reclaim_timeout(self):
        """#520 asks for the timeout too, and it is the field a run cannot show.

        It equals the default, so dropping it changes no behaviour and no other check here
        would notice — only the source says whether the host chose the bound or inherited it.
        """
        source = (_ROOT / "samples" / _SAMPLE / "agent.py").read_text(encoding="utf-8")
        assert "timeout=" in source, (
            f"samples/{_SAMPLE}/agent.py no longer states its `ReclaimConfig.timeout`, which "
            "bounds the removal, the disposal and the callback separately"
        )


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


class TestThePurgeThatCouldNotProveItself:
    """What `Not fully disposed` does and does not say.

    It says the sweep could not account for everything, not that something survived: docker
    raises `unlisted` whenever the label query fails, which happens whether or not there was
    anything to sweep. So it is reported on its own terms and answers for nothing else.
    """

    def test_it_is_reported_in_its_own_right(self):
        output = _HEALTHY.replace(
            _DISPOSAL_LINE,
            f"{_DISPOSAL_LINE}\n"
            f"{scaffold.MEASURED}Not fully disposed: DisposalFailure(code=TIMEOUT)",
        )
        failures = check.assess(output, _png(640, 480))
        assert any("could not prove it disposed everything" in reason for reason in failures)
        assert any("DisposalFailure(code=TIMEOUT)" in reason for reason in failures)

    def test_it_makes_the_nought_inconclusive_rather_than_excused(self):
        """Neither reading can be asserted, so the check reports that instead of picking.

        The same line is raised by a removal that failed with a sandbox — `_purge` counts only
        what it removed — and by a label query that failed without one. So it can neither
        excuse the nought nor be read past it.
        """
        output = _HEALTHY.replace(
            _DISPOSAL_LINE,
            f"{scaffold.MEASURED}Disposed 0 sandbox(es).\n"
            f"{scaffold.MEASURED}Not fully disposed: DisposalFailure(code=unlisted)",
        )
        failures = check.assess(output, _png(640, 480))
        assert not any("no sandbox was ever created" in reason for reason in failures)
        assert any("cannot say whether" in reason for reason in failures)
        assert any("could not prove it disposed everything" in reason for reason in failures)
        assert len(failures) == 2

    def test_a_clean_purge_still_names_the_model(self):
        """Without that line the nought is not ambiguous, and the check must still say so."""
        output = _HEALTHY.replace(_DISPOSAL_LINE, f"{scaffold.MEASURED}Disposed 0 sandbox(es).")
        failures = check.assess(output, _png(640, 480))
        assert any("no sandbox was ever created" in reason for reason in failures)
        assert not any("cannot say whether" in reason for reason in failures)

    def test_a_nonzero_count_does_not_hide_it(self):
        """A scope can dispose one sandbox and still fail on another; the count alone says fine."""
        output = _HEALTHY.replace(
            _DISPOSAL_LINE,
            f"{_DISPOSAL_LINE}\n"
            f"{scaffold.MEASURED}Not fully disposed: DisposalFailure(code=REFUSED)",
        )
        failures = check.assess(output, _png(640, 480))
        assert any("could not prove it disposed everything" in reason for reason in failures)

    def test_a_reclaim_failure_still_excuses_the_nought(self):
        """The one thing that does: its sandbox was disposed before the purge looked."""
        output = _HEALTHY.replace(
            _RECLAIM_LINE, f"{scaffold.MEASURED}Reclaim failures this turn: 1"
        ).replace(_DISPOSAL_LINE, f"{scaffold.MEASURED}Disposed 0 sandbox(es).")
        failures = check.assess(output, _png(640, 480))
        assert not any("no sandbox was ever created" in reason for reason in failures)

    def test_a_healthy_run_reports_nothing_here(self):
        """The line is absent unless `dispose_scope` returned a failure."""
        assert check.assess(_HEALTHY, _png(640, 480)) == []


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


class TestDimensions:
    def test_it_reads_what_the_header_declares(self):
        assert check.dimensions(_png(1920, 1080)) == (1920, 1080)

    def test_it_refuses_anything_it_cannot_read(self):
        assert check.dimensions(b"not a png at all, but long enough to reach the offsets") is None


class TestEveryReasonIsReported:
    def test_a_run_that_failed_three_ways_says_so_three_times(self):
        """A checker that stops at the first reason makes a red run take three live runs to read.

        Keyed on what each reason is about rather than on the count, so a fourth independent
        check does not turn this red for the wrong reason.
        """
        failures = check.assess("nothing here", b"<svg/>")
        assert len(failures) == 3
        assert any("did not run to completion" in reason for reason in failures)
        assert any("call directories" in reason for reason in failures)
        assert any("not a readable PNG" in reason for reason in failures)
