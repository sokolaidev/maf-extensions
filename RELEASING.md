# Releasing

Each package releases on its own, from a tag that names it. Publishing runs on [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/): there is no API token in this repository, its secrets, or anyone's shell history.

One-time setup — the PyPI organization, the trusted publishers, the GitHub environments — is done, and documented in [`docs/maintainers.md`](docs/maintainers.md). You need that only when adding a package.

## Cutting a release

1. **Bump `version`** in the package's `pyproject.toml`, and add a dated `## [<version>]` section to its `CHANGELOG.md`. The release job reads that section for the release notes and fails if it is missing.
2. **Merge it through a PR.** `main` requires linear history and a green build.
3. **Optionally rehearse on TestPyPI** — Actions → Publish → *Run workflow* → pick the package, target `testpypi`. Worth doing after a packaging change (a new dependency, a build-backend setting, a moved file); unnecessary for an ordinary code release, because the same install-and-use check runs on every PR and again before every publish.
4. **Tag and push:**

   ```bash
   git tag maf-sandbox-v0.1.1 && git push origin maf-sandbox-v0.1.1
   ```

   | Tag | Publishes |
   |---|---|
   | `maf-sandbox-v*` | `packages/maf-sandbox` |
   | `maf-sandbox-aca-v*` | `packages/maf-sandbox-aca` |
   | `maf-sandbox-bicep-v*` | `packages/maf-sandbox-bicep` |

5. **Approve the release.** The publish job runs in the `pypi` environment, which requires a reviewer — everything before it (tests, types, build, artifact checks, install smoke) runs unattended, and then the one irreversible step waits for a person.

The tag does the rest: publish to PyPI, then a GitHub Release carrying that changelog section.

## Release order

`maf-sandbox` first, then the packages that depend on it. This is enforced rather than merely documented: the smoke gate installs the built wheel from the real index with no local fallback, so publishing `maf-sandbox-aca` against an unpublished `maf-sandbox` fails there instead of shipping a version nobody can install.

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
