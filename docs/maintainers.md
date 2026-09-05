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

`verify-live.yml` installs the *published* wheels into a clean environment and runs fourteen sample jobs, plus one job that is neither — `acas-e2e` runs `maf-sandbox-acas`'s live suite from the checkout rather than from the index, because it tests a backend against the real service rather than asking whether the published set still works. It is the only place four of the shared `FILES_OUT` conformance probes meet the real service: they require `Capability.FILES_LIST`, this is the only backend that declares it, and so they skip in the docker suite that pull requests run. A fake answers them offline on every pull request, which is a weaker claim than it sounds — twice already, the package's own idea of the payload has disagreed with the service's. Eight of them are four pairs — each pair one workload against a real Azure sandbox and against a Docker container on the runner, so a red on one side while the other is green names the backend rather than the workload. [`samples/01_acas_bicep`](../samples/01_acas_bicep/) and [`samples/05_docker_bicep`](../samples/05_docker_bicep/) assert the compiler's diagnostics came back; [`samples/03_acas_codeact`](../samples/03_acas_codeact/) and [`samples/06_docker_codeact`](../samples/06_docker_codeact/) assert the 100th Fibonacci number came back from code actually run in the sandbox — together the happy-path half of the sample coverage; [`samples/08_docker_codeact_files`](../samples/08_docker_codeact_files/) and [`samples/14_acas_codeact_files`](../samples/14_acas_codeact_files/) assert that a declared summary came back *out*, read from disk rather than from stdout; and [`samples/15_acas_codeact_host_tools`](../samples/15_acas_codeact_host_tools/) sends a program inside the sandbox back *out* to call a host tool and gates on which route carried the data — run on either backend via `SAMPLE_BACKEND`, a billable ACAS microVM or a local Docker container — with what the round trip costs measured against the same question answered without one. The other six answer to nothing but themselves: [`samples/07_docker_diagram`](../samples/07_docker_diagram/) lands a rendered PNG through a kind the sample writes, 09 runs the Bicep workload against the CLI on the runner, 10 needs no sandbox and no model at all, 11 and 12 exercise the router's own selection and disposal, and 13 is the only multi-turn job. It runs on demand (Actions → *Verify (live)* → *Run workflow*) and once after each real publish of `maf-sandbox`, `maf-sandbox-acas`, `maf-sandbox-bicep`, `maf-sandbox-codeact` or `maf-sandbox-docker` (dispatched by `publish-packages.yml`, which is why those two files know each other). One release is deliberately left out: a `maf-sandbox` publish where no published dependent admits the candidate after the upload dispatches nothing, because there is nothing for a live run to measure — their own publishes dispatch the check, which is when its answer starts meaning something. A **breaking** `maf-sandbox` release is not that case: the breaking flag says nothing about whether the published dependents still import, so the dispatch is decided by that import check in the publish job, not the changelog heading — and a breaking release whose dependents do still admit and import runs the live check all the more. The verdict is measured after the upload, once the core is fully public, so a dependent that admits during the approval window or the upload itself still dispatches the check rather than a `skip` frozen at build. A break found before the upload refuses it; a break found only after the upload ships with the live check dispatched and the break surfaced, since the upload is immutable — the tradeoff accepted here. The publish run's summary says so when a run is skipped, and again when the train is still draining: a release that publishes while some published dependent has not itself published since that core gets a note saying a red in the live check is more likely the order of the train than the code. Publication time rather than a declared floor, because a floor is raised only when a dependent's code needs the version, so an older minimum can be permanent by design. An annotation rather than a gate, because gating the dispatch on a coarser signal is what was removed earlier and the annotation costs nothing either way. Five jobs create a **billable sandbox**, a PaaS container session rather than a VM you provision, which is why this workflow never runs on a pull request: the four ACAS samples — 01, 03, 14 and 15 — and `acas-e2e`. Two of those create more than one: `acas-e2e` creates **four** — one shared by its probes, one the lifecycle test disposes as the thing under test, one the prebuilt-image test boots from the service's catalogue, and one the egress leg acquires under an allowlist, since a denied host needs a sandbox that asked for confinement to be denied against — and sample 15 creates two, giving each route its own so neither can read the other's leftovers on legacy transports — it stays two after the transport gained a cleanup, because the sample runs against whichever core and CodeAct versions are published. Every count here is **spend, not concurrency**: `acas-e2e` acquires its four over the run and holds at most two at a time — the shared sandbox plus whichever of the other three is alive — because the lifecycle test disposes its own inside the test, the prebuilt-image one hangs off a class-scoped fixture pytest finalises when that class ends, and the egress one is requested by the class after it. `acas-e2e` is still the cheapest of the five per assertion, since it runs nineteen tests and calls no model. The eight docker jobs create nothing billable — six of them cost model inference and 11 and 12 cost not even that, since the router's own decisions need no model to demonstrate — and three build their guest image on the runner from this repository (`images/bicep-sandbox`, `images/diagram-sandbox`) rather than reading an image reference from configuration; 09 needs only the bicep CLI on the runner, and 10 reaches nothing at run time at all. The Docker backend's own live coverage on a pull request comes from `tests.yml`, not from here. The ACAS backend's live conformance also runs daily on a schedule, in its own workflow — `conformance-live.yml`, one job that is a copy of `acas-e2e` above, four billable sandboxes acquired over the run, no model — so a drift between that backend and the service is found in days rather than at the next dispatch. One job retries: `samples/13_bicep_fix_loop` runs its two-turn loop a second time when the check exits 3, which means every failure belonged to the model's half — either turn — and every deterministic measurement passed. Every other failure, in that job and in all the others, fails on the first attempt. A retry is annotated on the run and the attempt count is in the job summary, so a red is never a silent second chance. wslc has no scheduled leg at all: it needs Windows with a `wslc` that ships it, which no offered runner is, so its conformance runs wherever a developer has one.

Authentication is OIDC federation to Azure — `azure/login`, no stored secret, the same principle as Trusted Publishing above. Everything the sample reads is non-secret configuration (endpoints and ids), so all of it lives as **environment variables**, none as secrets.

Setting it up is a one-time job. The variables below all point at Azure resources, so stand those up first, then wire GitHub to them. None of the values belong in this repository — they live on the GitHub environment.

**In Azure — the resources the run reads from.** The sample's own [README](../samples/01_acas_bicep/README.md) covers these from a user's angle; this is the same list, mapped to the variables the workflow sets.

1. **A subscription enrolled in the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview**, and a resource group to hold the rest (→ `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_SUBSCRIPTION_ID`).
2. **A sandbox group** in it (→ `ACAS_SANDBOX_GROUP`), created and managed at the [Sandboxes portal](https://sandboxes.azure.com/), whose data-plane endpoint (`https://management.<region>.azuredevcompute.io`) is `ACAS_SANDBOX_ENDPOINT`. This is what the sample creates its sandbox in.
3. **A container registry** serving the pinned Bicep sandbox image (→ `ACAS_SANDBOX_REGISTRY`, its **login server FQDN** `<name>.azurecr.io`, not the bare name — it prefixes the bare `repository:tag` to form the pull reference), with that image **imported into the sandbox group as a disk image** (→ `BICEP_SANDBOX_IMAGE`, a bare `repository:tag`). A sandbox boots from a disk image, not from the registry it was pushed to; [`packages/maf-sandbox-acas/scripts/import_disk_image.py`](../packages/maf-sandbox-acas/scripts/import_disk_image.py) does the import, and the image itself is built outside this repository.
4. **The CodeAct sandbox image imported into the same sandbox group as a disk image**, for [`samples/14_acas_codeact_files`](../samples/14_acas_codeact_files/) and [`samples/15_acas_codeact_host_tools`](../samples/15_acas_codeact_host_tools/) — one import serves both, since they name the same reference: `mcr.microsoft.com/devcontainers/python:3.13-bookworm`. [`samples/03_acas_codeact`](../samples/03_acas_codeact/) no longer needs it: it names the service-provided prebuilt image `python-3.13`, which the group resolves from its catalogue, so nothing is imported for it. No environment variable accompanies this one — the samples name the fully-qualified MCR reference themselves (`CODEACT_IMAGE` in `agent.py`), and it is passed through to the backend untouched rather than qualified against a registry, so there is nothing here for the workflow to set beyond the import having already happened. Same idea as item 3, a public image needing no registry credential:

   ```bash
   export ACA_SUBSCRIPTION=<sub-id> ACA_RESOURCE_GROUP=<sandbox-group-rg> ACA_REGION=<region>
   aca sandboxgroup disk create --group <group> --image mcr.microsoft.com/devcontainers/python:3.13-bookworm --name python-3-13
   ```

   or the portal equivalent, which suits a one-off import the same as above: [sandboxes.azure.com](https://sandboxes.azure.com) → the sandbox group → **Disk Images** → **Create**.
5. **A Microsoft Foundry (or Azure OpenAI) resource with a reasoning-model deployment** (→ `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_MODEL`). It must be a reasoning model, or the sample fails its first call with `400 — Encrypted content is not supported with this model`. Set **`disableLocalAuth: true`** on it, so the run authenticates through Entra only and the account's local keys cannot be used.
6. **An identity with a federated credential.** An app registration (or user-assigned managed identity) whose federated credential has **entity type Environment**, environment name **`live-verify`**, issuer `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`. Its **Subject must be the exact subject GitHub puts in the token** — for this repo the *immutable* form `repo:sokolaidev@<owner-id>/maf-extensions@<repo-id>:environment:live-verify`, because the repo was created after GitHub's 2026-07-15 immutable-subject cutoff. The Azure *immutable ID* credential form builds that value from the **Organization ID** and **Repository ID** you enter, so let it — do **not** override the generated subject. **Do not trust `use_immutable_subject`** from `gh api repos/sokolaidev/maf-extensions/actions/oidc/customization/sub`: it can read `false` while `sub_claim_prefix` already holds the `@id` immutable prefix that is actually emitted. Take the subject from `sub_claim_prefix` (the whole `repo:sokolaidev@<owner-id>/maf-extensions@<repo-id>`) plus `:environment:live-verify`, or read the `subject claim` line in a failed *Sign in to Azure with OIDC* run, and match it byte-for-byte — a mismatch fails login with `AADSTS700213 — No matching federated identity record found`.
7. **RBAC for that identity**, since it authenticates as itself with no key: rights to **create sandboxes in the sandbox group**, a pull role on the registry — **`Container Registry Repository Reader`** on a registry in *RBAC + ABAC* permissions mode, or `AcrPull` on a classic-mode one — and the OpenAI inference role **`Cognitive Services OpenAI User`** on the model resource. That role carries `Microsoft.CognitiveServices/accounts/OpenAI/responses/*`, which is what the sample's `OpenAIChatClient` needs for its `POST /openai/v1/responses` call. **Assign it at the Foundry _resource (account)_ scope, not at a project inside it.** The OpenAI data actions live on the account, so a project-scoped assignment — or `Foundry User`, which grants Foundry-*project* actions rather than OpenAI data-plane ones — still fails `401 PermissionDenied — lacks … OpenAI/responses/write`, even though the role name is right. That scope mistake looks correct in the portal and is the easy one to make. Missing any of the three roles fails the sample with an error the run log carries in full. Set **`disableLocalAuth: true`** on the model resource regardless, so the identity is confined to Entra rather than the account's local keys.

**In GitHub — wire the workflow to them.** A GitHub environment **`live-verify`**, under Settings → Environments. Leave it **ungated** — no required reviewer (the post-release run must not wait on a human, and no credential exists while it would) — and place **no deployment-branch restriction** on it, because it runs both at a release tag (the automated path) and at a branch (a dispatch).

**Eight of these are secrets and four are variables, and the split is load-bearing.** Actions redacts a `secrets` value wherever it reaches a log and never redacts a `vars` one. The runner also echoes each step's resolved `env:` block, and a *job-level* block is echoed on the first step — `actions/checkout` — so no step runs early enough to mask a variable with `::add-mask::`. A variable holding an identifier is therefore published to anyone with a GitHub account the moment the job runs; these logs are readable by any signed-in user, this repository being public. The four that stay variables name public things: a Microsoft-owned endpoint, a public model name, and two `repository:tag` strings. The eight that name *this* estate are secrets.

Set the first table under *Environment secrets* and the second under *Environment variables*. **A secret cannot be read back once set** — to recover a value, read it from Azure rather than from GitHub.

   | Secret | For |
   |---|---|
   | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `azure/login` — the federated identity (Azure 6–7) |
   | `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_GROUP`, `ACAS_SANDBOX_REGISTRY` | the sandbox group and registry (Azure 1–3) |
   | `AZURE_OPENAI_ENDPOINT` | the reasoning-model deployment (Azure 5) |

   | Variable | For |
   |---|---|
   | `ACAS_SANDBOX_ENDPOINT` | the ACAS control plane. Microsoft-owned and the same for every tenant, so it names nothing of yours |
   | `AZURE_OPENAI_CHAT_MODEL` | the deployment name (Azure 5). A public model name — as a secret it would redact itself out of every sample's own output |
   | `BICEP_SANDBOX_IMAGE` | `repository:tag` of the imported Bicep image (Azure 3) |
   | `ACAS_SANDBOX_NONROOT_IMAGE` | **optional.** `repository:tag` of an imported image whose `USER` is not root. Nothing else in the live suites needs one, and unset is a supported state: the leg that measures the acquire-time non-root gate skips and names itself in the `-ra` summary. Set it and that leg costs two more billable sandboxes per run — the class fixture's, and one the cold-refusal test creates and deletes to prove a refused acquire leaks nothing |

Each pair shares one assertion script, which is what keeps the two sides comparable. **None of them reads the model's prose for anything the run is being gated on**. Samples 01–06 and 09 each print a fenced block of what their tool returned — the framework records that beside the call, so the model does not write it — closed by a line the sample tagged `[measured]`. The samples pass every reply through `quoted` in `_scaffold.py` first, which turns any line of it beginning with that tag into a quotation, so a model can write the heading and cannot close the block.

That was worth doing because the alternative was wrong in both directions at once. `samples/01_acas_bicep/main.bicep` names both rule ids, both severities and both line numbers in its own comments, so a model that never called `bicep_validate` could write a summary carrying every field the old check looked for; and the same check failed three healthy releases because a run rendered `**error**` where the pattern wanted `[error]`. The tools' own renderings are fixed, so reading them needs no guess about markup.

`scripts/check_live_sample.py` (samples 01, 05 and 09) reads `build(name): N diagnostic(s)` and `[level] rule @ file:line` out of that block. It still matches rule ids rather than whole strings, because the diagnostics carry a day count and an API-version list that climb on their own. It also asserts the compiler found `bicepconfig.json`, which is the one worth knowing about here: sample 01 boots a **disk image**, and a disk image is a snapshot. Overwriting the registry tag it was imported from leaves the running image untouched, so a config baked at a work-dir root the tool no longer writes to keeps booting, lints against the CLI's built-in defaults and passes every other assertion in the script. If that check is what goes red after a `maf-sandbox-bicep` release, the fix is a re-import under a **new** tag, not a change to the sample — see [`images/bicep-sandbox/README.md`](../images/bicep-sandbox/README.md). `scripts/check_live_codeact_sample.py` (03 and 06) looks for the literal Fibonacci value **inside** the block: the number is a constant any model can recite, so the same digits are worth nothing in a reply and everything in the interpreter's stdout. Each script reads one thing loosely and says so — that the reply *names* what the block reports, which is the claim the tool's answer reached the model rather than only the log.

The three artifact samples read the landed file instead of the transcript, because a model handed a sentence by the sink can write that sentence whether or not a file landed: `check_live_codeact_files_sample.py` reads the summary's text — for samples 08 and 14 both, the same way `check_live_codeact_sample.py` serves 03 and 06 — and `check_live_diagram_sample.py` reads the PNG's own header. What those two checkers read out of the transcript is mostly the host's own report, anchored on the `[measured]` tag for the reason above: all three samples print what `dispose_scope` returned, and 08 and 14 additionally print the list of what reached the sink this turn. The exception is deliberate and shared with 03 and 06 — the files checker also looks for the grand total anywhere in the transcript, which is a claim that the answer reached the model rather than only the log. It is an extra condition, never the one carrying the verdict. The anchor is doing real work there — `quoted` rewrites a tag that opens a line and leaves one buried mid-sentence alone, so `^` is the whole of what refuses a model writing the tag into the middle of a paragraph, and each checker has a test for exactly that. `check_live_host_tools_sample.py` (sample 10) is the one that matches *strictly*, and it can because no model stands between the library and stdout — the values it compares are `maf-sandbox`'s own answers, so a mismatch is a behaviour change rather than a paraphrase. Every one of them is a pure function unit-tested on each PR (`tests/test_check_live_*.py`); only the runs that feed them cost anything, and sample 10's costs nothing but runner time — no inference, no sandbox, no credential.

## The PyPI organization

Packages are published under the `sokolai` organization (SOKOLAI BV — a Company organization, which is the paid tier; the Community tier is for non-commercial projects).

Worth knowing if you rebuild this elsewhere: **the organization is not on the critical path.** Trusted publishers can be registered against a personal account and the projects transferred into an organization later. Organization approval is manual and can take days; a release does not have to wait for it.

## Why a Release exists before its upload does

release-please creates the GitHub Release the moment its Release PR merges — before anything has reached PyPI. That inverts the order this repository would prefer, where a Release is the record of an upload that *succeeded*. It is accepted knowingly, because both ways out are worse.

`"draft": true` is the one that looks right and is a trap. **A draft carries no tag, and a tag is how release-please finds where the last release ended.** Its lookup runs releases → tags → manifest: the release iterator skips releases with no tag commit, the tag backfill has nothing to find, and the manifest fallback synthesises a release with `sha: ''`. An empty sha matches no commit, so `commitsAfterSha` returns the entire history. And since the action calls `createReleases()` and then `createPullRequests()` in a single invocation, the same run that drafted a release would immediately open a second Release PR replaying everything that had just shipped. `tests/test_release_config.py` asserts `draft` stays off for this reason.

`"skip-github-release": true` is the other one, and it wedges release-please differently: the `autorelease: pending` label on the merged Release PR never flips to `autorelease: tagged`, and a pending label is what stops the *next* Release PR from being opened ([release-please#1561](https://github.com/googleapis/release-please/issues/1561)).

Those two labels are the release state machine, not decoration — which is why the workflow grants `issues: write` alongside the permissions you would expect. Labels live on the Issues API even when they sit on a pull request, and release-please creates its own pair the first time it runs. Trim that permission and every release wedges in the same way, for a third reason.

So: if a publish fails after its Release exists, there is nothing to delete. Releases here are immutable — `isImmutable` is true on every one this repository has cut — so neither the Release nor its tag can be removed or repointed, and the failed version stays visible with no artifact behind it. The number is spent either way: the manifest already records it as released. Annotate that version's changelog section so the gap is documented rather than mysterious, and pick the replacement number deliberately — the next Release PR proposes the one immediately after, which is the wrong answer whenever the failure was a gate that number cannot get past.

Automating the publish does **not** on its own recover the ordering, which is easy to assume and wrong. release-please creates the Release the moment its PR merges no matter what starts the upload, and the only two settings that would stop it are the two broken ones above. Recovering the ordering means taking its release bookkeeping over entirely — `skip-github-release: true`, then creating the tag, relabelling the merged Release PR `autorelease: tagged` yourself so [#1561](https://github.com/googleapis/release-please/issues/1561) does not wedge the next one, and letting the publish run create the Release at the end. That works, and it is a hand-built state machine standing in for a known bug. Weigh it as such.

## When one release is stuck, nothing releases

release-please does not release the commit that triggered it. Every run scans for merged Release PRs still labelled `autorelease: pending`, takes the oldest unfinished one, and stops there if it cannot finish it. So **one merged Release PR it cannot finish stops every package's release** — core, every backend, and every run after it, whatever commit triggered that run. Unfinished covers more than a Release that was refused: release-please creates the Release first and does its bookkeeping after, so a refusal at any of those later calls leaves the same stuck label. Observed on 2026-08-24: eleven consecutive failures across two runs, over an hour, all on one `maf-sandbox-acas` release the API refused to create — and the only trace was a red workflow on `main`.

The trace is no longer the only signal. `release-please.yml`'s last step runs on every run, red or green, and asks one thing: now that release-please has run, is any merged Release PR still labelled `autorelease: pending`? If one is, the step opens an issue naming the pull request, the tag it owes and the commands that clear it, edits that same issue on later runs rather than opening a second, and closes it on the first run that finds the train moving. It also fails the job, so a run that reported success while leaving a release behind is still red. `scripts/report_stuck_releases.py` decides and renders; the workflow gathers the state and carries the plan out.

A Release PR merged *while* a run is in flight is not counted, because it belongs to the run its own merge triggered — queued behind this one on the workflow's concurrency group. Without that the step would raise an alarm seconds before the next run cleared it, on the one signal that has to be trusted.

**Clearing it by hand** is four commands, or two when the Release already exists, chained so that a failure stops the rest. The label flip is the one nobody guesses, and the one whose order matters. Fill in the four values at the top and the rest needs no editing — shell variables rather than `<angle brackets>`, which a shell reads as redirections if the block is pasted before they are replaced. The tracking issue renders the same commands with the values already in them.

```bash
# The Release PR's own values: its title names the first two, its merge commit the third.
package=maf-sandbox-acas
version=0.13.0
merge_sha=ae818cc
release_pr=624
tag="$package-v$version"

# 1. The notes are that version's section of the changelog the Release PR itself wrote. Cut
#    it out at the merge commit, so it does not matter what the checkout is on: the awk
#    prints from the first `## [` heading to the next, which is exactly one release.
git show "$merge_sha:packages/$package/CHANGELOG.md" \
  | awk '/^## \[/{n++} n==1' > notes.md &&
# A pipeline's status is awk's, and awk succeeds on an empty stream, so a `git show` that
# found nothing would otherwise reach step 2 as a Release with no notes.
[ -s notes.md ] &&

# 2. The tag and Release release-please could not create.
gh release create "$tag" --target "$merge_sha" --title "$package $version" \
  --notes-file notes.md &&

# 3. The label flip. Without it release-please retries the same release for ever, and the
#    train stays stuck even though the tag now exists. Never before step 2: it tells
#    release-please the version was released, and a version number cannot be reused.
gh pr edit "$release_pr" --remove-label "autorelease: pending" \
  --add-label "autorelease: tagged" &&

# 4. A tag created by a user token starts no workflow, so nothing uploads on its own.
gh workflow run publish-packages.yml --ref "$tag" -f package="$package" -f target=pypi
```

**The `&&` between them is the point, not decoration.** Pasted as a block without it the shell
carries on past a failure, so a refused `gh release create` is followed by the label flip
anyway — and that flip is what spends the version. Chained, nothing after the first failure
runs. Run them one at a time if you prefer, but then stop at the first thing that fails.

**Steps 1 and 2 may already be done.** release-please creates the Release *before* its post-release bookkeeping — a comment on the pull request, then the label — so a refusal at any of those leaves a pending Release PR whose Release is already there, and `gh release create` then rejects the tag as a duplicate. The tracking issue says which of the two you are looking at, because the step looks the tag up. When the Release exists, the recovery is steps 3 and 4 alone; check the tag points at that pull request's merge commit before skipping ahead, and read the release-please run log for the call that was actually refused rather than assuming it was the label.

Then `gh workflow run release-please.yml`, so the rest of the train drains and the tracking issue closes itself. Step 4 is still held at the `pypi` environment's reviewer, so check that the upload actually happened rather than assuming the dispatch was the end of it.

**Read what failed before reaching for any of that.** A refusal that is ours — a trimmed permission, a config the action rejects — is fixed in the repository and re-run, and hand-creating the release buries it. The 2026-08-24 refusal was not ours, and it took probing the API as `github-actions[bot]` to establish that: the app could create releases at `main`'s head, at branch commits, and under the same tag name, and was refused *only* when the release targeted that one merge commit — which a user token could target fine. Not permissions, not a ruleset, not the organization's workflow policy; all three were changed during the diagnosis and none of them mattered.

## Why publishing is dispatched rather than triggered

`publish-packages.yml` still declares `on: push: tags`, and in the automated path nothing uses it. That is because of a GitHub rule with no configuration switch: **events triggered by a workflow's own `GITHUB_TOKEN` do not start another workflow run**, which exists to stop a workflow that pushes a commit from triggering itself forever. release-please creates the tag, so the tag push is the robot's, so `on: push: tags` never fires.

**`workflow_dispatch` and `repository_dispatch` are documented exceptions and always create a run**, even from `GITHUB_TOKEN`. So the release-please workflow dispatches the publish itself, at the tag, with no stored credential. Two details in that step are load-bearing: it targets the *tag* rather than `main`, because `main` may already have moved past the release commit, and it grants `actions: write` — which lets it start workflows and nothing else. It cannot approve one; the `pypi` environment's reviewer still stands between a dispatch and an upload.

The Release PR's checks are a different matter, and the same exception does **not** rescue them — the `GITHUB_TOKEN` rule is not what stops them either. A pull request opened by this token *does* start a `pull_request` run; that run is then held at `action_required` (see below), so the required check is never *reported* and `main` refuses the merge. Dispatching `tests.yml` at the branch does report the contexts against the same commit, and it changes nothing, because the held run blocks the merge regardless. That was built, measured, and reverted; a person clears the gate.

Two roads not taken. **Calling the publish job as a reusable workflow** is closed outright: PyPI forbids it — *"Reusable workflows cannot currently be used as the workflow in a Trusted Publisher"* ([warehouse#11096](https://github.com/pypi/warehouse/issues/11096)) — because the job that mints the token must live in the file registered as the publisher. And **a personal access token or GitHub App**, the usual answer elsewhere, is avoided here for the tag: dispatch already gets a publish running without a credential. For the Release PR an App is the *only* route that would remove the human step — dispatch was expected to and does not, since the held run blocks the merge whatever the checks say. It was weighed and declined here: an App ID and private key stored as repository secrets buys one click per bot pull request, in a repository whose whole publishing story is that it holds no credentials. If that is ever revisited, prefer an App via `actions/create-github-app-token` to a PAT — scoped to this repository, no expiry to forget, and no inherited ruleset bypass from an admin. It would still not fix the ordering.

## A Release PR's checks wait for approval

A Release PR arrives with `Python (pytest + ruff + pyright)` unreported, and `main` requires it. The run does exist: it is queued as `action_required`, because `github-actions[bot]` trips an approval gate. **Approve and run** on the held run is the step, and it is the only one. Expect one per bot pull request, every release — Release PRs and the range proposal alike.

Automating it was tried and reverted. `release-please.yml` used to dispatch `tests.yml` at each pull request it opened, which does report the required contexts against the same commit — and **the held run blocks the merge anyway**. Measured on the 0.8.0 cycle: both Release PRs had every required context green, CodeQL green, no unresolved threads and zero required approvals, and both stayed `BLOCKED`. Approving re-runs the suite as attempt 2, so the dispatched run was a duplicate rather than a shortcut; across that cycle it cost about forty extra runs and saved no clicks.

This has been misdiagnosed twice, so the negative results are worth keeping. It is *not* the event failing to fire — the `GITHUB_TOKEN` rule blocks tags from triggering workflows, and a pull request looked likely to be blocked the same way; the runs are in fact created and held. And it is *not* the outside-collaborator setting, which is the obvious culprit and was tested directly: moving Settings → Actions → General from *all external contributors* to *first-time contributors* changed nothing, the next bot pull request's runs were held exactly as before. What remains is the pull request's authorship by `GITHUB_TOKEN`, which is why release-please's own documentation reaches for a PAT or an App token.

Prefer either of those to a rule bypass. The bypass works, and is even defensible here — a Release PR only edits a version, a changelog and the manifest, and `publish-packages.yml` re-runs the entire gate on the tagged commit before anything is uploaded — but a release that routinely bypasses branch protection trains you to click through branch protection.

## What a red compatibility gate means

Two gates can refuse a release for a reason that is about *another* package's artifacts rather than the code in front of you, and they read differently.

**`published-cores` red on a dependent** means that package's suite failed against a `maf-sandbox` its own range admits — so either the code is wrong for that core, or the range claims more than it can keep. The floor is the usual culprit: a suite that needs API a release introduced, under a floor that does not require it. Raising the floor is the fix when the code genuinely needs the newer core; narrowing the ceiling is the fix when it does not and never will. Both are edits to the pull request in front of you, which is why the gate also runs there rather than only at publish.

**The import check red on a core** means a *published* dependent no longer imports the candidate core. That one is not an edit to anything: the failing artifact is on PyPI and cannot be changed. Either the break is unintended and the core changes, or it is intended and that dependent has to publish an adapted version before the core can go out. Read it beside the ordinary suite — the same dependent's tests run against the in-tree core on every pull request, so a green there and a red here says the break lands only on what is already shipped.

A gate that cannot reach PyPI fails rather than passes, in both directions and on purpose. A pass because the index was unreachable is the one outcome that would make either check worthless. One reset is not that, though: every read of the index retries a transient reply before giving up, and a gate that does give up says so in an annotation beginning *pypi.org did not answer*, naming the document it could not read. That is the third red, and it is one to re-run rather than diagnose.

The reasoning behind the whole arrangement, and what it still does not cover, is in [`release-compatibility.md`](release-compatibility.md).

## What a red lockfile-drift run means

`uv.lock` decides which `agent-framework-core` the offline suite runs against, because every CI job syncs with `uv sync --locked`; the `>=1.13.0,<2` the packages declare decides which one an adopter gets. Those are different versions the moment upstream ships a minor, and the behavioural suite is on the older one — the clean-environment smoke step does install each wheel unpinned, but it exercises imports and one usage path rather than behaviour. Nothing refreshed the lock between 1.13.0 and 1.17.0 (#809).

[`dependabot.yml`](../.github/dependabot.yml) refreshes it now: weekly, scoped by an allow list to the two `agent-framework` distributions and nothing else, so the `ruff` and `pyright` bands the dev group pins on purpose stay where they are. It rewrites the lockfile and any *pinned* version in a manifest, and every framework declaration here is a range, so the ranges stay: one is a promise to adopters, and raising a floor costs a release across the whole suite — the ceiling tax [`release-compatibility.md`](release-compatibility.md) carries.

[`lock-drift.yml`](../.github/workflows/lock-drift.yml) re-resolves those distributions once a month and reds if the lock is behind. **It measures the lockfile, not the bot**, which is the point: Dependabot's uv updater has an open defect on workspace repositories ([dependabot-core#14004](https://github.com/dependabot/dependabot-core/issues/14004)) and this is one, so a Dependabot that silently proposes nothing reds this run exactly as an unmerged proposal does. Monthly against its weekly, so a red means a month passed with nothing landing rather than that a proposal is a day old.

Clearing it is one command — the run summary prints it with the distributions that actually moved:

```bash
uv lock --upgrade-package agent-framework-core --upgrade-package agent-framework-openai
```

Open that as a `chore:` pull request. It touches no package, so it releases nothing, and what it needs is the ordinary gate: the offline suite has never run against the version it lands.

## Adding a package to this repository

1. Create `packages/<name>/` with its own `pyproject.toml`, `README.md`, `CHANGELOG.md`, `LICENSE`, and a `py.typed` beside the module.
2. Give it its own `[tool.ruff]`, `[tool.pyright]` (strict) and `[tool.pytest.ini_options]` — the workspace root does not reach into packages, and an sdist has no root to inherit from. Add its `tests/` to the root `pyproject.toml`'s `testpaths` too, or repository-wide runs will skip them without saying so.
3. Add a tag glob for it to `publish-packages.yml`'s `on.push.tags`, and to the `workflow_dispatch` package choices.
4. Allow its tag through the `pypi` environment: Settings → Environments → `pypi` → *Deployment branches and tags* → add `<name>-v*` as a tag rule (or `gh api -X POST repos/sokolaidev/maf-extensions/environments/pypi/deployment-branch-policies -f name="<name>-v*" -f type=tag`). The environment deploys only listed tag patterns, so without this the publish fails at the *Publish to pypi* job — after every gate has passed — with *"not allowed to deploy to pypi due to environment protection rules"*, and a green TestPyPI rehearsal proves nothing about it because `testpypi` carries no such restriction. The failed run is rerunnable once the pattern exists; this step is written down because it was found exactly that way.
5. Add it to the build/smoke loops in `tests.yml`, and to `scripts/smoke_install.py` — a package with no smoke can ship a broken wheel.
6. Register it in `release-please-config.json` — **with its `package-name`** — and in `.release-please-manifest.json`, seeded with the version its `pyproject.toml` already declares. Unregistered, it simply never gets a Release PR — so `tests/test_release_config.py` fails until both files list it, its manifest version matches, and its tag glob resolves to it alone.

   `package-name` is not optional here, and its absence fails quietly rather than loudly: release-please's Python strategy reads `pyproject.toml` only to find version-bearing files, never to name the component. Leave it out and the component is the empty string, so the package tags as a bare `v<version>` — which collides with every other package and matches none of the publish workflow's globs.

   It also needs **`exclude-paths`, naming that package's own `tests/`**. Without it the package is attributed a release for a commit whose only files under it were tests — the shape that used to force a split pull request or a corrective `chore:` (#629). `tests/test_release_config.py` fails for any package that omits it, so this is caught rather than discovered at release time, but registering it correctly is one line.

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
