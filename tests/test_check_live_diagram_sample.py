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

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_diagram_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_diagram_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: A healthy run: the model's own words about what it drew, then the line the sample prints
#: after disposing. Nothing here is matched — it is prose, and it is deliberately not the
#: prose a checker could be tuned to.
_HEALTHY = """\
I wrote the DOT for a three-node pipeline and called render_diagram. It saved the image as
diagram.png under out/. I have not seen the image itself.

Disposed 1 sandbox(es).
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
        output = "Here is a lovely diagram.\n\nDisposed 2 sandbox(es).\n"
        assert check.assess(output, _png(1, 1)) == []


class TestTheSandboxHalf:
    def test_no_disposal_line_fails(self):
        failures = check.assess("I drew a diagram.", _png(640, 480))
        assert any("did not run to completion" in reason for reason in failures)

    def test_disposing_none_fails(self):
        """The shape this check exists for: an answer with no tool call behind it."""
        output = "Here is your diagram, showing three stages.\n\nDisposed 0 sandbox(es).\n"
        failures = check.assess(output, _png(640, 480))
        assert any("no sandbox was ever created" in reason for reason in failures)


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
    def test_a_run_that_failed_twice_says_so_twice(self):
        """A checker that stops at the first reason makes a red run take two live runs to read."""
        failures = check.assess("nothing here", b"<svg/>")
        assert len(failures) == 2
