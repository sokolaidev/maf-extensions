# Contributing

Thanks for looking. These packages are early (`0.x`) and the API may still move, so bug reports and real-world usage notes are as useful as code.

## Getting set up

```bash
uv sync            # one workspace, one lock, all three packages editable
uv run pytest -q   # the whole suite, about a second
```

`agent-framework-core` resolves from PyPI at the range each package declares — deliberately the same artifact a consumer of the published wheel gets, not a development pin.

## Before opening a PR

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run pyright -p packages/maf-sandbox        # and -aca, -bicep
```

Type checking is **strict** and per package, and it covers `src/` only — fixtures and hand-rolled fakes are not where a strict checker's objections are signal.

CI runs all of that, plus something worth knowing about: it builds each wheel, installs it into a clean environment and *uses* it. That catches the class of defect no test here can see — a missing `py.typed`, a file the build backend never included, an import that only resolved because the workspace had every sibling on the path.

## What the tests are protecting

Some tests exist to stop a specific mistake, and their failure messages say which. Worth reading rather than working around:

- **`TestOnlyDeclaredDependencies`** — every module imports only the standard library, its own package, or something its `pyproject.toml` declares. An undeclared import works fine here and breaks the first person to `pip install` the package alone.
- **`TestZeroDependencies`** (`maf-sandbox`) — the protocol modules import nothing but the standard library. That layer exists to keep backends and workloads apart; a dependency there defeats it.
- **`TestNoDirectAzureImport`** (`maf-sandbox-bicep`) — the workload reaches a sandbox through the protocol, never through a backend, which is what lets the same tool run on Azure, on Docker, or on the in-process fake.

If a change genuinely needs to cross one of those lines, say so in the PR — the boundary may be wrong, but it should move deliberately.

## PR titles

This repository squash-merges, so a PR title becomes the commit subject on `main` — and that subject is what decides the next version. Titles follow [Conventional Commits](https://www.conventionalcommits.org/), which CI checks:

```
fix(aca): retry the label query when the control plane returns 429
feat: accept a list of arguments to exec, and quote them
docs: explain what the boundary tests protect
```

`feat:` releases a minor version, and `fix:`, `perf:`, `revert:` and `docs:` release a patch. A `!` after the type, or a `BREAKING CHANGE:` footer, releases a minor whatever the type, since every package is still `0.x`. `refactor`, `test`, `build`, `ci` and `chore` release nothing on their own — they are recorded, and ride along with whatever releases next.

`docs:` sits in the releasing set deliberately: a package's `README.md` is its PyPI front page, and publishing a version is the only way to change what is shown there. The rule underneath is simply that anything appearing in a changelog cuts a release, which is `changelog-sections` in `release-please-config.json`.

The scope in parentheses is free-form and optional. Which package a change belongs to is worked out from the files it touches, not from the scope, so a PR touching two packages releases both.

## Changelogs

You do not edit `CHANGELOG.md` in an ordinary PR. [release-please](https://github.com/googleapis/release-please) keeps a Release PR open per package and writes the section there, generated from commit subjects — which is exactly why **that section is a draft to rewrite before the Release PR is merged**.

Generated entries read like `* fix: correct the label digest (#42)`. What ships should read like something written for the person deciding whether to upgrade: what changed for them, and what they have to do about it. "Refactored internals" helps nobody; "`exec` now accepts a list of arguments, and quotes them for you" does. Those entries become the GitHub Release notes verbatim.

## Layering

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

A new backend implements `SandboxBackend`. A new workload (a "kind") is written against the protocol only. Neither should ever import the other — that separation is what makes a workload portable, and it is enforced by the tests above.

## Releases

Maintainers only: [`RELEASING.md`](RELEASING.md). A merged PR does not publish anything; releases go out from tags.
