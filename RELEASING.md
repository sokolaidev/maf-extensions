# Releasing

Each package releases on its own, from a tag that names it. Publishing runs on [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/): there is no API token in this repository, its secrets, or anyone's shell history.

One-time setup — the PyPI organization, the trusted publishers, the GitHub environments — is done, and documented in [`docs/maintainers.md`](docs/maintainers.md). You need that only when adding a package.

## What decides the version

[release-please](https://github.com/googleapis/release-please) watches `main` and keeps a **Release PR** open for every package that has unreleased changes. It works out the bump from the merged commit subjects — which are PR titles here, since this repository squash-merges — and attributes each change to a package by the files it touched. `feat:` bumps the minor; `fix:`, `perf:`, `revert:` and `docs:` bump the patch; a `!` or a `BREAKING CHANGE:` footer bumps the minor whatever the type, because every package is still `0.x`. `refactor`, `test`, `build`, `ci` and `chore` release nothing on their own. The rule behind that list is that any commit which earns a changelog entry earns a release, so it is `changelog-sections` in `release-please-config.json` that decides it.

## Cutting a release

1. **Review the Release PR, then merge it.** It carries the version bump, the `CHANGELOG.md` section assembled from the PR titles since the last release, and the manifest update. Nothing in it is meant to be edited by hand: the notes were written when those PRs were named ([`CONTRIBUTING.md`](CONTRIBUTING.md#pr-titles)), so reviewing it means reading the entries as a release rather than as a diff. Merging drafts a GitHub Release, which stays invisible and carries no tag until step 4 succeeds.

   Editing `CHANGELOG.md` here is an escape hatch rather than the process, for the entry that reads badly enough to be worth it. If you do, merge promptly: release-please regenerates this branch whenever `main` moves, and it will take your edit with it.

   Its required check will be missing, because a pull request opened by a workflow's own token starts no workflow run. Supply it with **Actions → Tests → Run workflow**, pointed at the Release PR's branch; the run reports against the same commit, and the PR then merges on its own merits rather than on a bypass.
2. **Optionally rehearse on TestPyPI** — Actions → Publish → *Run workflow* → pick the package, target `testpypi`. Worth doing after a packaging change (a new dependency, a build-backend setting, a moved file); unnecessary for an ordinary code release, because the same install-and-use check runs on every PR and again before every publish.
3. **Push the tag.** The Release Please run's job summary prints the exact command, with the right commit already filled in:

   ```bash
   git tag maf-sandbox-v0.1.1 <sha> && git push origin maf-sandbox-v0.1.1
   ```

   | Tag | Publishes |
   |---|---|
   | `maf-sandbox-v*` | `packages/maf-sandbox` |
   | `maf-sandbox-aca-v*` | `packages/maf-sandbox-aca` |
   | `maf-sandbox-bicep-v*` | `packages/maf-sandbox-bicep` |

   This step is deliberately yours rather than the robot's: a tag pushed by a workflow's own `GITHUB_TOKEN` starts no other workflow, so an automated tag would leave the release quietly unpublished. [`docs/maintainers.md`](docs/maintainers.md#why-the-tag-is-pushed-by-hand) has the full reasoning and what it would cost to automate.

4. **Approve the publish.** The publish job runs in the `pypi` environment, which requires a reviewer — everything before it (tests, types, build, artifact checks, install smoke) runs unattended, and then the one irreversible step waits for a person.

The tag does the rest: publish to PyPI, then the drafted GitHub Release goes public with that changelog section as its notes.

## Release order

`maf-sandbox` first, then the packages that depend on it. This is enforced rather than merely documented: the smoke gate installs the built wheel from the real index with no local fallback, so publishing `maf-sandbox-aca` against an unpublished `maf-sandbox` fails there instead of shipping a version nobody can install.

Each package gets its own Release PR, so ordering is a matter of which one you merge and tag first.

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

## If a release goes wrong

**A version cannot be replaced.** PyPI does not allow re-uploading a version, even after deleting it — deletion burns the number permanently. So:

- **Bad metadata, correct code** (a broken README, a wrong URL): publish a `.postN` release. The project page renders the newest release, so the corrected text supersedes what is shown.
- **Bad code**: publish the fix as a new patch version and [yank](https://pypi.org/help/#yanked) the bad one. Yanking keeps it installable for anyone who pinned it exactly while removing it from fresh resolutions — almost always better than deleting.
- **The publish failed, and a draft Release is left over.** Nothing was announced, and unless the upload step itself ran, no version was burned — that is what the draft is for. Re-run the failed jobs on the same tag once the cause is fixed; the run picks the draft back up and publishes it. If instead you abandon the version, delete the tag and the draft: the manifest already records that number as released, so the next Release PR will propose the one after it.
