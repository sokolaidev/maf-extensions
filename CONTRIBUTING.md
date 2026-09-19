# Contributing

Thanks for looking. These packages are early (`0.x`) and the API may still move, so bug reports and real-world usage notes are as useful as code.

## Getting set up

On Windows, use PowerShell 7 (`pwsh`) and a native Python; the workflow behavior checks need neither WSL nor Git Bash:

```powershell
uv sync --locked
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
./scripts/check_workflows.ps1 -Python "$PWD/.venv/Scripts/python.exe" -TestArgs '-q', '-ra'
```

The wrapper discovers tests marked `workflow` under `tests/`, runs the release and retry behavior shared with the production workflows, and preserves Python's exit status. Mark new workflow-behavior test modules with `pytestmark = pytest.mark.workflow` so they join the Windows and Linux checks automatically. The full local gate is `uv run poe gate`, also run from PowerShell. Bash integration checks run only on Linux and skip explicitly on Windows, even when a WSL launcher is on `PATH`. The portable workflow CI runs these checks on Windows and Linux.

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
uv run poe gate    # pytest -q, ruff check, ruff format --check, both pyright passes, doc references, commas
uv run poe md-blocks   # optional: lint the markdown's python blocks (report-only in CI too)
uv run poe sample-floors   # optional: type-check each sample against the core its block names
```

`poe` runs each task through `uv run` itself — it detects the workspace's `uv.lock`. `poe types-packages` enumerates every `packages/*/` carrying its own `[tool.pyright]`, so a new package is covered on the commit that adds it; `poe types` is the bare pass over `scripts/`, `tests/` and `samples/`.

The last line lints the ```python blocks embedded in the markdown against the installed `maf_sandbox*` packages — a renamed export or a removed enum member in a README quickstart fails it. Wiring-only snippets that import none of the packages are skipped, so undefined `router`/`context` and top-level `await` are tolerated. It is report-only in CI for now (`continue-on-error`); the gate flips on once it has stayed green across a release or two ([#289](https://github.com/sokolaidev/maf-extensions/issues/289)).

The gate also refuses two literals sitting side by side inside a list, tuple or set. Python joins them, so a comma left out leaves the collection one element shorter than it reads, and ruff has no rule for the form that spans lines. Wrap a message written across lines in parentheses when it is one value: that says so at the point of ambiguity, and it is what the CodeQL query behind the bot's review comment exempts as well.

Type checking comes in two passes. The per-package one is **strict** and covers `src/` only — fixtures and hand-rolled fakes are not where a strict checker's objections are signal. The bare `uv run pyright` is the second: `scripts/`, `tests/` and `samples/` belong to no package, so no `-p` pass reaches them. It runs at *standard*. The test trees relax four rules to warnings for the loose fakes they are made of; `scripts/` and `samples/` relax nothing, and a sample suppresses a single site inline when it has to. `samples/` is in the pass because a sample naming an attribute a package deleted is otherwise caught by nothing until the sample runs for real, which is after a release ([#334](https://github.com/sokolaidev/maf-extensions/issues/334)).

CI runs all of that, plus something worth knowing about: it builds each wheel, installs it into a clean environment and *uses* it. That catches the class of defect no test here can see — a missing `py.typed`, a file the build backend never included, an import that only resolved because the workspace had every sibling on the path.

## Adding a sample

A numbered sample is a consumer of published packages and a live verification target. Add its program, workflow wiring, evidence checker and tests together. [Sample 18](samples/18_acas_drawio_repair/) and its `sample-18` job in [Verify (live)](.github/workflows/verify-live.yml) provide a complete example, including artifact cleanup and per-call timing logs.

### Program and dependencies

Create the next numbered directory under [samples/](samples/README.md), with `agent.py`, a README and any input fixtures. Declare every runtime dependency in the agent's PEP 723 block, including dependencies imported by helper modules. Use the same `maf-sandbox>=...` floor as the other numbered samples. Floors must name published versions; workspace imports and local wheel overrides do not establish that a consumer can install them. Keep an example requiring unpublished packages under `samples/experimental/` until publication, then promote it and add normal live verification. Do not change generated versions or changelogs to make it installable.

Copy the canonical [_scaffold.py](samples/01_acas_bicep/_scaffold.py) unchanged. Use its installed-version report, `quoted` for model output, and `MEASURED` or `evidence` for host-established facts. For a hosted backend, derive a module constant whose name contains `THREAD` from `conversation_id("sample-18")`, substituting the new sample's name; this keeps concurrent workflow runs from purging each other's sandboxes. Put execution behind the main guard so importing the sample creates no resources.

Success must establish the workload's result, not just a model's claim or exit code zero. For stored outputs, verify actual bytes and read-back, track ownership of each destination, and clean up files and sandboxes on success, failure and cancellation. Report incomplete cleanup as failure. When reporting tool latency, use core's `SandboxObserver.tool_call_ended` and `ToolCallEnded.seconds`; it includes the tool body and cleanup, and is distinct from model latency or the whole sample's elapsed time.

### Workflow wiring

1. Add a job to [verify-live.yml](.github/workflows/verify-live.yml). Select it when `inputs.package` is empty or names a package the sample exercises: core, the backend, the kind and any adapter it uses. Normal sample jobs run in both `published` and `branch` modes; do not add a branch-only condition. Billable execution belongs in live verification, not ordinary pull-request tests.
2. If a package joins live verification for the first time, add it to all three matching filters in [publish-packages.yml](.github/workflows/publish-packages.yml): `wait-for-propagation`, `train-status` and `verify`. Keep their conditions identical. A job in the called workflow alone does not make that package's releases dispatch it.
3. Follow the existing source-selection and harness pattern. On release tags, check out the default branch's `scripts/` into `.harness`; run checkers through `$HARNESS`, while the sample stays at the ref under test. Wait with `await_live_version.py` when a release version is supplied. Obtain `$source_args` from [sample_source_args.py](scripts/sample_source_args.py), then run `uv run --no-project $source_args` on the agent. Published mode must resolve from PyPI; do not substitute `uv sync` and workspace execution. Retain the `check_live_versions.py` assertion for published runs with a supplied version, and skip that assertion in branch mode.
4. Add a standard-library-only checker under `scripts/` for the sample's evidence; [check_live_drawio_sample.py](scripts/check_live_drawio_sample.py) is an example. Read host-tagged records and reject missing results, wrong artifact attribution, invalid timings and incomplete cleanup. Use `set -euo pipefail` with `tee` so logging cannot hide a failed sample. Keep timing records visible on successful runs, and retain logs with an `always()` artifact step when later inspection is needed. Pin every action to a commit SHA.
5. For ACAS, make the job depend on `acas-images`, extend that job's package filter and configuration, and update [check_acas_live_images.py](scripts/check_acas_live_images.py). Check only images used by the selected jobs and source under test, including older release tags that lack the new sample. When the checker starts requiring an environment variable, pass it to the daily preflight in [conformance-live.yml](.github/workflows/conformance-live.yml) as well — its env block must cover everything `required_images` demands. Build and import required images before verification; do not widen guest egress to install prerequisites at runtime. Use the existing `live-verify` environment and OIDC login. Keep actual resource names, endpoints and identifiers out of repository files.
6. Add the sample to [samples/README.md](samples/README.md). Update [docs/maintainers.md](docs/maintainers.md) for prerequisites, environment variables, coverage and billable resource counts. If the release dispatch package set changed, update the matching paragraph in [RELEASING.md](RELEASING.md) as well.

### Tests and verification

Put sample integration tests in root `tests/`; do not make a kind's unit suite import backend or other sibling packages to test application wiring. Keep offline tests independent of Azure credentials and real model calls. An optional live pytest entry point must skip unless explicitly enabled; the normal workflow job still runs the sample directly in its declared environment.

| Check | What to cover or update |
| --- | --- |
| [test_sample_metadata.py](tests/test_sample_metadata.py) | Numbered samples are discovered automatically. Dependencies must cover imports, share the core floor and name released versions. |
| [test_sample_scaffold.py](tests/test_sample_scaffold.py) | Keep scaffold copies identical. Add a hosted sample to the expected hosted-sample list and use the conversation naming convention. |
| [test_sample_modules_import.py](tests/test_sample_modules_import.py) | Every module must import without live configuration. Tests loading `_scaffold` must restore `sys.modules` so another sample cannot inherit it. |
| Sample and evidence tests | Exercise the real converter where practical, plus rejection, repair, output ownership, cleanup failure and cancellation. Test that model-authored or incomplete evidence cannot satisfy the checker. |
| [test_verify_live_harness.py](tests/test_verify_live_harness.py) | The shared checks discover direct sample jobs and verify harness checkout, source selection, index waiting and installed-version assertions. |
| [test_check_acas_live_images.py](tests/test_check_acas_live_images.py) | Update consumer sets, package-selection expectations and fake image inventories. Cover missing images and older source trees. |
| [test_drawio_live_workflow.py](tests/test_drawio_live_workflow.py) and [test_release_config.py](tests/test_release_config.py) | Use the former as a pattern for the new job's trigger, log and failure-propagation checks. The latter keeps release dispatch filters and documentation aligned. Mark new workflow test modules with `pytestmark = pytest.mark.workflow`. |

Run the affected tests first, then `uv run poe gate`, `uv run poe md-blocks` and `uv run poe sample-floors`. The workspace type check alone does not verify the sample's published dependency floor. Stage new or moved documentation before checking paths, because [check_doc_paths.py](scripts/check_doc_paths.py) enumerates tracked files. On Windows, [check_workflows.ps1](scripts/check_workflows.ps1) runs the portable workflow checks; Bash-only checks require Linux.

Finally, run the published-package sample against its real prerequisites and feed the saved log to its checker. Verify cleanup and the installed-version report. A branch live run answers a separate question about checkout code; record which mode was verified and whether execution happened locally or in GitHub Actions. Do not describe local live verification as a completed CI run.

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

The PR-title workflow also compares the title with shipped package diffs. A releasing title must contain executable changes in every touched package; `docs:`, `refactor:`, `test:`, `build:`, `ci:` and `chore:` must not hide executable changes in a package. Repository workflows, scripts, tests, and documentation are not shipped product behavior, so use the matching repository title type for those changes. Documentation-only package edits are not executable for this check, and a package's own `tests/` does not count as touching it at all.

`docs:` sits in the releasing set deliberately: a package's `README.md` is its PyPI front page, and publishing a version is the only way to change what is shown there. The rule underneath is simply that anything appearing in a changelog cuts a release, which is `changelog-sections` in `release-please-config.json`.

The scope in parentheses is free-form and optional. Which package a change belongs to is worked out from the files it touches, not from the scope, so a PR touching two packages' shipped files releases both. A package's own `tests/` is excluded from that — changing it alone neither releases the package nor obliges the title to prove an executable change in it.

**Releasing both is usually right, so a change spanning a package and something that depends on it goes in one PR.** Wiring a backend or a kind to a surface the same PR adds to `maf-sandbox`, and moving the bound that surface requires, are changes that package really received. If you move that bound, **move both of its ends**: a package declares one string, `maf-sandbox>=0.35.0,<0.36` today, so raising only the floor to a coming 0.36 leaves `>=0.36,<0.36` — a range nothing can satisfy, before or after that core exists. Nothing here asks you to order those releases; that is a maintainer's job, in [`RELEASING.md`](RELEASING.md). Two consequences to accept: the type and any `!` reach every package the PR touches, so a `feat!:` marks a dependent breaking when all it did was adapt — and an **incidental** touch does not get the same excuse. A stale comment or a tidy in a package your change does not depend on drags that package into the release under a changelog line describing something it never received, so it goes in a **pull request of its own**, titled `chore:`. A separate commit inside this one would not save it: the squash merge makes this PR's title the commit subject for every package it touches.

## Changelogs

Nobody writes one. `CHANGELOG.md` is assembled from the titles above by [release-please](https://github.com/googleapis/release-please), which keeps a Release PR open per package and files each entry under its type. That section becomes the GitHub Release notes verbatim — so the quality of a release's notes is decided when you name your PR, and nowhere else.

The one thing a single line cannot carry is what a reader has to *do* about a change. That has its own slot: a `BREAKING CHANGE: …` footer, which release-please renders into its own `⚠ BREAKING CHANGES` section above everything else. Add it in GitHub's squash-commit message box when you merge — the body is blank by default, so the footer is all that ends up there.

## Layering

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

A new backend implements `SandboxBackend`. A new workload (a "kind") is written against the protocol only. Neither should ever import the other — that separation is what makes a workload portable, and it is enforced by the tests above.

A dependant's unit tests must not reference another dependant: those tests also run in isolated environments where only that package and its declared dependencies are installed. Cross-package coverage belongs in the repository-level `tests/` suite. Opt-in live integration tests are separate from this unit-test boundary.

## Releases

Maintainers only: [`RELEASING.md`](RELEASING.md). A merged PR does not publish anything; releases go out from tags.
