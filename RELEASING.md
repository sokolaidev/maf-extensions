# Releasing

Each package releases on its own, from a tag that names it. Publishing runs on [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/): there is no API token in this repository, its secrets, or anyone's shell history.

One-time setup — the PyPI organization, the trusted publishers, the GitHub environments — is done, and documented in [`docs/maintainers.md`](docs/maintainers.md). You need that only when adding a package.

## What decides the version

[release-please](https://github.com/googleapis/release-please) watches `main` and keeps a **Release PR** open for every package that has unreleased changes. It works out the bump from the merged commit subjects — which are PR titles here, since this repository squash-merges — and attributes each change to a package by the files it touched. `feat:` bumps the minor; `fix:`, `perf:`, `revert:` and `docs:` bump the patch; a `!` or a `BREAKING CHANGE:` footer bumps the minor whatever the type, because every package is still `0.x`. `refactor`, `test`, `build`, `ci` and `chore` release nothing on their own. The rule behind that list is that any commit which earns a changelog entry earns a release, so it is `changelog-sections` in `release-please-config.json` that decides it.

## Cutting a release

There are two steps, and the first one is the decision.

1. **Review the Release PR, then merge it.** It carries the version bump, the `CHANGELOG.md` section assembled from the PR titles since the last release, and the manifest update. Nothing in it is meant to be edited by hand: the notes were written when those PRs were named ([`CONTRIBUTING.md`](CONTRIBUTING.md#pr-titles)), so reviewing it means reading the entries as a release rather than as a diff.

   Editing `CHANGELOG.md` here is an escape hatch rather than the process, for the entry that reads badly enough to be worth it. If you do, merge promptly: release-please regenerates this branch whenever `main` moves, and it will take your edit with it.

2. **Approve the publish.** Merging tags the release, creates the GitHub Release, and starts the publish against that tag — no command to run. It builds, re-runs the whole gate on the tagged commit, checks the artifacts, installs the wheel into a clean environment and uses it, and then waits in the `pypi` environment for a reviewer. Everything that can be checked has been by the time it asks; the one irreversible step is the only one with a person in front of it.

That is the whole flow. Nothing else needs doing, and nothing publishes without that approval.

**After a real publish of `maf-sandbox`, `maf-sandbox-acas` or `maf-sandbox-bicep`, a live check runs on its own.** `verify-live.yml` installs the just-released wheels into a clean environment and runs `samples/01_acas_bicep` against a real Azure sandbox, asserting the compiler's diagnostics come back — the one thing CI cannot prove from the workspace, since the real backend, image and preview service are only exercised by a real run. It creates a billable sandbox and needs a subscription, so it is the one check here that does not run on pull requests; a failure means the released *set* does not agree end to end, not that the merge was wrong. Run it yourself any time from Actions → *Verify (live)* → *Run workflow*. Its one-time setup is in [`docs/maintainers.md`](docs/maintainers.md#verifying-a-release-against-a-live-sandbox).

Three things a Release PR does need from you first, all of them because it is a generated branch that release-please stops maintaining once the content it would write settles.

- **Its required check sits at "Approve and run"** rather than reporting on its own, because a bot-opened PR trips the outside-collaborator rule.
- **A branch left behind by another release brings itself up to date**, on the next run after any push to `main`. That is `always-update` in `release-please-config.json`: without it release-please rewrites a release branch only when the content it would write changes, so the *other* packages' Release PRs — whose own notes did not change — stay pinned to an old `main` and conflict on the manifest and the lockfile that every release touches. Merging one release then meant deleting the rest.
- **If one is somehow still out of date**, update it with **rebase** rather than the default merge commit — `main` requires branches to be current, and the branch is release-please's, which assumes a single commit on it. The check resets and needs approving again afterwards.
- **If it says merge conflicts, do not resolve them.** Delete the branch, then re-run release-please (Actions → Release Please → *Run workflow*, since nothing pushes to `main` to trigger it), and it is rebuilt on the current `main`. This should no longer happen; it is the escape hatch, not the routine. Deleting is safe unless you used the changelog escape hatch on that branch, which goes with it.

**Optionally, before merging: rehearse on TestPyPI** — Actions → Publish → *Run workflow* → pick the package, target `testpypi`. Worth doing after a packaging change (a new dependency, a build-backend setting, a moved file); unnecessary for an ordinary code release, because the same install-and-use check runs on every PR and again before every publish.

**Releasing by hand** still works, for a release the automation cannot cut — push the tag and the same pipeline runs:

| Tag | Publishes |
|---|---|
| `maf-sandbox-v*` | `packages/maf-sandbox` |
| `maf-sandbox-acas-v*` | `packages/maf-sandbox-acas` |
| `maf-sandbox-bicep-v*` | `packages/maf-sandbox-bicep` |
| `maf-sandbox-codeact-v*` | `packages/maf-sandbox-codeact` |
| `maf-sandbox-docker-v*` | `packages/maf-sandbox-docker` |
| `maf-sandbox-wslc-v*` | `packages/maf-sandbox-wslc` |

Note the order this leaves you with: **the GitHub Release exists before PyPI has the package.** release-please creates it when its PR merges, and the alternatives that would delay it break release-please outright — see [`docs/maintainers.md`](docs/maintainers.md#why-a-release-exists-before-its-upload-does). If a publish fails, delete the Release and its tag; the version number is spent regardless.

## Release order

`maf-sandbox` first, then the packages that depend on it. This is enforced rather than merely documented: the smoke gate installs the built wheel from the real index with no local fallback, so publishing `maf-sandbox-acas` against an unpublished `maf-sandbox` fails there instead of shipping a version nobody can install.

**A release of `maf-sandbox` that the dependents need takes four steps**, because their constraint on it cannot be correct at both ends at once. They pin `maf-sandbox>=<floor>,<ceiling>`, and the two halves move at different times:

1. **Widen the ceiling** in an ordinary pull request, so it admits the version about to be released. Until then the `maf-sandbox` Release PR cannot even go green: the same run builds all three packages and installs each into a clean environment against `dist/`, and a ceiling that excludes the `maf-sandbox` wheel sitting there resolves the *older* one off PyPI instead and fails on import.
2. **Merge `maf-sandbox`'s Release PR** and let it publish.
3. **Raise the floor**, now that the version exists to point at. It could not move in step 1 — the smoke gate would have had nothing to resolve. **The release workflow opens this pull request for you**: once `maf-sandbox` publishes, it raises the floor in every dependent whose ceiling already admits the new version (the ones you widened in step 1), titles it `fix:`, and leaves it for you to merge, retitle, or close. Review it as a proposal — merging is still confirming the dependents genuinely use the version. Supply its required check the way you do for any bot-opened PR (approve the held run, or dispatch `tests.yml` at its branch). If a dependent's constraint has been reformatted away from `maf-sandbox>=X,<Y`, the step fails rather than skip silently, and `tests/test_release_config.py` guards that shape so you learn of the drift in an ordinary PR first.
4. **Merge the dependents' Release PRs.**

Between steps 1 and 3 the dependents' constraint is briefly wider than the truth, which is unavoidable while the version their code needs is not yet on PyPI. Getting the order wrong is caught rather than shipped — the publish smoke installs the older `maf-sandbox` and fails on import before anything is uploaded — but it spends a version number.

Each package gets its own Release PR, so ordering is a matter of which you merge first — but merging now publishes, so **let one finish before merging the next**. Two merged back to back would have their publishes in flight together, and the dependent one fails at the smoke gate while `maf-sandbox` is still waiting for your approval. That failure is safe and re-runnable; it is just noise you can avoid by waiting.

## Versioning

[SemVer](https://semver.org/), and every package is below `1.0.0` — so a minor bump may break API. Say so in the changelog when it does; at `0.x` the version number alone warns nobody.

Packages version independently. There is no lockstep release, and a fix in one is not a reason to bump the others.

## What the workflow refuses to do

- **Publish a tag that disagrees with the manifest.** PyPI releases are immutable, so a mismatched version would be permanent and unreproducible from this history.
- **Publish without the full gate passing on the tagged commit.** The Tests workflow ran on a branch; a tag can point anywhere.
- **Publish a stale or mixed `dist/`.** Exactly one sdist and one wheel, both named for the package being released.
- **Publish a wheel missing `py.typed` or the licence.** Both are invisible to this repository's own tests and break consumers.
- **Publish a package whose dependency is not on PyPI yet** (see *Release order*).
- **Publish a version with no changelog entry.** An empty release reads as "nothing changed", which is never true of a release.
- **Publish against a stale `uv.lock`.** Both gates sync with `--locked`, so a lockfile that disagrees with the versions being released fails before anything is built.

## If a release goes wrong

**A version cannot be replaced.** PyPI does not allow re-uploading a version, even after deleting it — deletion burns the number permanently. So:

- **Bad metadata, correct code** (a broken README, a wrong URL): ship it as an ordinary `docs:` change and let it cut a patch release. PyPI renders the newest release, so the corrected text supersedes what is shown. A PEP 440 `.postN` release is the traditional answer and is *not* available here — versions are generated, release-please parses them as SemVer, and hand-setting `0.1.0.post1` puts the manifest, the tag and the package's own metadata into three-way disagreement. That `docs:` releases at all is precisely so this route exists.
- **Bad code**: publish the fix as a new patch version and [yank](https://pypi.org/help/#yanked) the bad one. Yanking keeps it installable for anyone who pinned it exactly while removing it from fresh resolutions — almost always better than deleting.
- **Merging did not start a publish at all.** Look at the Release Please run: the *Publish each released package* step dispatches it, and a failure there leaves the tag and the Release in place with nothing running. Start it yourself against the tag that exists — `gh workflow run publish-packages.yml --ref maf-sandbox-v0.1.1 -f package=maf-sandbox -f target=pypi` — since re-pushing an existing tag does nothing.
- **The publish failed after the Release was created.** Nothing reached PyPI unless the upload step itself ran, but the Release and tag exist and the manifest already counts the version as released. Re-run the failed jobs if the cause was transient. Otherwise delete the Release and the tag, and let the next Release PR propose the following number — the failed one is spent.
