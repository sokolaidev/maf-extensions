# Authoring these documents

How a document under `docs/` is shaped, so the next one lands in the same shape rather than inventing one. [`README.md`](README.md) is the reader's way in; this is for whoever is writing.

## One directory per extension family

Put everything about one extension suite in `docs/<family>/`. Today there is one, [`sandbox/`](sandbox/README.md). Give a second family its own directory beside it — never a section inside an existing one, however much the two overlap.

Leave [`maintainers.md`](maintainers.md) outside the families. Release plumbing — trusted publishing, the release train, the order the packages go out in — is repo-wide and belongs to no suite.

## What a family holds

- `README.md` — the front door, for someone who has not met the suite before, ending in the map of everything beside it.
- the main documents — one per axis or boundary. Give a group its own subdirectory once it outgrows a page (`kinds/`, `backends/`).
- `research/` — the records.

## Writing a main document

A main document is the decided design. Write it in the present tense, describing the target as though it were the whole truth: a document hedged into a description of the current commit stops being a design and starts being a changelog.

End it with a `## Status` table, and that is where the tense is paid for:

```
| Decision | State | Tracking |
```

One row per decision — what it is, whether it is implemented, and a tracking reference: a shipped PR, an open issue, or an explicit `untracked`. A row with an empty tracking cell is the failure this convention exists to prevent, since it is how a document quietly drifts from the code. A group's index README — [`sandbox/kinds/README.md`](sandbox/kinds/README.md), [`sandbox/backends/README.md`](sandbox/backends/README.md) — is the exception, and pins through the page that owns the subject rather than through an issue: its State cell carries the fact in prose and its Tracking cell is a relative `.md` link to whichever per-item page or sibling document holds the issue trail, so the index stays self-sufficient for a reader who never leaves the tree. Move the reference into the owning page before you drop it from the index; nothing is deleted on the way.

Decided but unimplemented content goes in the body like everything else, in the same present tense, with an open tracker row saying it has not shipped yet. Do not hedge the prose instead; the row is the honest place for that, and the prose stops being a design the moment it starts qualifying itself.

## Writing a record under `research/`

`<family>/research/` is the pre-decision pipeline: explorations that ask whether something is worth doing, and proposals that argue for a specific shape. Open each with a banner blockquote saying which it is and what it covers.

Start new work there and let it graduate. When a decision is made, **move** the decided content out into the main documents and **leave** the record behind — kept in the tense it was written, as the argument rather than a description of the code, its banner extended with a short line naming where the decided content now lives. A record is never edited to match what shipped; the main document is what tracks that. Nothing is deleted on the way.

## Citing a line

Write a line reference as a code span holding nothing else — `` `testing.py:181` `` — and put the name it points at beside it. That name is the whole check: [`check_doc_paths.py`](../scripts/check_doc_paths.py) requires the line to **begin the definition** of the name written before it, because a line number is otherwise the one reference that never 404s. It goes stale when somebody edits the source, and it still lands on a real line of a real file, so the page keeps looking maintained while it sends its reader into the middle of some other function ([#746](https://github.com/sokolaidev/maf-extensions/issues/746)).

Three rules follow. Name **one** line and not a range, since only the line a definition starts on can be derived. **Link** the reference when the basename is shared — four files here are called `_backend.py`, and a reader sent to one of them has been told nothing. And leave the file off with a bare `` `:187` `` to continue the one named before it in the same paragraph, which is how a page walks a class member by member; a bare number opening a paragraph names nothing and is not read as a reference at all.

A record under `research/` carries no line numbers. It is never edited to match what shipped, so a number in one cannot be repaired once the code moves — it rots by construction. Name the symbol and quote the code, which is what the argument rested on anyway.

## Diagrams

Hand-author the SVG and commit it to `<family>/assets/`. There is no build step and no rendering service, so the picture is reviewable in the diff like the prose is. Share one house palette across a family, so two diagrams of the same system read as the same system rather than two drawings of it. Every file carries a `<title>` and a `<desc>` wired through `role="img"` and `aria-labelledby`, and the embed carries a long alt that narrates the whole picture — someone who cannot see it should come away with the argument, not a caption. Where the text already has an ascii sketch, keep it beside the SVG: the terse form and the full one do different jobs, and the terse one is what a reader quotes.

## What the structure test enforces

[`tests/test_docs_structure.py`](../tests/test_docs_structure.py) checks the parts a reader would only find by clicking, so a mistake fails in CI rather than surviving as a dead link:

- every relative markdown link resolves on disk,
- every main document ends in a `## Status` heading with a table under it,
- every tracker row pins something, and the two index READMEs pin through a page rather than an issue,
- every record under `research/` opens with its banner inside the first five lines,
- no record under `research/` cites a line number,
- no main document carries the old `Status:` banner grammar — location conveys status now, and the table carries the detail.

The front door and the records are exempt from the `## Status` rule, and so is anything outside a family directory, this file included.
