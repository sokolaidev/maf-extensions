# AGENTS.md

Instructions for AI agents working in this repository. [`CONTRIBUTING.md`](CONTRIBUTING.md) is the human version and has the reasoning; this file is the short, directive form, and does not replace it.

## The rule that matters most

**The pull request title is the changelog entry.** This repository squash-merges, so the title becomes the commit subject, and that subject both decides the next version and is what a reader sees in the release notes. Write it for someone deciding whether to upgrade — not as a summary of the diff.

```
feat: accept a list of arguments to exec, and quote them      ← good
fix(acas): retry the label query when the control plane returns 429
chore: update exec                                            ← says nothing
```

Titles must be [Conventional Commits](https://www.conventionalcommits.org/); CI rejects anything else. The PR-title workflow compares the title with shipped package diffs: a releasing title must contain executable changes in every touched package, while `docs:`/`refactor:`/`test:`/`build:`/`ci:`/`chore:` must not hide executable changes in a package. Repository tooling, workflows, tests, and documentation are not shipped product behavior; their changes use the matching repository title type — except a change confined to `docs/<family>/research/`, which may be `chore:` instead of `docs:`. A record there is a pre-decision argument rather than documentation the suite ships, and it touches no package, so neither type releases anything. Both pass the title check, and `chore:` is the honest subject for it. `feat:` releases a minor, `fix:`/`perf:`/`revert:`/`docs:` a patch, and `refactor:`/`test:`/`build:`/`ci:`/`chore:` release nothing. Scope is optional and free-form — a change is attributed to a package by the files it touches, not by the scope. **Every file under `packages/<name>/` attributes to that package except its own `tests/`**: `src/`, `README.md` and `pyproject.toml` alike, because `release-please-config.json` roots each component at that package. That package's `tests/` is the one exception, listed in its `exclude-paths`, so a change whose only files under a package are its tests neither releases it nor owes it an executable change. A `tests` directory anywhere else — nested under `src/`, say — ships and attributes like any other source.

**So any touch outside the package your title is about inherits your commit's type — not only a drive-by.** Correcting a stale comment in `maf-sandbox-bicep` inside a `feat!:` commit about `maf-sandbox` released bicep 0.5.0, with a changelog announcing a breaking change that package never received. The version that used to recur — **a test added to another package's suite, for a feature that lives in core** — no longer does: a shared conformance suite written that way touches only those packages' `tests/`, which attribute to nobody, so it needs no split. Anything else outside your package still does: commit it **separately**, as `chore:`, which releases nothing — whether it is incidental or something your change genuinely needs.

**That split costs a second pull request, not a second release.** The `chore:` half wires other packages to a surface the `feat:` half adds, so it cannot go green until that half is *merged* — but merged is all it needs, unlike the dependency floor below, which needs the first half **released**. Land the core half, then the wiring, back to back.

**And when the touch outside is not a drive-by but something the package you are working on needs, a dependency floor may only name a version that exists — on PyPI.** The core wheel built from your branch still carries its pre-release version, because release-please bumps versions only in a Release PR, so a dependent declaring `maf-sandbox>=0.16.0` resolves against nothing on the index — however complete the addition to `maf-sandbox` on the same branch is. What a pull request may do is floor on the *prepared* version: `tests.yml` passes `--overrides "$CORE_OVERRIDE"` to its clean-environment install and `--local-core "$CORE_WHEEL"` to the published-cores gate, so a dependent floored on the release this branch cuts is tested against the artifact the branch itself builds. The upload is the step that still requires a published core, and `publish-packages.yml` passes no flag. Land the core half and the dependent half in the order that keeps the index resolvable — [`RELEASING.md`](RELEASING.md) step 4 and [`docs/release-compatibility.md`](docs/release-compatibility.md) carry the gates that make each pairing honest.

**And a samples floor moves on its own once the core is on PyPI ahead of the dependents that admit it.** The bump pull request the release workflow opens carries the packages' ranges and all fifteen `samples/*/agent.py` together, which is right in the ordinary order and wrong in this one: `scripts/check_samples_against_declared_core.py` stops using its `--local-core` escape the moment something published satisfies the floor, and then resolves every sample with the new core pinned from the index and its dependents taken from there too — where each still caps below that core. Every sample naming a dependent goes unsatisfiable, which blocks the dependent Release PRs whose publishing is the fix. Drop the samples' hunk, merge the packages' half, let all of them publish, then move the samples in a `chore:` pull request. `tests/test_sample_metadata.py` permits the samples to name the release before the current one, which is what makes the gap legal.

## Never edit these — they are generated

- `packages/*/CHANGELOG.md`
- `version` in `packages/*/pyproject.toml`
- `.release-please-manifest.json`
- any `chore(main): release …` pull request

release-please owns all four. Adding a changelog entry or bumping a version by hand is the most common mistake an agent makes here: it desynchronises the manifest from the package, which `tests/test_release_config.py` fails on, and it competes with a branch that gets regenerated anyway. A change gets released by merging a Release PR, never by editing a version.

**One exception, and only this one:** correcting the prose of an **already-released** changelog section — a note saying an entry misattributes a change, as `maf-sandbox-bicep` 0.5.0 carries. It touches no version and no manifest entry, so nothing desynchronises, and release-please regenerates only the *unreleased* section, so nothing competes with it. Add a note; never delete the generated entries, which are the honest record of what release-please saw. Commit it as `chore:` — `docs:` releases a patch, so a note about a bad release would ship a release of its own.

## Opening a pull request

1. Run the local checks first — a red PR wastes a review:
   ```bash
   uv sync && uv run poe gate
   # gate = pytest -q → ruff check . → ruff format --check . → every package's strict
   # pyright (enumerated from packages/*/, not listed) → the bare pyright over scripts/,
   # tests/ and samples/, which no per-package pass sees
   ```
   CI runs these **and more**: it builds every wheel, checks their metadata, and installs each one into a clean environment and uses it. Green locally is not the full gate — do not report it as one. Two checks sit outside `gate` because they reach the network: `poe md-blocks`, and `poe sample-floors` if you touched `samples/` — the latter type-checks each sample against the core its own PEP 723 block names, which the workspace pyright pass cannot see.
2. Title it as above.
3. Body: follow [`.github/pull_request_template.md`](.github/pull_request_template.md). Say what changed and why, and name anything a reviewer should look at first. Describe what the code does now — not what you did, and not a narrative of your process.
4. **If the PR closes an issue, update every `## Status` row that tracks it — in this same PR, and name the PR that delivered it.** A row (in `docs/sandbox/*.md` and elsewhere) annotates each reference `(open)`/`(closed)`/`(merged)` and carries a status word; search the docs for the issue's number rather than trusting memory — `git grep -n '#811' -- docs` — because the rows live far from the code and the checker names a missed one only once the request exists to promise the close. Flip what the request closes **as a promise, not a post-merge TODO**: `check_doc_trackers.py` judges a row against the state at *merge* — the closing keywords in your PR's body (`Fixes #N` and its set) count as closed — so the flipped row passes before the merge, and a row left saying `(open)` about an issue the PR closes fails on your PR rather than on the next one's gate. Cite the deliverer in the flipped row, in the form its neighbours carry — `[#811](https://github.com/sokolaidev/maf-extensions/issues/811) (closed) by [#829](https://github.com/sokolaidev/maf-extensions/pull/829) (merged)` — in the PR itself: a request's number exists only once it is open, so the citation is the one edit that lands after opening, and a citation inside the PR cannot be forgotten the way a TODO can. Your PR's own number is scored `MERGED` the same way, so the citation passes with the row it delivers rather than waiting on the merge it blocks. The check lives outside `poe gate` (the gate is hermetic; this one asks GitHub), so run it with a token before opening — `GITHUB_TOKEN=$(gh auth token) uv run python scripts/check_doc_trackers.py`. If a row is only half-shipped (part landed, part deferred to another issue), move only the reference that closed and say what remains — the way a negotiation tracker can stay open beside a shipped split.
5. State plainly what you did **not** verify. An unverified claim in a PR body is worse than an absent one.

**Stop there.** Do not merge, do not push tags, do not approve the `pypi` deployment environment, and do not run the publish workflow. Releasing is the maintainer's, and every one of those steps is irreversible or nearly so.

## Answering a review

Reviews here come from Copilot, and **it hides findings**. A review body can say "generated no new comments" and still carry several, inside a `<details><summary>Suppressed comments (K)</summary>` block. No comments endpoint returns them, nothing marks them resolved, and they are routinely the sharpest findings on the pull request. Read the body whole — `gh api repos/{owner}/{repo}/pulls/N/reviews/REVIEW_ID -q '.body'` — and read the `(K)` before you read the findings: piping that body through `head` or `tail` silently drops the rest, and nothing downstream will ever tell you.

Verify every finding against the source before acting on it. Accept it because it is right and refuse it because it is wrong, and say which; a reviewer that is wrong once is not wrong generally. Where a finding names a consequence, check *that*, not just the line — several here have been right about the defect and wrong about what it breaks.

**Fixing a review finding is the most dangerous commit you will write**, for three reasons worth checking one at a time:

- **The incident rule breaks here more than anywhere.** You have just read a paragraph explaining a defect, and the tempting docstring is the one that retells it. See *Comments and docstrings* below: state the constraint, put the story in the commit.
- **A fix in one spot usually has siblings.** Grep for the shape before you push. A literal copied into a loop, a rule applied on one branch of a flag, a survivor check that hardcodes what the layout derives — each of those has shipped here as one fix that left three instances.
- **Removing the last use of something leaves its declaration behind.** When a fix deletes a call, grep the name: a guest-utility list, a capability table, a README row and a docstring have each gone stale that way.

Reply to each finding separately, on its own thread — never one comment answering several. Say whether it is right, what the consequence was, and for a fix, the commit and what now pins it. For a suppressed finding there is no thread, so post a new review comment on the line it names and label it with the review id, because `K of M` repeats every round.

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
- **Three words, and no fourth.** A **call** is one execution of a tool function — qualify any other kind: `transport call`, `backend call`. A **run** is the host-tools transport's unit, one supervised guest program with a `GuestRunLayout`; there is at most one per call and kinds without host tools have none. A **path** is a place inside the guest, and `guest_` says so, because a bare name reads as host-side. Do not spend `invocation` on any of them — it already means a `docker` or `wslc` subprocess. Two kinds each invented a word for the same thing before this was written down, so reach for one of these before coining. [`docs/sandbox/tool-call.md`](docs/sandbox/tool-call.md) carries the reasoning.
- **An integrity value is a label, never a tier.** *Tier* is spent twice already: `T0` and `T2` are this stack's grounding levels, and FIDES's three-tier priority is what decides where a result's label comes from — so "tier" is ambiguous in the one place where being wrong changes what a host's middleware does. Say **label**, or **level** where the sentence ranks them ("the weakest level over sources only") or where *label* would collide with the framework's `ContentLabel`. A host's *confidentiality* tiers keep the word: that is the host's own vocabulary being described, not a value this suite names. [`docs/sandbox/hosts.md`](docs/sandbox/hosts.md) carries the fold it appears in.

## Comments and docstrings

Comments explain **why**, never what — the code already says what. Match the density of the file you are editing rather than the density you would choose.

Keep them short. Agents reliably overshoot here, and the tell is prose that outgrows the code it explains: a rationale several times the length of the branch below it, an incident retold in full at a call site, a docstring paragraph repeated as an inline comment a line later. Long is not thorough; it is the reader's problem.

- **A docstring says what the function is for and what a caller must know.** One or two sentences for most. Reserve more for a genuine trap — an argument that must be a callable, an ordering that is load-bearing — and state the trap, not its history.
- **Write the reason once, at the level it belongs to.** If the module docstring has it, the function does not repeat it; if the function has it, the inline comment does not.
- **An incident belongs in the issue and the commit message, not in the source.** That covers the bug this code used to have, what an earlier version of it got wrong, and what a review found — `Closes #22` and the commit reach the whole story. State the constraint the code has to hold to; a comment re-telling the defect it replaced ages badly and is read a thousand times more often. The shape to watch for is a comment or docstring that only makes sense to someone who followed the review: "an earlier version of this test…", "this used to search the whole output…", "Copilot caught…". **Watch for it hardest in the commit that answers a review** — that is where the defect is freshest in your head and the retelling reads most like an explanation.
- **Do not annotate the obvious.** No comment above an import, a `return`, or a well-named call.
- **Delete a comment that has become a caption.** If it restates the line beneath it, the line is either clear enough already or should be renamed.
