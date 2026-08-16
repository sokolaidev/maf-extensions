# Maintainer setup

Everything here is already done for the packages published today — a count is deliberately not given, because it moves and a stale one reads as an instruction to skip a step. You need it when **adding a package** to this repository, or when reconstructing the release plumbing somewhere else.

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

A third environment, `live-verify`, exists too, but it belongs to release verification rather than publishing — its setup is in [its own section](#verifying-a-release-against-a-live-sandbox) below.

## Verifying a release against a live sandbox

`verify-live.yml` installs the *published* wheels into a clean environment and runs six samples, in three pairs — each pair one workload against a real Azure sandbox and against a Docker container on the runner, so a red on one side while the other is green names the backend rather than the workload. [`samples/01_acas_bicep`](../samples/01_acas_bicep/) and [`samples/05_docker_bicep`](../samples/05_docker_bicep/) assert the compiler's diagnostics came back; [`samples/03_acas_codeact`](../samples/03_acas_codeact/) and [`samples/06_docker_codeact`](../samples/06_docker_codeact/) assert the 100th Fibonacci number came back from code actually run in the sandbox — together the happy-path half of [#33](https://github.com/sokolaidev/maf-extensions/issues/33). [`samples/07_docker_diagram`](../samples/07_docker_diagram/) and [`samples/08_docker_codeact_files`](../samples/08_docker_codeact_files/) assert that a file came back *out*, a rendered PNG and a declared summary, read from disk rather than from stdout. It runs on demand (Actions → *Verify (live)* → *Run workflow*) and once after each real publish of `maf-sandbox`, `maf-sandbox-acas`, `maf-sandbox-bicep`, `maf-sandbox-codeact` or `maf-sandbox-docker` (dispatched by `publish-packages.yml`, which is why those two files know each other). One release is deliberately left out: a **breaking `maf-sandbox`** publish dispatches nothing, because the published dependents cannot work against it until each of them releases again, and those releases dispatch the check themselves ([#273](https://github.com/sokolaidev/maf-extensions/issues/273)). The publish run's summary says so when it happens, and "breaking" is read from the changelog section release-please wrote, since below `1.0.0` the version number cannot say it. Only the two ACAS samples create a **billable sandbox** — a PaaS container session, not a VM you provision — which is why this workflow never runs on a pull request; the four docker jobs cost nothing but model inference, and two of them build their guest image on the runner from this repository (`images/bicep-sandbox`, `images/diagram-sandbox`) rather than reading an image reference from configuration. The Docker backend's own live coverage on a pull request comes from `tests.yml`, not from here.

Authentication is OIDC federation to Azure — `azure/login`, no stored secret, the same principle as Trusted Publishing above. Everything the sample reads is non-secret configuration (endpoints and ids), so all of it lives as **environment variables**, none as secrets.

Setting it up is a one-time job. The variables below all point at Azure resources, so stand those up first, then wire GitHub to them. None of the values belong in this repository — they live on the GitHub environment.

**In Azure — the resources the run reads from.** The sample's own [README](../samples/01_acas_bicep/README.md) covers these from a user's angle; this is the same list, mapped to the variables the workflow sets.

1. **A subscription enrolled in the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview**, and a resource group to hold the rest (→ `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_SUBSCRIPTION_ID`).
2. **A sandbox group** in it (→ `ACAS_SANDBOX_GROUP`), created and managed at the [Sandboxes portal](https://sandboxes.azure.com/), whose data-plane endpoint (`https://management.<region>.azuredevcompute.io`) is `ACAS_SANDBOX_ENDPOINT`. This is what the sample creates its sandbox in.
3. **A container registry** serving the pinned Bicep sandbox image (→ `ACAS_SANDBOX_REGISTRY`, its **login server FQDN** `<name>.azurecr.io`, not the bare name — it prefixes the bare `repository:tag` to form the pull reference), with that image **imported into the sandbox group as a disk image** (→ `BICEP_SANDBOX_IMAGE`, a bare `repository:tag`). A sandbox boots from a disk image, not from the registry it was pushed to; [`packages/maf-sandbox-acas/scripts/import_disk_image.py`](../packages/maf-sandbox-acas/scripts/import_disk_image.py) does the import, and the image itself is built outside this repository.
4. **The CodeAct sandbox image imported into the same sandbox group as a disk image**, for [`samples/03_acas_codeact`](../samples/03_acas_codeact/): `mcr.microsoft.com/devcontainers/python:3.13-bookworm`. No environment variable accompanies this one — the sample names the fully-qualified MCR reference itself (`CODEACT_IMAGE` in `agent.py`), and it is passed through to the backend untouched rather than qualified against a registry, so there is nothing here for the workflow to set beyond the import having already happened. Same idea as item 3, a public image needing no registry credential:

   ```bash
   aca sandboxgroup disk create --image mcr.microsoft.com/devcontainers/python:3.13-bookworm --name python-3-13
   ```

   or the portal equivalent, which suits a one-off import the same as above: [sandboxes.azure.com](https://sandboxes.azure.com) → the sandbox group → **Disk Images** → **Create**.
5. **A Microsoft Foundry (or Azure OpenAI) resource with a reasoning-model deployment** (→ `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_MODEL`). It must be a reasoning model, or the sample fails its first call with `400 — Encrypted content is not supported with this model` ([#21](https://github.com/sokolaidev/maf-extensions/issues/21)). Set **`disableLocalAuth: true`** on it, so the run authenticates through Entra only and the account's local keys cannot be used.
6. **An identity with a federated credential.** An app registration (or user-assigned managed identity) whose federated credential has **entity type Environment**, environment name **`live-verify`**, issuer `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`. Its **Subject must be the exact subject GitHub puts in the token** — for this repo the *immutable* form `repo:sokolaidev@<owner-id>/maf-extensions@<repo-id>:environment:live-verify`, because the repo was created after GitHub's 2026-07-15 immutable-subject cutoff. The Azure *immutable ID* credential form builds that value from the **Organization ID** and **Repository ID** you enter, so let it — do **not** override the generated subject. **Do not trust `use_immutable_subject`** from `gh api repos/sokolaidev/maf-extensions/actions/oidc/customization/sub`: it can read `false` while `sub_claim_prefix` already holds the `@id` immutable prefix that is actually emitted. Take the subject from `sub_claim_prefix` (the whole `repo:sokolaidev@<owner-id>/maf-extensions@<repo-id>`) plus `:environment:live-verify`, or read the `subject claim` line in a failed *Sign in to Azure with OIDC* run, and match it byte-for-byte — a mismatch fails login with `AADSTS700213 — No matching federated identity record found`.
7. **RBAC for that identity**, since it authenticates as itself with no key: rights to **create sandboxes in the sandbox group**, a pull role on the registry — **`Container Registry Repository Reader`** on a registry in *RBAC + ABAC* permissions mode, or `AcrPull` on a classic-mode one — and the OpenAI inference role **`Cognitive Services OpenAI User`** on the model resource. That role carries `Microsoft.CognitiveServices/accounts/OpenAI/responses/*`, which is what the sample's `OpenAIChatClient` needs for its `POST /openai/v1/responses` call. **Assign it at the Foundry _resource (account)_ scope, not at a project inside it.** The OpenAI data actions live on the account, so a project-scoped assignment — or `Foundry User`, which grants Foundry-*project* actions rather than OpenAI data-plane ones — still fails `401 PermissionDenied — lacks … OpenAI/responses/write`, even though the role name is right. That scope mistake looks correct in the portal and is the easy one to make. Missing any of the three roles fails the sample with an error the run log carries in full. Set **`disableLocalAuth: true`** on the model resource regardless, so the identity is confined to Entra rather than the account's local keys.

**In GitHub — wire the workflow to them.** A GitHub environment **`live-verify`**, under Settings → Environments. Leave it **ungated** — no required reviewer (the post-release run must not wait on a human, and no credential exists while it would) — and place **no deployment-branch restriction** on it, because it runs both at a release tag (the automated path) and at a branch (a dispatch). Set these variables on it (Settings → Environments → `live-verify` → *Environment variables*), all non-secret:

   | Variable | For |
   |---|---|
   | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `azure/login` — the federated identity (Azure 6–7) |
   | `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_GROUP`, `ACAS_SANDBOX_REGISTRY` | the sandbox group and registry (Azure 1–3) |
   | `BICEP_SANDBOX_IMAGE` | `repository:tag` of the imported Bicep image (Azure 3) |
   | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_MODEL` | the reasoning-model deployment (Azure 5) |

Each pair shares one assertion script, which is what keeps the two sides comparable. **None of them reads the model's prose for anything the run is being gated on** ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)). Samples 01–06 and 09 each print a fenced block of what their tool returned — the framework records that beside the call, so the model does not write it — closed by a line the sample tagged `[measured]`. The samples pass every reply through `quoted` in `_scaffold.py` first, which turns any line of it beginning with that tag into a quotation, so a model can write the heading and cannot close the block.

That was worth doing because the alternative was wrong in both directions at once. `samples/01_acas_bicep/main.bicep` names both rule ids, both severities and both line numbers in its own comments, so a model that never called `bicep_validate` could write a summary carrying every field the old check looked for; and the same check failed three healthy releases because a run rendered `**error**` where the pattern wanted `[error]`. The tools' own renderings are fixed, so reading them needs no guess about markup.

`scripts/check_live_sample.py` (samples 01, 05 and 09) reads `build(name): N diagnostic(s)` and `[level] rule @ file:line` out of that block. It still matches rule ids rather than whole strings, because the diagnostics carry a day count and an API-version list that climb on their own. It also asserts the compiler found `bicepconfig.json`, which is the one worth knowing about here: sample 01 boots a **disk image**, and a disk image is a snapshot. Overwriting the registry tag it was imported from leaves the running image untouched, so a config baked at a work-dir root the tool no longer writes to keeps booting, lints against the CLI's built-in defaults and passes every other assertion in the script ([#308](https://github.com/sokolaidev/maf-extensions/issues/308)). If that check is what goes red after a `maf-sandbox-bicep` release, the fix is a re-import under a **new** tag, not a change to the sample — see [`images/bicep-sandbox/README.md`](../images/bicep-sandbox/README.md). `scripts/check_live_codeact_sample.py` (03 and 06) looks for the literal Fibonacci value **inside** the block: the number is a constant any model can recite, so the same digits are worth nothing in a reply and everything in the interpreter's stdout. Each script reads one thing loosely and says so — that the reply *names* what the block reports, which is the claim the tool's answer reached the model rather than only the log.

The two artifact samples read the landed file instead of the transcript, because a model handed a sentence by the sink can write that sentence whether or not a file landed: `check_live_codeact_files_sample.py` reads the summary's text, `check_live_diagram_sample.py` reads the PNG's own header. `check_live_host_tools_sample.py` (sample 10) is the one that matches *strictly*, and it can because no model stands between the library and stdout — the values it compares are `maf-sandbox`'s own answers, so a mismatch is a behaviour change rather than a paraphrase. Every one of them is a pure function unit-tested on each PR (`tests/test_check_live_*.py`); only the runs that feed them cost anything, and sample 10's costs nothing but runner time — no inference, no sandbox, no credential.

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

The Release PR's checks are a different matter, and the same exception does **not** rescue them — the `GITHUB_TOKEN` rule is not what stops them either. A pull request opened by this token *does* start a `pull_request` run; that run is then held at `action_required` (see below), so the required check is never *reported* and `main` refuses the merge. Dispatching `tests.yml` at the branch does report the contexts against the same commit, and it changes nothing, because the held run blocks the merge regardless. That was built, measured, and reverted ([#196](https://github.com/sokolaidev/maf-extensions/issues/196)); a person clears the gate.

Two roads not taken. **Calling the publish job as a reusable workflow** is closed outright: PyPI forbids it — *"Reusable workflows cannot currently be used as the workflow in a Trusted Publisher"* ([warehouse#11096](https://github.com/pypi/warehouse/issues/11096)) — because the job that mints the token must live in the file registered as the publisher. And **a personal access token or GitHub App**, the usual answer elsewhere, is avoided here for the tag: dispatch already gets a publish running without a credential. For the Release PR an App is the *only* route that would remove the human step — dispatch was expected to and does not, since the held run blocks the merge whatever the checks say. It was weighed and declined in [#196](https://github.com/sokolaidev/maf-extensions/issues/196): an App ID and private key stored as repository secrets buys one click per bot pull request, in a repository whose whole publishing story is that it holds no credentials. If that is ever revisited, prefer an App via `actions/create-github-app-token` to a PAT — scoped to this repository, no expiry to forget, and no inherited ruleset bypass from an admin. It would still not fix the ordering.

## A Release PR's checks wait for approval

A Release PR arrives with `Python (pytest + ruff + pyright)` unreported, and `main` requires it. The run does exist: it is queued as `action_required`, because `github-actions[bot]` trips an approval gate. **Approve and run** on the held run is the step, and it is the only one. Expect one per bot pull request, every release — Release PRs and the range proposal alike.

Automating it was tried and reverted ([#196](https://github.com/sokolaidev/maf-extensions/issues/196)). `release-please.yml` used to dispatch `tests.yml` at each pull request it opened, which does report the required contexts against the same commit — and **the held run blocks the merge anyway**. Measured on the 0.8.0 cycle: both Release PRs had every required context green, CodeQL green, no unresolved threads and zero required approvals, and both stayed `BLOCKED`. Approving re-runs the suite as attempt 2, so the dispatched run was a duplicate rather than a shortcut; across that cycle it cost about forty extra runs and saved no clicks.

This has been misdiagnosed twice, so the negative results are worth keeping. It is *not* the event failing to fire — the `GITHUB_TOKEN` rule blocks tags from triggering workflows, and a pull request looked likely to be blocked the same way; the runs are in fact created and held. And it is *not* the outside-collaborator setting, which is the obvious culprit and was tested directly: moving Settings → Actions → General from *all external contributors* to *first-time contributors* changed nothing, the next bot pull request's runs were held exactly as before. What remains is the pull request's authorship by `GITHUB_TOKEN`, which is why release-please's own documentation reaches for a PAT or an App token. Weighed in [#196](https://github.com/sokolaidev/maf-extensions/issues/196).

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

The tag globs do not overlap despite the shared prefix: in `maf-sandbox-acas-v0.1.0`, the character after `maf-sandbox-` is `a`, not `v`. Keep that true for any new name, or two packages will answer the same tag.
