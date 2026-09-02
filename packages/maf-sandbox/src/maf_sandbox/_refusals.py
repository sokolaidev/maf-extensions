"""``echoed_name``: how a refusal names the argument it rejected.

A refusal is text the model reads back on every later turn, so a name quoted into one is a
channel out of the tool — and the value being quoted is not always the one the model chose.
MAF's information-flow middleware rewrites a bracketed variable reference into the content it
stands for *before* the tool body runs, so a kind that echoes an argument can hand back
content the framework had hidden, under whatever label the tool declares (#810).

Quoting is still what the caller needs: a model that misspelled a name cannot fix it from a
message that will not say which.  So a name is quoted where it reads like a name, and named by
its position where it does not.  **That bounds the channel rather than closing it** — an
instruction can be written without spaces — and a kind wanting it closed renders the position
alone.
"""

from __future__ import annotations

__all__ = ["MAX_ECHOED_NAME_CHARACTERS", "echoed_name"]

#: Longer than any name these tools are given, and short enough that whatever gets through
#: reads as a token rather than as a sentence.  Counted in characters rather than in UTF-8
#: bytes: what is bounded here is what a model reads, not what a filesystem stores.
MAX_ECHOED_NAME_CHARACTERS: int = 120


def echoed_name(name: str, *, at: str | None = None) -> str:
    """``name`` quoted, or its position where the value does not read like a name.

    A name reads like one when it is at most :data:`MAX_ECHOED_NAME_CHARACTERS` characters,
    printable, and free of spaces.  ``at`` says where the value came from — ``"files[1]"`` —
    and is what a refusal names in place of the value; without it a refusal can say only how
    long the value was.
    """
    if len(name) <= MAX_ECHOED_NAME_CHARACTERS and name.isprintable() and " " not in name:
        return repr(name)
    if at is None:
        return f"a {len(name)}-character value"
    return f"the {len(name)}-character value at {at}"
