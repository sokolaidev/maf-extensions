"""The host-tool file transport's wire format, written down once and enforced.

The guest writes request files; the host supervisor writes response files. The request side has
**two implementations and nothing but this module compares them**: the generated shim
(:func:`maf_sandbox._shim.host_tool_shim`) and the ``_ScriptedGuest`` double the supervisor suite
runs against, which hand-writes the same shapes. Rename a key in one and the other keeps emitting
the old one, the supervisor keeps parsing the old one, and the suite stays green against a format
nothing in production produces. These probes are what keep the two honest: both are driven through
:func:`assert_request_conforms`, so a shape that satisfies one satisfies the other or the suite
goes red. A second guest implementation — a Node or Go one, or the hand-rolled programs the
transport serves identically — has the same contract to meet, and it is this.

The format, in one place:

- Requests live in the calls directory (``host_tool_calls/``) as ``NNNN.request.json``, ``NNNN``
  a 1-based, zero-padded four-digit identifier assigned in order.
- A request is a JSON object with **exactly** ``{"id", "name", "arguments"}`` — ``id`` the string
  form of ``NNNN``, ``name`` the tool, ``arguments`` an object — or **exactly**
  ``{"id", "abandoned": true}`` for a number a caller claimed and could not publish under.
- A response is written whole by the host as ``NNNN.response.json``: **exactly** ``{"value": ...}``
  or **exactly** ``{"refusal": "..."}``.
- Every payload is strict UTF-8, and non-empty: a zero-length file means *not arrived yet*, never
  an empty payload.
- Publication is atomic — a ``.part`` sibling renamed onto the final name — so a poll never reads
  a half-written file.

``NNNN.claim`` (taken with ``O_CREAT | O_EXCL``, never removed) is the shim's own multi-writer
coordination; the host never reads it and the single-writer double never writes it, so it is not
part of *this* shared contract. The reserved neighbour names a guest program may not take
(``program.py``'s siblings) are enforced where they are chosen, in
:func:`maf_sandbox.guest_run_layout`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_REQUEST_NAME = re.compile(r"^(?P<n>\d{4})\.request\.json$")
_RESPONSE_NAME = re.compile(r"^(?P<n>\d{4})\.response\.json$")


def _decoded_object(name: str, body: bytes) -> dict[str, Any]:
    """The JSON object a payload holds, or an :class:`AssertionError` saying why it is not one."""
    assert body, f"{name}: zero-length, which the transport reads as 'not arrived yet'"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as bad:
        raise AssertionError(f"{name}: not strict UTF-8 ({bad})") from bad
    try:
        parsed: Any = json.loads(text)
    except ValueError as bad:
        raise AssertionError(f"{name}: not valid JSON ({bad})") from bad
    assert isinstance(parsed, dict), (
        f"{name}: the payload is a JSON object, not {type(parsed).__name__}"
    )
    return parsed


def assert_request_conforms(name: str, body: bytes) -> None:
    """A request file — its ``name`` and its ``body`` bytes — matches the wire format.

    Raises :class:`AssertionError` naming the first departure. Both the generated shim and the
    double are driven through this, so a divergence between them is a failing assertion here.
    """
    named = _REQUEST_NAME.match(name)
    assert named, f"{name!r} is not NNNN.request.json"
    identifier = named.group("n")
    parsed = _decoded_object(name, body)
    keys = set(parsed)
    if keys == {"id", "name", "arguments"}:
        assert parsed["id"] == identifier, f"{name}: id {parsed['id']!r} does not match the name"
        assert isinstance(parsed["name"], str), f"{name}: name is not a string"
        assert isinstance(parsed["arguments"], dict), f"{name}: arguments is not an object"
    elif keys == {"id", "abandoned"}:
        assert parsed["id"] == identifier, f"{name}: id {parsed['id']!r} does not match the name"
        assert parsed["abandoned"] is True, f"{name}: abandoned must be the literal true"
    else:
        raise AssertionError(
            f"{name}: keys {sorted(keys)} are neither a call {{'id', 'name', 'arguments'}} nor an "
            f"abandonment {{'id', 'abandoned'}}"
        )


def assert_response_conforms(name: str, body: bytes) -> None:
    """A response file — its ``name`` and its ``body`` bytes — matches the wire format."""
    assert _RESPONSE_NAME.match(name), f"{name!r} is not NNNN.response.json"
    parsed = _decoded_object(name, body)
    keys = set(parsed)
    assert keys in ({"value"}, {"refusal"}), (
        f"{name}: a response is exactly {{'value'}} or {{'refusal'}}, not {sorted(keys)}"
    )
    if keys == {"refusal"}:
        assert isinstance(parsed["refusal"], str), f"{name}: refusal is not a string"


def assert_calls_conform(
    files: Mapping[str, bytes], *, expect_requests: int | None = None
) -> tuple[int, int]:
    """Validate every request and response file in a producer's calls directory.

    ``files`` maps a path (or bare name) to its bytes; anything that is not a request or a
    response file — a ``.claim``, a ``.part`` — is ignored, since neither is part of the shared
    contract. Returns ``(requests, responses)`` validated.

    ``expect_requests`` is the tripwire against a vacuous pass: a producer that wrote nothing
    would satisfy every probe by having nothing to check, so a caller that knows how many calls it
    drove says so, and a count that falls short fails here rather than passing green.
    """
    requests = responses = 0
    for path, body in files.items():
        name = path.rsplit("/", 1)[-1]
        if _REQUEST_NAME.match(name):
            assert_request_conforms(name, body)
            requests += 1
        elif _RESPONSE_NAME.match(name):
            assert_response_conforms(name, body)
            responses += 1
    if expect_requests is not None:
        assert requests == expect_requests, (
            f"expected {expect_requests} request file(s), validated {requests}"
        )
    return requests, responses
