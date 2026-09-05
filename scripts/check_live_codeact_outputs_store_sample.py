"""Assert that a live withheld CodeAct run got its answer back out of the outputs store.

For `samples/16_docker_codeact_outputs_store`, and the reason it needs a checker of its own
rather than `check_live_codeact_files_sample.py`: that sample lands to a host directory the
checker can open, and this one lands into an in-memory store that only the run itself ever
holds. What stands in for the file on disk is the model's own read-back, fenced.

    python samples/16_docker_codeact_outputs_store/agent.py | tee out.txt
    python scripts/check_live_codeact_outputs_store_sample.py out.txt

**The grand total in the reply is the load-bearing check, and it is stronger here than in
sample 08.** The sample runs with `withhold_guest_output=True`, so nothing the program printed
comes back: the tool's result is one line saying whether the program exited cleanly, and the
standing sentence naming this call's folder. A correct total in the reply therefore cannot
have come from `stdout`. The only road left is the
one this sample exists to draw: the program wrote a file, `make_file_store_sink` landed it under
this call's folder, and the model read it back with a host tool.

The fenced block is what makes that more than an inference. `evidence` in the sample's
`_scaffold.py` closes it with a `[measured]` line, and `quoted` takes that tag away from
anything the model said before either is printed — so a model can write the heading and write
plausible Markdown under it, and cannot close the block. What is inside came from the tool.

Three further things are read, all of them the host's own tagged lines: that the scope purge
disposed a sandbox and could account for every one, that the summary reached the sink this
turn, and that it landed under a per-call folder rather than at the top of the store. The last is what would go quietly wrong if
`Artifact.call_id` ever stopped reaching the sink: every call would overwrite the last, which
the sink's own refusal turns into a failed landing rather than a silent one — but only the
folder shape says the id was ever there.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

#: The grand total over the `sales.csv` that ships beside the sample. One right answer.
_GRAND_TOTAL = "1124"

#: Every region in that file, and what the summary must report for each.
_REGION_TOTALS = {"north": "390", "south": "200", "east": "84", "west": "450"}

#: The declared output's name, and what the landing path must end with.
_SUMMARY_NAME = "summary.md"

#: The per-call folder is `uuid4().hex`, minted host-side. Checked for shape rather than value:
#: what matters is that the landing went *under* one, not which one this run drew.
_LANDED_PATH = re.compile(rf"^[0-9a-f]{{32}}/{re.escape(_SUMMARY_NAME)}$")

#: Both tagged patterns read a line the *sample* wrote. See `check_live_codeact_files_sample.py`
#: for why the `^` anchor is the whole of what refuses a tag written mid-sentence by a model.
_M = r"^  (?-i:\[measured\]) "
_F = re.MULTILINE | re.IGNORECASE

_DISPOSED = re.compile(_M + r"Disposed\s+(\d+)\s+sandbox", _F)
_NOT_DISPOSED = re.compile(_M + r"Not fully disposed:\s*(.+)$", _F)
_LANDED = re.compile(_M + r"Landed this turn in the outputs store[^:\n]*:[ \t]*(.+)$", _F)

_HEADING = re.compile(r"==\s*read back out of the outputs store\s*==", re.IGNORECASE)
_READBACKS = re.compile(_M + r"Read-backs the model made:\s*(\d+)", _F)

#: What a model may put between thousands, and the signs it may render — the reply half of this
#: output is model-authored prose, where `\u2212390` is as likely as `-390`.
_SEPARATOR = "[,\u00a0\u202f ]"
_SIGNS = "+\u2212\uff0b\uff0d-"


def _number(value: str) -> re.Pattern[str]:
    """Match ``value`` where it is the whole number rather than part of a longer token.

    The same reader `check_live_codeact_files_sample.py` uses, and for the same reason: nothing
    may abut the value, so `840`, `-390`, `1,390` and `1124e3` are all not it.
    """
    grouped = value if len(value) <= 3 else f"{value[:-3]}{_SEPARATOR}?{value[-3:]}"
    return re.compile(
        rf"(?<![\w.{_SIGNS}])(?<!\d{_SEPARATOR}){grouped}"
        rf"(?:\.0*)?(?!\.?\d)(?!{_SEPARATOR}\d)(?!\w)"
    )


def _regions_reporting_their_own_total(summary: str) -> tuple[set[str], set[str]]:
    """Split the regions into (named and correct, named but not followed by their total).

    Each region's total must appear between that region's name and whatever region is named
    next.  Checking the two independently over the whole block passes a summary with every value
    swapped, since all eight strings are still present.
    """
    mentions = sorted(
        (match.start(), region)
        for region in _REGION_TOTALS
        for match in re.finditer(rf"\b{re.escape(region)}\b", summary, re.IGNORECASE)
    )
    correct: set[str] = set()
    for index, (start, region) in enumerate(mentions):
        end = mentions[index + 1][0] if index + 1 < len(mentions) else len(summary)
        if _number(_REGION_TOTALS[region]).search(summary[start:end]):
            correct.add(region)
    named = {region for _, region in mentions}
    return correct, named - correct


def _landed_paths(reported: str) -> set[str]:
    """The destinations on the sample's landing line, exactly as the host recorded them.

    JSON so this can be read rather than guessed at: an artifact name may legally contain a
    comma. An unparseable line is no destinations, which fails.
    """
    try:
        paths = json.loads(reported)
    except ValueError:
        return set()
    if not isinstance(paths, list):
        return set()
    return {path for path in paths if isinstance(path, str)}


def _split(output: str) -> tuple[str, str, int] | None:
    """The model's reply, the read-backs the tool returned, and the count that closes them.

    ``None`` when there is no block to read.  Exactly one closing line may exist in a healthy
    run and a second is reason enough to trust none of them; the *last* heading before it is the
    sample's, so a reply that quoted the heading leaves its own text in the reply half.
    """
    closes = list(_READBACKS.finditer(output))
    if len(closes) != 1:
        return None
    opened = list(_HEADING.finditer(output, 0, closes[0].start()))
    if not opened:
        return None
    return (
        output[: opened[-1].start()],
        output[opened[-1].end() : closes[0].start()],
        int(closes[0].group(1)),
    )


def _assess_readbacks(block: str, readbacks: int) -> list[str]:
    """What the outputs store handed back, read from the store's own tool rather than the reply."""
    failures: list[str] = []
    if readbacks < 1:
        failures.append(
            "the model never read the outputs store — with the guest's output withheld there "
            "is no other road to the summary, so the run answered from something it made up"
        )
        return failures

    if not _number(_GRAND_TOTAL).search(block):
        failures.append(
            f"the read-backs do not contain {_GRAND_TOTAL} as a number — the file the model "
            "read is not the summary this task asked for"
        )

    correct, wrong = _regions_reporting_their_own_total(block)
    for region in _REGION_TOTALS:
        if region in wrong:
            failures.append(
                f"the read-backs name the {region} region but not its total of "
                f"{_REGION_TOTALS[region]} before the next region — a swapped or wrong value"
            )
        elif region not in correct:
            failures.append(f"the read-backs do not mention the {region} region")
    return failures


def _assess_reply(reply: str) -> list[str]:
    """The half that proves the value reached the *model*, not merely the log."""
    if not _number(_GRAND_TOTAL).search(reply):
        return [
            f"{_GRAND_TOTAL} is not in the reply as a number — the program's output is withheld, "
            "so a total that never reaches the model has nowhere else to have come from"
        ]
    return []


def _assess_landing(output: str) -> list[str]:
    """Where the sink actually put the file, and that it put it under a per-call folder."""
    landed = _LANDED.search(output)
    if landed is None:
        return [
            "no measured 'Landed this turn in the outputs store' line — the sample did not "
            "reach its final report"
        ]
    paths = _landed_paths(landed.group(1))
    if not any(_LANDED_PATH.match(path) for path in paths):
        return [
            f"the host recorded landing {landed.group(1).strip()!r}, and none of those is a "
            f"per-call folder holding {_SUMMARY_NAME!r} — either the declared output never "
            "reached the sink, or it landed without this call's id"
        ]
    return []


def _assess_disposal(output: str) -> list[str]:
    """Every reason the scope purge is not proof that this conversation left nothing behind.

    A purge that reported a failure makes a nought *inconclusive* rather than excusing it: the
    sample cannot then say whether no sandbox was made or one was made and could not be removed.
    """
    disposed = _DISPOSED.search(output)
    if disposed is None:
        return ["no measured 'Disposed N sandbox(es)' line — the sample did not run to completion"]
    undisposed = _NOT_DISPOSED.search(output)
    failures: list[str] = []
    if int(disposed.group(1)) < 1 and undisposed is None:
        failures.append(
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing ran in one"
        )
    elif int(disposed.group(1)) < 1:
        failures.append(
            "'Disposed 0 sandbox(es)' beside a purge that failed — this cannot say whether "
            "nothing ran or a sandbox was made and could not be removed"
        )
    if undisposed is not None:
        failures.append(
            f"the scope purge could not account for every sandbox "
            f"({undisposed.group(1).strip()}) — data from this conversation may remain"
        )
    return failures


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    split = _split(output)
    if split is None:
        return (
            [
                "the run printed no block of what the outputs store returned — the total in the "
                "reply is then a constant the model could recite, which is what the fence exists "
                "to rule out"
            ]
            + _assess_landing(output)
            + _assess_disposal(output)
        )

    reply, block, readbacks = split
    failures = _assess_readbacks(block, readbacks)
    failures.extend(_assess_reply(reply))
    failures.extend(_assess_landing(output))
    failures.extend(_assess_disposal(output))
    return failures


def main(argv: list[str]) -> int:
    """CLI entry: read the sample output, run ``assess``, and print OK or FAIL."""
    if len(argv) != 2:
        print(f"usage: {argv[0]} <output-file>", file=sys.stderr)
        return 2

    failures = assess(Path(argv[1]).read_text(encoding="utf-8"))
    if failures:
        print("FAIL: the live sample run did not verify the published stack:", file=sys.stderr)
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "OK  the CodeAct sample landed its summary under a per-call folder and read it back "
        "out of the outputs store, with the guest's own output withheld"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
