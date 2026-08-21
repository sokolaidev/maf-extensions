# Contributing

Thanks for looking. These packages are early (`0.x`) and the API may still move, so bug reports and real-world usage notes are as useful as code.

## Getting set up

```bash
uv sync            # one workspace, one lock, every package editable
uv run pytest -q   # the whole suite, about half a minute
uv run python scripts/install_hooks.py # the commit, message and push hooks: lint, format, the scrub guard, pyright
```

`agent-framework-core` resolves from PyPI at the range each package declares — deliberately the same artifact a consumer of the published wheel gets, not a development pin.

`install_hooks.py` writes runtime-resolving wrappers into Git's hooks directory, which is shared by the main checkout and linked worktrees and moves with the clone. It leaves `core.hooksPath` unset, so unrelated hooks already in that directory remain active; it refuses to run when another hooks path is configured. For an existing clone, first run `git config --local --unset core.hooksPath` if it still points at `.githooks`, then inspect `.git/hooks/{pre-commit,pre-push,commit-msg}` and preserve or remove any old pre-commit wrappers before running the installer. The wrappers delegate to `uv run pre-commit`; unlike `uv run pre-commit install`, they do not bake the installing interpreter's absolute path into the hook. The scrub guard's optional owner-only list (`.no-origin-identifiers`) lives in the Git common directory, so one list covers the main checkout and every worktree.

## Before opening a PR

```bash
uv sync            # one workspace, one lock, every package editable
uv run poe gate    # pytest -q, ruff check, ruff format --check, both pyright passes
uv run poe md-blocks   # optional: lint the markdown's python blocks (report-only in CI too)
```

`poe` runs each task through `uv run` itself — it detects the workspace's `uv.lock`. `poe types-packages` enumerates every `packages/*/` carrying its own `[tool.pyright]`, so a new package is covered on the commit that adds it; `poe types` is the bare pass over `scripts/`, `tests/` and `samples/`.

The last line lints the ```python blocks embedded in the markdown against the installed `maf_sandbox*` packages — a renamed export or a removed enum member in a README quickstart fails it. Wiring-only snippets that import none of the packages are skipped, so undefined `router`/`context` and top-level `await` are tolerated. It is report-only in CI for now (`continue-on-error`); the gate flips on once it has stayed green across a release or two ([#289](https://github.com/sokolaidev/maf-extensions/issues/289)).

Type checking comes in two passes. The per-package one is **strict** and covers `src/` only — fixtures and hand-rolled fakes are not where a strict checker's objections are signal. The bare `uv run pyright` is the second: `scripts/`, `tests/` and `samples/` belong to no package, so no `-p` pass reaches them. It runs at *standard*. The test trees relax four rules to warnings for the loose fakes they are made of; `scripts/` and `samples/` relax nothing, and a sample suppresses a single site inline when it has to. `samples/` is in the pass because a sample naming an attribute a package deleted is otherwise caught by nothing until the sample runs for real, which is after a release ([#334](https://github.com/sokolaidev/maf-extensions/issues/334)).

CI runs all of that, plus something worth knowing about: it builds each wheel, installs it into a clean environment and *uses* it. That catches the class of defect no test here can see — a missing `py.typed`, a file the build backend never included, an import that only resolved because the workspace had every sibling on the path.

## What the tests are protecting

Some tests exist to stop a specific mistake, and their failure messages say which. Worth reading rather than working around:

- **`TestOnlyDeclaredDependencies`** — every module imports only the standard library, its own package, or something its `pyproject.toml` declares. An undeclared import works fine here and breaks the first person to `pip install` the package alone.
- **`TestZeroDependencies`** (`maf-sandbox`) — the protocol modules import nothing but the standard library. That layer exists to keep backends and workloads apart; a dependency there defeats it.
- **`TestNoDirectAzureImport`** (`maf-sandbox-bicep`, `maf-sandbox-codeact`) — a workload reaches a sandbox through the protocol, never through a backend, which is what lets the same tool run on Azure, on Docker, or on the in-process fake.
- **`test_conformance_coverage.py`** — a package that implements the pull surface (`stat_file` and `read_file`, with a body rather than a `raise`) has to call `maf_sandbox.conformance`'s FILES_OUT suite from its own tests. Two backends written against the prose alone shipped the same confinement escape, twice each ([#142](https://github.com/sokolaidev/maf-extensions/issues/142)); the probes are what that cost bought, and this keeps a third backend from being held to prose again. It is a wiring check and says so: it proves the call is written, not that it ran, so disabling a conformance test is caught in review rather than here. Every sandbox backend is also held to the FILES_IN, EXEC and FILES_DELETE suites — a withholding backend answers FILES_DELETE with `measure_files_delete_probes` (findings, not promises) or, where no mechanism exists behind the gate, asserts the runner's refusal — and has to carry the static `tuple[SandboxBackend, type[Sandbox]]` binding under `TYPE_CHECKING`, one per discovered backend class (the annotation is what catches a narrowed signature or a missing protocol method, which `isinstance` cannot; [#450](https://github.com/sokolaidev/maf-extensions/issues/450) is the near-miss that made both rules).
- **`test_pr_gate_enumerates.py`** — the CI steps that type-check, build and smoke every package loop over `packages/*/` rather than naming them. A hardcoded list of six is how a seventh package shipped unchecked until someone remembered a line, and the omission looked exactly like success ([#450](https://github.com/sokolaidev/maf-extensions/issues/450)). `publish-packages.yml`'s tag patterns stay listed, and stay out of scope: a tag pattern is a filter GitHub matches, not a list this repository expands.
- **`test_docs_structure.py`** — every relative link under `docs/` resolves, every main document ends in a pinned `## Status` table, and every research record opens with its banner; [`docs/AUTHORING.md`](docs/AUTHORING.md) is the convention it holds you to, and where to start before adding or editing a document there.

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

The PR-title workflow also compares the title with shipped package diffs. A releasing title must contain executable changes in every touched package; `docs:`, `refactor:`, `test:`, `build:`, `ci:` and `chore:` must not hide executable changes in a package. Repository workflows, scripts, tests, and documentation are not shipped product behavior, so use the matching repository title type for those changes. Test-only and documentation-only package edits are not executable for this check.

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
