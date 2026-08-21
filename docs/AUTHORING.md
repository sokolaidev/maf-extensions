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

One row per decision — what it is, whether it is implemented, and a tracking reference: a shipped PR, an open issue, or an explicit `untracked`. A row with an empty tracking cell is the failure this convention exists to prevent, since it is how a document quietly drifts from the code.

Decided but unimplemented content goes in the body like everything else, in the same present tense, with an open tracker row saying it has not shipped yet. Do not hedge the prose instead; the row is the honest place for that, and the prose stops being a design the moment it starts qualifying itself.

## Writing a record under `research/`

`<family>/research/` is the pre-decision pipeline: explorations that ask whether something is worth doing, and proposals that argue for a specific shape. Open each with a banner blockquote saying which it is and what it covers.

Start new work there and let it graduate. When a decision is made, **move** the decided content out into the main documents and **leave** the record behind — kept in the tense it was written, as the argument rather than a description of the code, its banner extended with a short line naming where the decided content now lives. A record is never edited to match what shipped; the main document is what tracks that. Nothing is deleted on the way.

## Diagrams

Hand-author the SVG and commit it to `<family>/assets/`. There is no build step and no rendering service, so the picture is reviewable in the diff like the prose is. Share one house palette across a family, so two diagrams of the same system read as the same system rather than two drawings of it. Every file carries a `<title>` and a `<desc>` wired through `role="img"` and `aria-labelledby`, and the embed carries a long alt that narrates the whole picture — someone who cannot see it should come away with the argument, not a caption. Where the text already has an ascii sketch, keep it beside the SVG: the terse form and the full one do different jobs, and the terse one is what a reader quotes.

## What the structure test enforces

[`tests/test_docs_structure.py`](../tests/test_docs_structure.py) checks the parts a reader would only find by clicking, so a mistake fails in CI rather than surviving as a dead link:

- every relative markdown link resolves on disk,
- every main document ends in a `## Status` heading with a table under it,
- every tracker row pins something,
- every record under `research/` opens with its banner inside the first five lines,
- no main document carries the old `Status:` banner grammar — location conveys status now, and the table carries the detail.

The front door and the records are exempt from the `## Status` rule, and so is anything outside a family directory, this file included.
