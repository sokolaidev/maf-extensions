"""Assert that a live `samples/07_docker_diagram` run actually rendered and landed a PNG.

    python samples/07_docker_diagram/agent.py | tee out.txt
    python scripts/check_live_diagram_sample.py out.txt samples/07_docker_diagram/out/diagram.png

Everything this sample prints except its own tagged lines is model prose, and prose is not
evidence that a renderer ran: an agent describing the picture it would have drawn writes the
same paragraph as one that drew it. So the assertion is what the host produced itself — the
sample's own disposal line, which is non-zero only if a sandbox was created to serve a tool
call, and the file the sink wrote.

The image is read structurally rather than compared: a PNG signature, then the dimensions out
of the IHDR chunk. The model writes the DOT, so what the graph contains and how large it comes
out are its choices, and anything tighter than "a raster with both dimensions non-zero" would
assert on those instead of on the round trip.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

#: Tagged, so a model writing "Disposed 1 sandbox(es)." into its reply does not answer for the
#: router — this line is the sample's own report of what `dispose_scope` returned. `MEASURED` in
#: `samples/*/_scaffold.py` writes the tag and `quoted` there takes it away from anything the
#: model said. Case-sensitive on the tag, lax after it: a reader broader than its sanitizer is a
#: hole rather than tolerance (#314).
_DISPOSED = re.compile(
    r"^  (?-i:\[measured\]) Disposed\s+(\d+)\s+sandbox", re.MULTILINE | re.IGNORECASE
)

#: The framework's own count of call directories it could not remove, from the handler the
#: sample wires on the router. Nought is the claim; anything else cost the conversation its
#: sandbox, since this sample's policy disposes what it could not clean. Tagged like the line
#: above, so a model writing "no reclaim failures" into its reply cannot answer for the
#: framework.
_RECLAIM_FAILURES = re.compile(
    r"^  (?-i:\[measured\]) Reclaim failures this turn:\s+(\d+)", re.MULTILINE | re.IGNORECASE
)

#: The 8-byte PNG signature, and the fixed layout that must follow it: a 4-byte chunk length,
#: the chunk type `IHDR`, then width and height as big-endian uint32. The header chunk is first
#: in every PNG, so these offsets need no chunk walk.
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TYPE_AT = len(_SIGNATURE) + 4
_WIDTH_AT = _TYPE_AT + 4
_HEADER_BYTES = _WIDTH_AT + 8


def dimensions(image: bytes) -> tuple[int, int] | None:
    """The ``(width, height)`` a PNG declares, or ``None`` if this is not a readable PNG.

    A truncated render fails here rather than at a size comparison: `dot` writing a header and
    dying leaves a file that still opens with the right magic bytes.
    """
    if len(image) < _HEADER_BYTES or not image.startswith(_SIGNATURE):
        return None
    if image[_TYPE_AT : _TYPE_AT + 4] != b"IHDR":
        return None
    width, height = struct.unpack(">II", image[_WIDTH_AT:_HEADER_BYTES])
    return width, height


def assess(output: str, image: bytes | None) -> list[str]:
    """Return every reason this was not a healthy run — empty means it passed.

    ``image`` is the landed file's bytes, or ``None`` when it is not there at all. Kept as an
    argument rather than read here so the whole judgement stays a pure function.
    """
    failures: list[str] = []

    disposed = _DISPOSED.search(output)
    if disposed is None:
        failures.append(
            "no measured 'Disposed N sandbox(es)' line — the sample did not run to completion"
        )
    elif int(disposed.group(1)) < 1:
        failures.append(
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so the model answered "
            "without calling render_diagram"
        )

    left_behind = _RECLAIM_FAILURES.search(output)
    if left_behind is None:
        failures.append(
            "no measured 'Reclaim failures this turn' line — the sample did not reach its "
            "`finally`, so nothing vouches for the call directories it made"
        )
    elif int(left_behind.group(1)) > 0:
        failures.append(
            f"{left_behind.group(1)} call director(ies) could not be reclaimed — each one cost "
            "the conversation its sandbox, and where the disposal did not land either, those "
            "files stay readable by the next call"
        )

    if image is None:
        failures.append(
            "no image on disk — the turn may have described a diagram and rendered nothing, "
            "which is the failure this sample exists to catch"
        )
        return failures

    size = dimensions(image)
    if size is None:
        failures.append(
            f"the landed file is not a readable PNG ({len(image)} bytes, starting "
            f"{image[:8]!r}) — nothing usable came back through FILES_OUT"
        )
        return failures

    width, height = size
    if width == 0 or height == 0:
        failures.append(
            f"the landed PNG declares {width}x{height} — a header with no raster behind it"
        )

    return failures


def main(argv: list[str]) -> int:
    """CLI entry: read the sample output and the landed image, run ``assess``, and print OK or FAIL."""
    if len(argv) != 3:
        print(f"usage: {argv[0]} <output-file> <landed-image-path>", file=sys.stderr)
        return 2

    output = Path(argv[1]).read_text(encoding="utf-8")
    landed_path = Path(argv[2])
    image = landed_path.read_bytes() if landed_path.is_file() else None

    failures = assess(output, image)
    if failures:
        print(
            "FAIL: the live sample run did not verify the published stack:",
            file=sys.stderr,
        )
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    size = dimensions(image) if image is not None else None
    measured = f" {size[0]}x{size[1]}" if size else ""
    print(
        f"OK  the diagram sample rendered in a live container and landed a{measured} PNG "
        "against the published wheels"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
