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

## Why releases are drafted

release-please creates the GitHub Release the moment its Release PR merges — before anything has reached PyPI. Left alone that inverts the ordering the publish workflow was built around, where a Release is the record of an upload that *succeeded*.

`"draft": true` resolves it. A draft is invisible to anyone without push access, sends no notification, and creates no tag of its own; `publish-packages.yml` flips it public once the upload has gone through, and until then a failed release has announced nothing.

The obvious alternative, `"skip-github-release": true`, is a trap: it wedges release-please. The `autorelease: pending` label on the merged Release PR never flips to `autorelease: tagged`, and a pending label is what stops the *next* Release PR from being opened ([release-please#1561](https://github.com/googleapis/release-please/issues/1561)).

## Why the tag is pushed by hand

The one manual step in a release is `git push origin <tag>`, and it is manual because of a GitHub rule with no configuration switch: **events triggered by a workflow's own `GITHUB_TOKEN` never start another workflow run.** release-please could create the tag itself, but that tag would start no publish — leaving a release that looks cut and was never uploaded, with nothing to say so.

Two ways around it were considered and rejected:

- **Call the publish job as a reusable workflow** from the release-please run, skipping tags entirely. PyPI forbids it: *"Reusable workflows cannot currently be used as the workflow in a Trusted Publisher"* ([warehouse#11096](https://github.com/pypi/warehouse/issues/11096)). The job that mints the token has to live in the workflow file registered as the publisher.
- **Give release-please a personal access token or a GitHub App**, so the tag push is not the robot's. This works, and it is the usual answer. It costs a stored credential in a repository whose entire publishing design is that there isn't one — to save a step from the same person who has to show up at the approval gate a minute later.

If that trade later looks worth making: add `token: ${{ secrets.RELEASE_PLEASE_TOKEN }}` to the action, drop `"draft": true` (a draft has no tag, so there would be nothing to trigger on), and delete the draft branch of the *Publish the GitHub Release* step. Be deliberate about what that gives up — with releases no longer drafted, a GitHub Release announces a version before PyPI has it.

## Release PRs and the required check

The same rule has a second consequence, and this one looks like a broken repository if you meet it cold. `main` requires the `Python (pytest + ruff + pyright)` check, and a pull request opened by a workflow's own token starts no workflow run — so a Release PR arrives with that check not merely failing but *never reported*, and GitHub will not merge it.

`tests.yml` therefore accepts `workflow_dispatch`. Running it against the Release PR's branch reports the check against that same commit and the PR merges normally. Prefer that to a rule bypass: the bypass works and is even defensible here — a Release PR only edits a version, a changelog and the manifest, and `publish-packages.yml` re-runs the entire gate on the tagged commit before anything is uploaded — but a release that routinely bypasses branch protection trains you to click through it.

A token for release-please would also fix this, by making the PR an ordinary one. That is the strongest argument for adding one; it is still a stored credential in a repository that has none.

## Adding a package to this repository

1. Create `packages/<name>/` with its own `pyproject.toml`, `README.md`, `CHANGELOG.md`, `LICENSE`, and a `py.typed` beside the module.
2. Give it its own `[tool.ruff]`, `[tool.pyright]` (strict) and `[tool.pytest.ini_options]` — the workspace root does not reach into packages, and an sdist has no root to inherit from. Add its `tests/` to the root `pyproject.toml`'s `testpaths` too, or repository-wide runs will skip them without saying so.
3. Add a tag glob for it to `publish-packages.yml`'s `on.push.tags`, and to the `workflow_dispatch` package choices.
4. Add it to the build/smoke loops in `tests.yml`, and to `scripts/smoke_install.py` — a package with no smoke can ship a broken wheel.
5. Register it in `release-please-config.json` and `.release-please-manifest.json`, seeding the manifest with the version its `pyproject.toml` already declares. Unregistered, it simply never gets a Release PR — so `tests/test_release_config.py` fails until both files list it, its manifest version matches, and its tag glob resolves to it alone.
6. Register its pending publishers (see above), then release it.

The tag globs do not overlap despite the shared prefix: in `maf-sandbox-aca-v0.1.0`, the character after `maf-sandbox-` is `a`, not `v`. Keep that true for any new name, or two packages will answer the same tag.
