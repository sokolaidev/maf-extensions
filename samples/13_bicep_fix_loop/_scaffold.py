"""Configuration scaffolding for this sample. Not part of the library, and not an example.

Every sample carries a byte-identical copy of this file, and `tests/test_sample_scaffold.py`
holds them that way. Copying is deliberate: a sample has to run from a directory the reader
downloaded, against wheels from PyPI, with nothing installed from this repository — so a
shared module would have to be published, and publishing it would make it API. `sys.path[0]`
is the script's directory, which is what lets `from _scaffold import ...` work at all.

`require_env_vars` is here rather than in `maf-sandbox` for a reason worth stating, since the
line count argues the other way: it prints to stderr and returns a sentinel for `main` to exit
on. That is a script's job. A library helper that did it would be reached for by an
application, where printing to stderr and exiting is exactly wrong.

`tool_results` and `evidence` are here for a different reason: what they print is read by
`scripts/check_live_*.py`, so the *format* is a contract between two files that live in
different directories and run at different times. One copy is what keeps a checker from having
to accept a dialect of that block per sample that emits it (#314).
"""

from __future__ import annotations

import os
import sys

#: Prefix on every line a sample vouches for, as opposed to anything a model said. The model
#: answers into the same stream that the live check parses, so a reply containing
#: "compiles that reached the sandbox: 0" is otherwise indistinguishable from the count. It also
#: tells a human reading a CI log which lines are the harness speaking.
_TAG = "[measured]"
MEASURED = f"  {_TAG} "


def require_env_vars(names: tuple[str, ...]) -> dict[str, str] | None:
    """Read `names` from the environment, or report every one that is missing.

    Worth doing before anything else, and worth failing on.  A kind's tool factory returns an
    empty list when the router has no backend, so a half-configured run does not crash — it
    quietly produces an agent with no tools, which answers the question from the model alone.
    That is the T0 behaviour these samples exist to contrast with, and it is indistinguishable
    from success unless someone says so out loud.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print("Not configured. These environment variables are unset:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print("\nSee this directory's README.md.", file=sys.stderr)
        return None
    return {name: os.environ[name] for name in names}


def quoted(text: str) -> str:
    """`text`, with any line impersonating a measurement marked as a quotation.

    `MEASURED` is what tells a live check which lines the sample vouches for, and a model writes
    into the same stream. Nothing puts the tag in its context, so a collision is improbable — but
    a tag is worth exactly what it excludes, and one pass over the text makes that structural
    rather than statistical.

    Matched case-insensitively on purpose: a sanitizer narrower than its reader is a hole. Worth
    applying to a tool's own output as well as to a model's replies wherever the model can steer
    what the tool echoes back — a rejected filename comes back inside the refusal.
    """
    return "\n".join(
        f"> {line.lstrip()}" if line.lstrip().lower().startswith(_TAG.lower()) else line
        for line in text.splitlines()
    )


def tool_results(reply: object, name: str) -> list[str]:
    """Everything the tool `name` returned during `reply`, in the order it came back.

    The point of reading these rather than `reply.text` is that a model does not write them: the
    framework records what the tool returned, next to the call that asked for it. A model can say
    a compiler reported something; it cannot put that sentence here without the compiler.

    Results are matched to calls by `call_id` rather than taken as every function result in the
    turn, so a sample that attaches more than one tool counts only its own.
    """
    asked = {
        content.call_id
        for message in getattr(reply, "messages", [])
        for content in message.contents
        if getattr(content, "type", None) == "function_call"
        and getattr(content, "name", None) == name
    }
    return [
        str(getattr(content, "result", ""))
        for message in getattr(reply, "messages", [])
        for content in message.contents
        if getattr(content, "type", None) == "function_result"
        and getattr(content, "call_id", None) in asked
    ]


def evidence(heading: str, results: list[str], counted: str) -> str:
    """A tool's own output, fenced so a live check can read it and nothing else.

    The fence is the heading and the tagged count that closes it. A model is free to write the
    heading — and to write plausible diagnostics under it — but the closing line carries the tag,
    which `quoted` has already taken away from anything the model said. So a forged block cannot
    be closed, and a checker that reads the *last* heading a closing line still follows reads
    this one.

    Every result is quoted before it is indented, in that order: quoting first defangs a tag the
    tool was made to echo, and indenting after puts what is left beyond the checker's anchor.
    """
    body = "\n\n".join(
        "\n".join(f"  {line}" for line in quoted(result).splitlines()) for result in results
    )
    return (
        f"== {heading} ==\n\n"
        + (f"{body}\n\n" if body.strip() else "")
        + (f"{MEASURED}{counted}: {len(results)}")
    )
