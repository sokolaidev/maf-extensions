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

## Why publishing is dispatched rather than triggered

`publish-packages.yml` still declares `on: push: tags`, and in the automated path nothing uses it. That is because of a GitHub rule with no configuration switch: **events triggered by a workflow's own `GITHUB_TOKEN` do not start another workflow run**, which exists to stop a workflow that pushes a commit from triggering itself forever. release-please creates the tag, so the tag push is the robot's, so `on: push: tags` never fires.

**`workflow_dispatch` and `repository_dispatch` are documented exceptions and always create a run**, even from `GITHUB_TOKEN`. So the release-please workflow dispatches the publish itself, at the tag, with no stored credential. Two details in that step are load-bearing: it targets the *tag* rather than `main`, because `main` may already have moved past the release commit, and it grants `actions: write` — which lets it start workflows and nothing else. It cannot approve one; the `pypi` environment's reviewer still stands between a dispatch and an upload.

The same exception supplies the Release PR's checks, for the second half of the same rule: a pull request opened by this token starts no `pull_request` run, so its required check would never be *reported* and `main` would refuse the merge. The workflow dispatches `tests.yml` at the Release PR's branch, and that run reports against the same commit.

Two roads not taken. **Calling the publish job as a reusable workflow** is closed outright: PyPI forbids it — *"Reusable workflows cannot currently be used as the workflow in a Trusted Publisher"* ([warehouse#11096](https://github.com/pypi/warehouse/issues/11096)) — because the job that mints the token must live in the file registered as the publisher. And **a personal access token or GitHub App**, the usual answer elsewhere, is now unnecessary: it would make these events a person's events, but dispatch already achieves that without a credential. If the dispatched check ever turns out not to satisfy the branch rule, an App via `actions/create-github-app-token` is the fallback — prefer it to a PAT, since it is scoped to this repository, has no expiry to forget, and does not inherit an admin's ruleset bypass. It would still not fix the ordering.

## A Release PR's checks wait for approval

A Release PR arrives with `Python (pytest + ruff + pyright)` unreported, and `main` requires it. The run does exist: it is queued as `action_required`, because `github-actions[bot]` trips the *require approval for outside collaborators* setting. **Approve and run** on the run, and the check reports and passes.

This was misdiagnosed at first as the event never firing — the `GITHUB_TOKEN` rule blocks tags from triggering workflows, so a PR seemed likely to be blocked the same way. It is not; the runs are created and held. Loosening that setting under Settings → Actions → General would make Release PRs self-checking, at the cost of applying to every contributor. A GitHub App token would too, by making the PR an ordinary one.

Prefer either of those to a rule bypass. The bypass works, and is even defensible here — a Release PR only edits a version, a changelog and the manifest, and `publish-packages.yml` re-runs the entire gate on the tagged commit before anything is uploaded — but a release that routinely bypasses branch protection trains you to click through branch protection.

## Adding a package to this repository

1. Create `packages/<name>/` with its own `pyproject.toml`, `README.md`, `CHANGELOG.md`, `LICENSE`, and a `py.typed` beside the module.
2. Give it its own `[tool.ruff]`, `[tool.pyright]` (strict) and `[tool.pytest.ini_options]` — the workspace root does not reach into packages, and an sdist has no root to inherit from. Add its `tests/` to the root `pyproject.toml`'s `testpaths` too, or repository-wide runs will skip them without saying so.
3. Add a tag glob for it to `publish-packages.yml`'s `on.push.tags`, and to the `workflow_dispatch` package choices.
4. Allow its tag through the `pypi` environment: Settings → Environments → `pypi` → *Deployment branches and tags* → add `<name>-v*` as a tag rule (or `gh api -X POST repos/sokolaidev/maf-extensions/environments/pypi/deployment-branch-policies -f name="<name>-v*" -f type=tag`). The environment deploys only listed tag patterns, so without this the publish fails at the *Publish to pypi* job — after every gate has passed — with *"not allowed to deploy to pypi due to environment protection rules"*, and a green TestPyPI rehearsal proves nothing about it because `testpypi` carries no such restriction. The failed run is rerunnable once the pattern exists; this step is written down because it was found exactly that way.
5. Add it to the build/smoke loops in `tests.yml`, and to `scripts/smoke_install.py` — a package with no smoke can ship a broken wheel.
6. Register it in `release-please-config.json` — **with its `package-name`** — and in `.release-please-manifest.json`, seeded with the version its `pyproject.toml` already declares. Unregistered, it simply never gets a Release PR — so `tests/test_release_config.py` fails until both files list it, its manifest version matches, and its tag glob resolves to it alone.

   `package-name` is not optional here, and its absence fails quietly rather than loudly: release-please's Python strategy reads `pyproject.toml` only to find version-bearing files, never to name the component. Leave it out and the component is the empty string, so the package tags as a bare `v<version>` — which collides with every other package and matches none of the publish workflow's globs.

   The entry also needs its **`extra-files` updater for `uv.lock`**, copied from a sibling with the name changed in the `jsonpath`. The lock records a version for every workspace member and release-please knows nothing about it, so a package without one releases perfectly happily and leaves the lock a version behind — after which `uv sync --locked` fails for everyone, on a branch that changed none of this.

   Two details in that entry are load-bearing, and both were arrived at the hard way:

   - **The path is `/uv.lock`, with a leading slash.** `extra-files` paths are resolved against the package directory, and release-please rejects `../` outright — `illegal pathing characters in path` — failing the whole run, so no package gets a Release PR at all. A leading slash means repository-root-relative, which is the only way to reach this file.
   - **The filter reads `@.name.value`, not `@.name`.** release-please parses TOML into position-annotated nodes (`{start, end, value}`), so comparing `@.name` to a string compares an object and matches nothing. That failure is quieter — a warning in the log and a Release PR with a stale lock — and what catches it is the `uv sync --locked` gate turning that PR's required check red.

   The second is release-please's internal parser shape rather than a documented contract. Re-check it when bumping the action, and verify a config change before merging it rather than on `main`:

   ```bash
   npx release-please release-pr --repo-url=sokolaidev/maf-extensions \
     --target-branch=<your-branch> --config-file=release-please-config.json \
     --manifest-file=.release-please-manifest.json --dry-run --token="$(gh auth token)"
   ```
7. Register its pending publishers (see above), then release it.

The tag globs do not overlap despite the shared prefix: in `maf-sandbox-aca-v0.1.0`, the character after `maf-sandbox-` is `a`, not `v`. Keep that true for any new name, or two packages will answer the same tag.
