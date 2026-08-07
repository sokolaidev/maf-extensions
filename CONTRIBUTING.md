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

## Changelogs

Every user-visible change gets a line in that package's `CHANGELOG.md`, under the unreleased version's heading. Those entries become the GitHub Release notes verbatim, so write them for someone deciding whether to upgrade: what changed for them, and what they have to do about it. "Refactored internals" helps nobody; "`exec` now accepts a list of arguments, and quotes them for you" does.

## Layering

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

A new backend implements `SandboxBackend`. A new workload (a "kind") is written against the protocol only. Neither should ever import the other — that separation is what makes a workload portable, and it is enforced by the tests above.

## Releases

Maintainers only: [`RELEASING.md`](RELEASING.md). A merged PR does not publish anything; releases go out from tags.
