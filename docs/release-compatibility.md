# Release compatibility

Every dependent declares a range on `maf-sandbox` — `>=0.22.0,<0.24` today — and that range is a promise about artifacts nobody can edit afterwards. PyPI wheels are immutable, so a range published once is a claim this repository has to keep for as long as that wheel is installable. This document is what proves the claim, and where each proof runs.

The suite's design question is not "does the code work together" — the ordinary test suite answers that on every pull request. It is **"does the code work together in the shapes people actually install"**, which is a different pairing and, until recently, a much less examined one.

## The four pairings

A release involves two moving parts, each of which exists as source in this repository and as an artifact on PyPI. That gives four combinations, and they are not equally covered.

| pairing | where it runs | what it proves |
| --- | --- | --- |
| dependent **source** × core **source** | `tests.yml`, every pull request | the code works together. `[tool.uv.sources]` points `maf-sandbox` at the workspace, so this is always the in-tree core |
| dependent **wheel** × core **published** | `published-cores`, at publish and on every pull request | the wheel works with the cores its range admits — the pairing a consumer installs |
| core **wheel** × dependent **published** | `check_published_dependents_work.py`, at publish | every admitting published dependent still *imports* the candidate core |
| core **wheel** × dependent **source** | not directly; see below | that a core release does not break dependent code already adapted but not yet shipped |

The first is comprehensive and has always existed. The second is the one this repository lacked until recently: a dependent's suite had never been run against a core resolved from the index, only against the workspace core, and a range said what it liked without being asked to prove it.

The third is real and blocking, and it is import-only by design — `python -c "import maf_sandbox_<x>"`. A name that still exists but changed its signature, a moved default, a renamed keyword: all import clean and ship.

The fourth is covered incidentally rather than by design. A core Release PR touches `packages/`, so the path classifier calls it code and the full suite runs — which is the first pairing, at the moment of the core release. Nothing states that as a requirement, and a change to the classifier would remove it silently.

## What each gate refuses

**The dependent gate** (`check_dependent_works_with_published_cores.py`) reads the range off the **wheel** rather than off `pyproject.toml`, because the wheel is what ships, then runs that package's whole suite against every published, non-yanked core the range admits — oldest first, because the floor is where a mis-declared range shows itself. It refuses two things: a suite that fails against an admitted core, and a range no published core satisfies at all, which is a wheel uninstallable as declared.

It runs at publish time, in front of the upload, and on every pull request where code changed. The pull request run is not redundant: a Release PR is generated from commits already merged, so a failure discovered there is one whose fix is a *different* pull request. Run on the pull request that introduced the range or the code, and the same failure is an edit to that pull request.

It installs the sibling dependents alongside, deliberately. Two suites reach for one: `maf-sandbox-codeact`'s end-to-end module imports `maf_sandbox_docker`, and `maf-sandbox-docker`'s parity test asserts `maf-sandbox-wslc` is importable rather than skipping. The pairing under test is *this repository's code against a core that is published*, and a sibling is this repository's code. Whether a wheel stands up alone is [`smoke_install.py`](../scripts/smoke_install.py)'s question, asked per package in its own environment.

The whole suite runs, not a subset — the same tests are collected as in an ordinary run. A handful skip that do not skip in the workspace, and both kinds say so themselves: a per-package check that reads `pyproject.toml` next to the installed package, and one that looks for a Dockerfile in the repository. Neither touches the core boundary, and both still run in the ordinary suite.

## What the ranges cost, and why that matters here

`set_dependents_range.target_ceiling` returns `(major, minor + 2)` — one minor of headroom. So every core minor invalidates every dependent's ceiling, and the widening pull request that follows cuts a release of all five whose entire content is a two-character edit. The ceiling is widened on schedule, without anyone checking whether the coming minor breaks anything; the floor is the half that gets examined.

That is the argument for making these gates strong enough to carry the weight the ceiling is pretending to. A guard that is always relaxed on schedule is not guarding, and the thing that could check runs at every publish. Widening the headroom is only honest once something verifies the promise at the moment of release rather than a cycle earlier. The full argument, the measurements behind it and the order the pieces have to land in are in [#628](https://github.com/sokolaidev/maf-extensions/issues/628).

## Two windows that remain open

Both come from the same shape: a gate's verdict is a function of what is published *at the moment it runs*, and the upload happens later — the `publish` job waits on the `pypi` environment's required reviewer, which is a human, so the gap is hours or days rather than seconds.

**A core published during a dependent's approval wait.** The dependent's gate enumerates published cores, passes, and the run halts for approval. A core is released in that gap that the dependent's ceiling admits. The wheel then uploads, and its published range admits a core its suite was never run against. `check_published_dependents_work.py` solves the mirror-image problem by taking its verdict three times — a build-time pass recording what it tested, a pre-upload re-check of only what newly admits, and a post-upload run for the dispatch verdict — and that is the pattern to copy here.

**A core published while adapted-but-unpublished dependent code exists.** The core's gate tests published dependents, which by definition predate the adaptation, so it sees failures that are expected rather than informative. Testing the branch's dependents alongside is what separates *"this break is real and nothing has handled it"* from *"this break is already handled on main and those packages simply need to publish"* — and the second is the sentence a maintainer needs before deciding whether to widen a ceiling.

Neither window is closed today. Both are named in #628 rather than left to be rediscovered.

## Where the rules live

[`RELEASING.md`](../RELEASING.md) is the procedure — the order a core minor goes out in, what the gates refuse, and what to do when a release goes wrong. [`maintainers.md`](maintainers.md) is the plumbing: the environments, the approvals, and what a red gate means in practice. This document is the *why* behind both, and the record of what is proved versus what is merely believed.
