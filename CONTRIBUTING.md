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
uv run pyright -p packages/maf-sandbox        # and -acas, -bicep, -wslc
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

**Your PR title is the changelog entry.** This repository squash-merges, so it becomes the commit subject on `main`, and that subject both decides the next version and is what a reader sees in the release notes. Write it for the person deciding whether to upgrade — what changed for them, not what you did to the code. "Refactored internals" helps nobody; "accept a list of arguments to `exec`, and quote them" does.

Titles follow [Conventional Commits](https://www.conventionalcommits.org/), which CI checks:

```
fix(acas): retry the label query when the control plane returns 429
feat: accept a list of arguments to exec, and quote them
docs: explain what the boundary tests protect
```

`feat:` releases a minor version, and `fix:`, `perf:`, `revert:` and `docs:` release a patch. A `!` after the type, or a `BREAKING CHANGE:` footer, releases a minor whatever the type, since every package is still `0.x`. `refactor`, `test`, `build`, `ci` and `chore` release nothing on their own — they are recorded, and ride along with whatever releases next.

`docs:` sits in the releasing set deliberately: a package's `README.md` is its PyPI front page, and publishing a version is the only way to change what is shown there. The rule underneath is simply that anything appearing in a changelog cuts a release, which is `changelog-sections` in `release-please-config.json`.

The scope in parentheses is free-form and optional. Which package a change belongs to is worked out from the files it touches, not from the scope, so a PR touching two packages releases both.

## Changelogs

Nobody writes one. `CHANGELOG.md` is assembled from the titles above by [release-please](https://github.com/googleapis/release-please), which keeps a Release PR open per package and files each entry under its type. That section becomes the GitHub Release notes verbatim — so the quality of a release's notes is decided when you name your PR, and nowhere else.

The one thing a single line cannot carry is what a reader has to *do* about a change. That has its own slot: a `BREAKING CHANGE: …` footer, which release-please renders into its own `⚠ BREAKING CHANGES` section above everything else. Add it in GitHub's squash-commit message box when you merge — the body is blank by default, so the footer is all that ends up there.

## Layering

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

A new backend implements `SandboxBackend`. A new workload (a "kind") is written against the protocol only. Neither should ever import the other — that separation is what makes a workload portable, and it is enforced by the tests above.

## Releases

Maintainers only: [`RELEASING.md`](RELEASING.md). A merged PR does not publish anything; releases go out from tags.
