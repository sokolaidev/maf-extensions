# AGENTS.md

Instructions for AI agents working in this repository. [`CONTRIBUTING.md`](CONTRIBUTING.md) is the human version and has the reasoning; this file is the short, directive form, and does not replace it.

## The rule that matters most

**The pull request title is the changelog entry.** This repository squash-merges, so the title becomes the commit subject, and that subject both decides the next version and is what a reader sees in the release notes. Write it for someone deciding whether to upgrade — not as a summary of the diff.

```
feat: accept a list of arguments to exec, and quote them      ← good
fix(acas): retry the label query when the control plane returns 429
chore: update exec                                            ← says nothing
```

Titles must be [Conventional Commits](https://www.conventionalcommits.org/); CI rejects anything else. `feat:` releases a minor, `fix:`/`perf:`/`revert:`/`docs:` a patch, and `refactor:`/`test:`/`build:`/`ci:`/`chore:` release nothing. Scope is optional and free-form — a change is attributed to a package by the files it touches, not by the scope. **Every file under `packages/<name>/` attributes to that package**: `src/`, `tests/`, `README.md` and `pyproject.toml` alike, because `release-please-config.json` roots each component at the package directory. A test is not a lesser touch than a source file.

**So any touch outside the package your title is about inherits your commit's type — not only a drive-by.** Correcting a stale comment in `maf-sandbox-bicep` inside a `feat!:` commit about `maf-sandbox` released bicep 0.5.0, with a changelog announcing a breaking change that package never received. The version that recurs looks nothing like a stray: **a test added to another package's suite, for a feature that lives in core.** A shared conformance suite is written exactly that way, so a `feat(sandbox):` whose only paths in two backends were `tests/` announced that feature under *Features* in both their changelogs, with neither package's `src/` changed. Commit the touch outside **separately**, as `chore:`, which releases nothing — and do it whether the touch is incidental or something your change genuinely needs.

**That split costs a second pull request, not a second release.** The `chore:` half wires other packages to a surface the `feat:` half adds, so it cannot go green until that half is *merged* — but merged is all it needs, unlike the dependency floor below, which needs the first half **released**. Land the core half, then the wiring, back to back.

**And when the touch outside is not a drive-by but something the package you are working on needs, splitting it is not enough — the first half has to be *released* before the second can go green.** A dependency floor may only name a version that exists, and CI installs every built wheel into a clean environment to prove it. The core wheel built from your branch still carries its pre-release version, because release-please bumps versions only in a Release PR, so a dependent declaring `maf-sandbox>=0.16.0` resolves against nothing — not the wheel beside it, not PyPI — however complete the addition to `maf-sandbox` on the same branch is. Land the `maf-sandbox` half on its own, with its own title; adopt it and raise the floor afterwards, as [`RELEASING.md`](RELEASING.md) step 4 describes.

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
   CI runs these **and more**: it builds every wheel, checks their metadata, and installs each one into a clean environment and uses it. Green locally is not the full gate — do not report it as one.
2. Title it as above.
3. Body: follow [`.github/pull_request_template.md`](.github/pull_request_template.md). Say what changed and why, and name anything a reviewer should look at first. Describe what the code does now — not what you did, and not a narrative of your process.
4. State plainly what you did **not** verify. An unverified claim in a PR body is worse than an absent one.

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
- **Three words, and no fourth.** A **call** is one execution of a tool function — qualify any other kind: `transport call`, `backend call`. A **run** is the host-tools transport's unit, one supervised guest program with a `GuestRunLayout`; there is at most one per call and kinds without host tools have none. A **path** is a place inside the guest, and `guest_` says so, because a bare name reads as host-side. Do not spend `invocation` on any of them — it already means a `docker` or `wslc` subprocess. Two kinds each invented a word for the same thing before this was written down, so reach for one of these before coining. [`docs/design/call-lifetime.md`](docs/design/call-lifetime.md) carries the reasoning.

## Comments and docstrings

Comments explain **why**, never what — the code already says what. Match the density of the file you are editing rather than the density you would choose.

Keep them short. Agents reliably overshoot here, and the tell is prose that outgrows the code it explains: a rationale several times the length of the branch below it, an incident retold in full at a call site, a docstring paragraph repeated as an inline comment a line later. Long is not thorough; it is the reader's problem.

- **A docstring says what the function is for and what a caller must know.** One or two sentences for most. Reserve more for a genuine trap — an argument that must be a callable, an ordering that is load-bearing — and state the trap, not its history.
- **Write the reason once, at the level it belongs to.** If the module docstring has it, the function does not repeat it; if the function has it, the inline comment does not.
- **An incident belongs in the issue and the commit message, not in the source.** That covers the bug this code used to have, what an earlier version of it got wrong, and what a review found — `Closes #22` and the commit reach the whole story. State the constraint the code has to hold to; a comment re-telling the defect it replaced ages badly and is read a thousand times more often. The shape to watch for is a comment or docstring that only makes sense to someone who followed the review: "an earlier version of this test…", "this used to search the whole output…", "Copilot caught…". **Watch for it hardest in the commit that answers a review** — that is where the defect is freshest in your head and the retelling reads most like an explanation.
- **Do not annotate the obvious.** No comment above an import, a `return`, or a well-named call.
- **Delete a comment that has become a caption.** If it restates the line beneath it, the line is either clear enough already or should be renamed.
