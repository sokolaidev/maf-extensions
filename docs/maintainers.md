# Maintainer setup

Everything here is already done for the three published packages. You need it when **adding a package** to this repository, or when reconstructing the release plumbing somewhere else.

Read it before touching PyPI settings: much of it is one-time configuration whose failure mode is a `403` at token-mint time that does not say which field was wrong. The later sections are the reasoning behind two choices in the release flow that look like oversights until you know what was tried.

## Trusted publishing

Publishing uses OIDC. PyPI is told to trust *this repository, this workflow file, this environment*, and mints a short-lived credential per run. Nothing is stored as a secret.

Register one publisher per package, at [pypi.org → Publishing](https://pypi.org/manage/account/publishing/) (and the same on [test.pypi.org](https://test.pypi.org/manage/account/publishing/) for rehearsals). Before a project exists this is the "pending publisher" form; it converts to a normal publisher on first upload.

| Field | Value |
|---|---|
| PyPI Project Name | the distribution name, e.g. `maf-sandbox` |
| Owner | `sokolaidev` |
| Repository name | `maf-extensions` |
| Workflow name | `publish-packages.yml` |
| Environment name | `pypi` on PyPI, `testpypi` on TestPyPI |

Every field must match exactly. Two that reliably catch people:

- **Workflow name is the filename** — `publish-packages.yml`, not the workflow's display name ("Publish").
- **Environment name is matched literally**, so `pypi` and `testpypi` are not interchangeable and neither tolerates a typo.

### Pending publishers are unique per identity

A pending publisher is unique on `(owner, repository, workflow, environment)`. You **cannot** register all of a monorepo's packages up front — the second attempt fails with *"a pending trusted publisher matching this configuration has already been registered for a different project name"*.

The constraint disappears once a project exists, because the publisher stops being pending. So adding several packages is a loop:

> register the pending publisher → publish that package → register the next

which happens to match the required release order anyway (a package before the ones that depend on it).

## GitHub environments

`pypi` and `testpypi`, under Settings → Environments. The names are half of the identity PyPI matches on, so they must equal what is registered there.

`pypi` carries a **required reviewer**. The publish job does nothing but exchange a token and upload; everything that can be verified has already run by then, so the reviewer is approving exactly one irreversible action, with no credential existing while it waits. `testpypi` is deliberately ungated — rehearsals should stay frictionless.

## The PyPI organization

Packages are published under the `sokolai` organization (SOKOLAI BV — a Company organization, which is the paid tier; the Community tier is for non-commercial projects).

Worth knowing if you rebuild this elsewhere: **the organization is not on the critical path.** Trusted publishers can be registered against a personal account and the projects transferred into an organization later. Organization approval is manual and can take days; a release does not have to wait for it.

## Why a Release exists before its upload does

release-please creates the GitHub Release the moment its Release PR merges — before anything has reached PyPI. That inverts the order this repository would prefer, where a Release is the record of an upload that *succeeded*. It is accepted knowingly, because both ways out are worse.

`"draft": true` is the one that looks right and is a trap. **A draft carries no tag, and a tag is how release-please finds where the last release ended.** Its lookup runs releases → tags → manifest: the release iterator skips releases with no tag commit, the tag backfill has nothing to find, and the manifest fallback synthesises a release with `sha: ''`. An empty sha matches no commit, so `commitsAfterSha` returns the entire history. And since the action calls `createReleases()` and then `createPullRequests()` in a single invocation, the same run that drafted a release would immediately open a second Release PR replaying everything that had just shipped. `tests/test_release_config.py` asserts `draft` stays off for this reason.

`"skip-github-release": true` is the other one, and it wedges release-please differently: the `autorelease: pending` label on the merged Release PR never flips to `autorelease: tagged`, and a pending label is what stops the *next* Release PR from being opened ([release-please#1561](https://github.com/googleapis/release-please/issues/1561)).

Those two labels are the release state machine, not decoration — which is why the workflow grants `issues: write` alongside the permissions you would expect. Labels live on the Issues API even when they sit on a pull request, and release-please creates its own pair the first time it runs. Trim that permission and every release wedges in the same way, for a third reason.

So: if a publish fails after its Release exists, delete the Release and the tag. The version number is spent either way — the manifest already records it as released, and the next Release PR will propose the one after it.

Automating the publish does **not** on its own recover the ordering, which is easy to assume and wrong. release-please creates the Release the moment its PR merges no matter what starts the upload, and the only two settings that would stop it are the two broken ones above. Recovering the ordering means taking its release bookkeeping over entirely — `skip-github-release: true`, then creating the tag, relabelling the merged Release PR `autorelease: tagged` yourself so [#1561](https://github.com/googleapis/release-please/issues/1561) does not wedge the next one, and letting the publish run create the Release at the end. That works, and it is a hand-built state machine standing in for a known bug. Weigh it as such.

## Why the publish is launched by hand

release-please creates the tag, so nobody pushes one. What stays manual is *starting* the upload, and it is manual because of a GitHub rule with no configuration switch: **events triggered by a workflow's own `GITHUB_TOKEN` do not start another workflow run**, which exists to stop a workflow that pushes a commit from triggering itself forever. So release-please's tag lands and `publish-packages.yml`'s `on: push: tags` never fires.

One way out was considered and is closed: **calling the publish job as a reusable workflow** from the release-please run. PyPI forbids it — *"Reusable workflows cannot currently be used as the workflow in a Trusted Publisher"* ([warehouse#11096](https://github.com/pypi/warehouse/issues/11096)) — because the job that mints the token has to live in the file registered as the publisher.

The way out that is open, and not yet taken: **`workflow_dispatch` and `repository_dispatch` are documented exceptions and always create a run**, even from `GITHUB_TOKEN`. This workflow can therefore start the publish itself, with no credential — dispatching `publish-packages.yml` at the tag release-please just created. It is left as a follow-up rather than grown into the change that introduced everything above. Note what it does not do: the Release still precedes the upload, for the reason in the section above.

**A personal access token or GitHub App** is the third option and the usual answer elsewhere: it makes the tag a person's tag, so the push triggers normally. Note what it does and does not buy here. It fixes the required check on Release PRs, which the dispatch route only probably fixes. It does *not* fix the ordering, since a real release still precedes the upload. And it is a stored credential in a repository whose publishing design is that there isn't one — though it is a GitHub credential, not a PyPI one: nothing holding it could publish a package, which still needs OIDC and an approval on the `pypi` environment. Prefer a GitHub App via `actions/create-github-app-token` over a PAT: scoped to this repository, no expiry to forget, and it does not inherit an admin's ruleset bypass.

## Release PRs and the required check

The same rule has a second consequence, and this one looks like a broken repository if you meet it cold. `main` requires the `Python (pytest + ruff + pyright)` check, and a pull request opened by a workflow's own token starts no workflow run — so a Release PR arrives with that check not merely failing but *never reported*, and GitHub will not merge it.

`tests.yml` therefore accepts `workflow_dispatch`. Running it against the Release PR's branch reports the check against that same commit and the PR merges normally. Prefer that to a rule bypass: the bypass works and is even defensible here — a Release PR only edits a version, a changelog and the manifest, and `publish-packages.yml` re-runs the entire gate on the tagged commit before anything is uploaded — but a release that routinely bypasses branch protection trains you to click through it.

A token for release-please would also fix this, by making the PR an ordinary one. That is the strongest argument for adding one; it is still a stored credential in a repository that has none.

## Adding a package to this repository

1. Create `packages/<name>/` with its own `pyproject.toml`, `README.md`, `CHANGELOG.md`, `LICENSE`, and a `py.typed` beside the module.
2. Give it its own `[tool.ruff]`, `[tool.pyright]` (strict) and `[tool.pytest.ini_options]` — the workspace root does not reach into packages, and an sdist has no root to inherit from. Add its `tests/` to the root `pyproject.toml`'s `testpaths` too, or repository-wide runs will skip them without saying so.
3. Add a tag glob for it to `publish-packages.yml`'s `on.push.tags`, and to the `workflow_dispatch` package choices.
4. Add it to the build/smoke loops in `tests.yml`, and to `scripts/smoke_install.py` — a package with no smoke can ship a broken wheel.
5. Register it in `release-please-config.json` — **with its `package-name`** — and in `.release-please-manifest.json`, seeded with the version its `pyproject.toml` already declares. Unregistered, it simply never gets a Release PR — so `tests/test_release_config.py` fails until both files list it, its manifest version matches, and its tag glob resolves to it alone.

   `package-name` is not optional here, and its absence fails quietly rather than loudly: release-please's Python strategy reads `pyproject.toml` only to find version-bearing files, never to name the component. Leave it out and the component is the empty string, so the package tags as a bare `v<version>` — which collides with every other package and matches none of the publish workflow's globs.
6. Register its pending publishers (see above), then release it.

The tag globs do not overlap despite the shared prefix: in `maf-sandbox-aca-v0.1.0`, the character after `maf-sandbox-` is `a`, not `v`. Keep that true for any new name, or two packages will answer the same tag.
