"""Historical summarizer prompts used by the paired compaction experiments."""

from typing import Final

BASELINE_REVISION: Final[str] = "55612574ec2bdf500b4ed4fa010ab5b92428978f"

BASELINE_PROMPTS: Final[dict[str, str]] = {
    "summary": (
        "You are compacting a conversation to save space. Below are the user's own turns, in "
        "order, with the assistant's replies and any tool output removed. Rewrite them as a "
        "single, much shorter account of what the user asked for, in the order they asked for"
        " it. Keep every requirement, constraint, correction and preference exactly as "
        "stated, and quote verbatim any value that could not be reconstructed or guessed: "
        "identifiers, codes, names, numbers, paths, URLs, versions, states, timestamps. Drop "
        "pleasantries, restatements and anything a later turn superseded. Write about the "
        "user in the third person and add nothing that is not in the text you were given."
    ),
    "fold": (
        "You are compacting a conversation to save space. Below are earlier compaction "
        "summaries of the user's own turns, in the order those turns were spoken; each one "
        "already stands for turns that are no longer present, and none of it is padding. "
        "Rewrite them as a single, shorter account of what the user asked for, in the order "
        "they asked for it. Keep every requirement, constraint, correction and preference "
        "exactly as stated, and quote verbatim any value that could not be reconstructed or "
        "guessed: identifiers, codes, names, numbers, paths, URLs, versions, states, "
        "timestamps. Drop only what a later summary superseded. Write about the user in the "
        "third person and add nothing that is not in the text you were given."
    ),
    "merge": (
        "The numbered texts below are compaction records: each is an account of tool results "
        "an agent received earlier in a conversation, written so those results could be "
        "removed. Write one record that replaces all of them. Copy every identifier, code, "
        "number, date, name and other exact value verbatim, from every record; do not "
        "paraphrase, round, abbreviate or drop any of them. Remove what the records repeat "
        "and cut wording, not values. Reply with the record text only."
    ),
    "rewrite_1": (
        "Rewrite the compaction record below so that it is at most about 60% of its current "
        "length. Keep every identifier, code, number, date, name and other exact value "
        "verbatim: do not paraphrase, round, abbreviate or drop any of them. Cut wording "
        "instead -- repetition, explanation, connecting prose and formatting. If several "
        "numbered records are given, write one record that carries the values of all of them."
        " Reply with the record text only."
    ),
    "rewrite_2": (
        "Rewrite the compaction record below so that it is at most about 36% of its current "
        "length. Keep every identifier, code, number, date, name and other exact value "
        "verbatim: do not paraphrase, round, abbreviate or drop any of them. Cut wording "
        "instead -- repetition, explanation, connecting prose and formatting. If several "
        "numbered records are given, write one record that carries the values of all of them."
        " Reply with the record text only."
    ),
}
