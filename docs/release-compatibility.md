# Release compatibility

Every dependent declares a range on `maf-sandbox` — `>=0.22.0,<0.24` today — and that range is a promise about artifacts nobody can edit afterwards. PyPI wheels are immutable, so a range published once is a claim this repository has to keep for as long as that wheel is installable. This document is what proves the claim, and where each proof runs.

The suite's design question is not "does the code work together" — the ordinary test suite answers that on every pull request. It is **"does the code work together in the shapes people actually install"**, which is a different pairing and, until recently, a much less examined one.

## The four pairings

A release involves two moving parts, each of which exists as source in this repository and as an artifact on PyPI. That gives four combinations, and they are not equally covered.

| pairing | where it runs | what it proves |
| --- | --- | --- |
| dependent **source** × core **source** | `tests.yml`, every pull request | the code works together. `[tool.uv.sources]` points `maf-sandbox` at the workspace, so this is always the in-tree core |
| dependent **wheel** × core **published** | `published-cores`, at publish and on every pull request | the wheel works with the cores its range admits — the pairing a consumer installs |
| core **wheel** × dependent **published** | `check_core_against_dependents.py`, at publish | every admitting published dependent's own suite, recovered from its release tag, against the candidate core. `check_published_dependents_work.py` runs beside it and proves the weaker thing: that the dependent still *imports* |
| core **wheel** × dependent **source** | `check_core_against_dependents.py`, at publish | the dependents as this checkout has them, built and run against the same core — code already adapted but not yet shipped |

The first is comprehensive and has always existed. The second is the one this repository lacked until recently: a dependent's suite had never been run against a core resolved from the index, only against the workspace core, and a range said what it liked without being asked to prove it.

The third and fourth are one job, and the difference between them is the point. A breaking core makes them disagree: the published half fails because those artifacts predate the adaptation, while the branch half passes because the adaptation is already merged. That reads as *the break is real and already handled — those packages simply have to publish*, which is a different instruction from *nothing has handled this yet*, and the published half alone cannot tell them apart.

The import check stays beside them rather than being replaced. It is cheap, it runs over the same set, and a failure there is a stronger statement than a failing suite: the module would not even load.

A fifth question is not a pairing at all. The dependents are installed **together** — `samples/03` takes acas and codeact beside the core, `samples/11` takes bicep and docker — so a range that moved past a *sibling* rather than past the index leaves the family unresolvable while every per-package check passes. `check_suite_installs_together.py` asks the resolver, per candidate against the published others and as a whole set, and reports what it picked: latest-of-everything and had-to-go-back are both installable, and the difference is drift worth seeing early.

## What each gate refuses

**The dependent gate** (`check_dependent_works_with_published_cores.py`) reads the range off the **wheel** rather than off `pyproject.toml`, because the wheel is what ships, then runs that package's whole suite against every published, non-yanked core the range admits — oldest first, because the floor is where a mis-declared range shows itself. It refuses two things: a suite that fails against an admitted core, and a range no published core satisfies at all, which is a wheel uninstallable as declared.

It runs at publish time, in front of the upload, and on every pull request where code changed. The pull request run is not redundant: a Release PR is generated from commits already merged, so a failure discovered there is one whose fix is a *different* pull request. Run on the pull request that introduced the range or the code, and the same failure is an edit to that pull request.

It installs the sibling dependents alongside, deliberately. Two suites reach for one: `maf-sandbox-codeact`'s end-to-end module imports `maf_sandbox_docker`, and `maf-sandbox-docker`'s parity test asserts `maf-sandbox-wslc` is importable rather than skipping. The pairing under test is *this repository's code against a core that is published*, and a sibling is this repository's code. Whether a wheel stands up alone is [`smoke_install.py`](../scripts/smoke_install.py)'s question, asked per package in its own environment.

The whole suite runs, not a subset — the same tests are collected as in an ordinary run. A handful skip that do not skip in the workspace, and both kinds say so themselves: a per-package check that reads `pyproject.toml` next to the installed package, and one that looks for a Dockerfile in the repository. Neither touches the core boundary, and both still run in the ordinary suite.

## What the ranges cost, and why that matters here

`set_dependents_range.target_ceiling` returns `(major, minor + 2)` — one minor of headroom. So every core minor invalidates every dependent's ceiling, and the widening pull request that follows cuts a release of all five whose entire content is a two-character edit. The ceiling is widened on schedule, without anyone checking whether the coming minor breaks anything; the floor is the half that gets examined.

That is the argument for making these gates strong enough to carry the weight the ceiling is pretending to. A guard that is always relaxed on schedule is not guarding, and the thing that could check runs at every publish. Widening the headroom is only honest once something verifies the promise at the moment of release rather than a cycle earlier. The full argument, the measurements behind it and the order the pieces have to land in are in [#628](https://github.com/sokolaidev/maf-extensions/issues/628).

**Half of that has now happened.** `check_release_order.py` no longer refuses a core release whose version some dependent's ceiling excludes — it reports what follows. That refusal was what put the widening ahead of every core release; without it the widening is an offer a maintainer takes when they want the new core reachable, and declining it is a complete outcome. A core released outside every ceiling is out of reach of everything already installed, which for a breaking release is the point.

**The headroom itself is still one minor**, and that is the remaining decision rather than an oversight. Widening it is what turns a drain every minor into a drain every few, and it is only honest because the gates above now measure the promise at each release. It is left open here because the number is a judgement with a wide blast radius: 28 tests pin the current arithmetic, which is the repository saying that the value is policy rather than a constant.

## The window that remains open

Two were named when this was written and one is now closed. Both came from the same shape: a gate's verdict is a function of what is published *at the moment it runs*, and the upload happens later — the `publish` job waits on the `pypi` environment's required reviewer, which is a human, so the gap is hours or days rather than seconds.

**A core published during a dependent's approval wait.** The dependent's gate enumerates published cores, passes, and the run halts for approval. A core is released in that gap that the dependent's ceiling admits. The wheel then uploads, and its published range admits a core its suite was never run against. `check_published_dependents_work.py` solves the mirror-image problem by taking its verdict three times — a build-time pass recording what it tested, a pre-upload re-check of only what newly admits, and a post-upload run for the dispatch verdict — and that is the pattern to copy here.

**A core published while adapted-but-unpublished dependent code exists.** This one is closed: the core gate runs the branch half beside the published half, so the adaptation on `main` is tested against the candidate core at the moment of release rather than only whenever the classifier last happened to run the ordinary suite.

The first window is still open, and is named in [#628](https://github.com/sokolaidev/maf-extensions/issues/628) rather than left to be rediscovered. The fix is the shape `check_published_dependents_work.py` already uses for its own version of the problem, and it is worth building once for both gates rather than twice.

## Where the rules live

[`RELEASING.md`](../RELEASING.md) is the procedure — the order a core minor goes out in, what the gates refuse, and what to do when a release goes wrong. [`maintainers.md`](maintainers.md) is the plumbing: the environments, the approvals, and what a red gate means in practice. This document is the *why* behind both, and the record of what is proved versus what is merely believed.
