"""``echoed_name``: how a refusal names the argument it rejected.

A refusal is text the model reads back on every later turn, so a name quoted into one is a
channel out of the tool — and the value being quoted is not always the one the model chose.
MAF's information-flow middleware rewrites a bracketed variable reference into the content it
stands for *before* the tool body runs, so a kind that echoes an argument can hand back
content the framework had hidden, under whatever label the tool declares (#810).

Quoting is still what the caller needs: a model that misspelled a name cannot fix it from a
message that will not say which.  So a value of a shape a refusal can afford to repeat is
repeated, and anything else is named by its position instead.

**A shape bound is not a classifier.** Nothing in this module can tell a rewritten argument
from one the model chose, and an instruction can be written without spaces —
``IGNORE_PRIOR_INSTRUCTIONS`` is the shape of a perfectly ordinary file name.  So the shape is
the weaker of two answers and the fallback for callers who have no other:
:func:`~maf_sandbox.maf.values_holding_hidden_content` asks the host's middleware which values
it rewrote, and ``hidden=True`` here settles the question without consulting the shape at all.
"""

from __future__ import annotations

__all__ = ["MAX_ECHOED_NAME_CHARACTERS", "echoed_name"]

#: How much of a value a refusal may repeat.  A bound on the *output*, not a claim about what a
#: name may be: :func:`~maf_sandbox.validate_artifact_name` accepts up to
#: :data:`~maf_sandbox.MAX_ARTIFACT_NAME_BYTES`, so a legitimate name longer than this is named
#: by its position too.  Counted in characters rather than in UTF-8 bytes, because what is
#: bounded is what a model reads rather than what a filesystem stores.
MAX_ECHOED_NAME_CHARACTERS: int = 120


def echoed_name(name: str, *, at: str | None = None, hidden: bool = False) -> str:
    """``name`` quoted, or its position where the value is not one a refusal may repeat.

    ``at`` says where the value came from — ``"files[1]"`` — and is what a refusal names in
    its place; without it a refusal can say only how long the value was.

    ``hidden`` is the answer a caller should prefer wherever it can get one: pass ``True``
    where the framework expanded content it had hidden into this value, and the shape below is
    not consulted at all.  :func:`~maf_sandbox.maf.values_holding_hidden_content` computes it.
    Left ``False``, the value is repeated when it is at most
    :data:`MAX_ECHOED_NAME_CHARACTERS` characters, printable and free of spaces — a bound on
    shape, which is what a caller with no better answer has.
    """
    if (
        not hidden
        and len(name) <= MAX_ECHOED_NAME_CHARACTERS
        and name.isprintable()
        and " " not in name
    ):
        return repr(name)
    if at is None:
        return f"a {len(name)}-character value"
    return f"the {len(name)}-character value at {at}"
