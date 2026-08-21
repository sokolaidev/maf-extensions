# Documentation

How this directory is organised, so the next extension family lands in the same shape rather than inventing one.

## One directory per extension family

`docs/<family>/` holds everything about one extension suite. Today there is one: [`sandbox/`](sandbox/README.md), the sandboxed-execution suite. A second family gets its own directory next to it, not a section inside this one.

[`maintainers.md`](maintainers.md) sits outside the families, because release plumbing — trusted publishing, the release train, the order the packages go out in — is repo-wide and belongs to no suite.

## Inside a family: main documents, then research

A family directory holds three things:

- `README.md` — the front door, for someone who has not met the suite before.
- the main documents — one per axis or boundary, with a subdirectory once a group outgrows a page (`kinds/`, `backends/`).
- `research/` — the records.

The **main documents** are the decided design. They are written in the present tense, describing the target as though it were the whole truth, because a document hedged into a description of the current commit stops being a design and starts being a changelog.

Every main document ends with a `## Status` table, and that is where the tense is paid for. One row per decision: what it is, whether it is implemented, and a tracking reference — a shipped PR, an open issue, or an explicit `untracked`. A row with an empty tracking cell is the failure this convention exists to prevent, since it is how a document quietly drifts from the code.

`<family>/research/` is the pre-decision pipeline: explorations that ask whether something is worth doing, and proposals that argue for a specific shape. Each opens with a banner saying what it is. When a decision is made, the decided content **moves out** into the main documents and the record **stays** — kept in the tense it was written, as the argument rather than a description of the code. A record is never edited to match what shipped; the main document is what tracks that.

New work starts in `research/` and graduates. Nothing is deleted on the way.

## The tests that hold it

`tests/test_docs_structure.py` checks the parts a reader would only find by clicking: every relative markdown link resolves, every main document ends in a `## Status` table, every row in one is pinned, and every research record opens with its banner.
