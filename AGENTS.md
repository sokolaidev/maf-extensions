# AGENTS.md

Instructions for AI agents working in this repository. [`CONTRIBUTING.md`](CONTRIBUTING.md) is the human version and has the reasoning; this file is the short, directive form, and does not replace it.

## The rule that matters most

**The pull request title is the changelog entry.** This repository squash-merges, so the title becomes the commit subject, and that subject both decides the next version and is what a reader sees in the release notes. Write it for someone deciding whether to upgrade — not as a summary of the diff.

```
feat: accept a list of arguments to exec, and quote them      ← good
fix(aca): retry the label query when the control plane returns 429
chore: update exec                                            ← says nothing
```

Titles must be [Conventional Commits](https://www.conventionalcommits.org/); CI rejects anything else. `feat:` releases a minor, `fix:`/`perf:`/`revert:`/`docs:` a patch, and `refactor:`/`test:`/`build:`/`ci:`/`chore:` release nothing. Scope is optional and free-form — a change is attributed to a package by the files it touches, not by the scope.

## Never edit these — they are generated

- `packages/*/CHANGELOG.md`
- `version` in `packages/*/pyproject.toml`
- `.release-please-manifest.json`
- any `chore(main): release …` pull request

release-please owns all four. Adding a changelog entry or bumping a version by hand is the most common mistake an agent makes here: it desynchronises the manifest from the package, which `tests/test_release_config.py` fails on, and it competes with a branch that gets regenerated anyway. A change gets released by merging a Release PR, never by editing a version.

## Opening a pull request

1. Run the local checks first — a red PR wastes a review:
   ```bash
   uv sync && uv run pytest -q && uv run ruff check . && uv run ruff format --check .
   uv run pyright -p packages/maf-sandbox && uv run pyright -p packages/maf-sandbox-aca && uv run pyright -p packages/maf-sandbox-bicep
   ```
   CI runs these **and more**: it builds every wheel, checks their metadata, and installs each one into a clean environment and uses it. Green locally is not the full gate — do not report it as one.
2. Title it as above.
3. Body: follow [`.github/pull_request_template.md`](.github/pull_request_template.md). Say what changed and why, and name anything a reviewer should look at first. Describe what the code does now — not what you did, and not a narrative of your process.
4. State plainly what you did **not** verify. An unverified claim in a PR body is worse than an absent one.

**Stop there.** Do not merge, do not push tags, do not approve the `pypi` deployment environment, and do not run the publish workflow. Releasing is the maintainer's, and every one of those steps is irreversible or nearly so.

## Filing an issue

Use a template from [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) — `bug.md` or `feature_request.md` — and fill every section or delete it. Never leave the placeholder text in place. A bug report must name the package, its version, and the Python version. One issue per problem.

## This repository is public

These packages were extracted from a private application. Never name that repository, link to it, cite its issue or PR numbers, or reproduce host paths, internal URLs or infrastructure identifiers — in code, comments, tests, documentation, commit messages, issues or PR descriptions. This was scrubbed once already; do not reintroduce it. Issue and PR numbers **of this repository** are fine.

## Layering, which the tests enforce

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

A new backend implements `SandboxBackend`. A new workload — a "kind" — is written against the protocol only. `maf-sandbox`'s protocol modules import nothing but the standard library, and every module imports only what its own `pyproject.toml` declares. `TestZeroDependencies`, `TestNoDirectAzureImport` and `TestOnlyDeclaredDependencies` will fail if you cross those lines. If a change genuinely needs to, say so in the PR rather than working around the test.

Each package is self-contained: its own metadata, `ruff`/`pyright`/`pytest` configuration, `LICENSE` and `CHANGELOG.md`. The workspace root does not reach into packages, and an sdist has no root to inherit from.

## Smaller things

- Pin every GitHub Action by commit SHA with the version in a trailing comment. This repository publishes to PyPI; a moving tag is a supply-chain hole.
- Do not wrap lines in Markdown. One paragraph is one line.

## Comments and docstrings

Comments explain **why**, never what — the code already says what. Match the density of the file you are editing rather than the density you would choose.

Keep them short. Agents reliably overshoot here, and the tell is prose that outgrows the code it explains: a rationale several times the length of the branch below it, an incident retold in full at a call site, a docstring paragraph repeated as an inline comment a line later. Long is not thorough; it is the reader's problem.

- **A docstring says what the function is for and what a caller must know.** One or two sentences for most. Reserve more for a genuine trap — an argument that must be a callable, an ordering that is load-bearing — and state the trap, not its history.
- **Write the reason once, at the level it belongs to.** If the module docstring has it, the function does not repeat it; if the function has it, the inline comment does not.
- **An incident belongs in the issue and the commit message, not in the source.** `Closes #22` reaches the whole story. A comment that re-tells it ages badly and is read a thousand times more often.
- **Do not annotate the obvious.** No comment above an import, a `return`, or a well-named call.
- **Delete a comment that has become a caption.** If it restates the line beneath it, the line is either clear enough already or should be renamed.
