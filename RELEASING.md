# Releasing

Every package publishes from a tag that names it, through [`publish-packages.yml`](.github/workflows/publish-packages.yml), using **PyPI Trusted Publishing**. No API token exists in this repository, its secrets, or anyone's shell history: PyPI is told to trust *this repository, this workflow file, this environment*, and a short-lived credential is minted per run.

## One-time setup

### 1. The PyPI organization

`sokolai`, requested as a **Company** organization (SOKOLAI BV — commercial entity, so the Community tier does not apply). Approval is manual and can take days.

**The org is not on the critical path.** Trusted publishers can be registered as *pending publishers* against a personal PyPI account before any project exists, and projects can be transferred into the organization once it is approved. If the review drags and a release is wanted sooner, register the pending publishers on the personal account and transfer later.

### 2. Trusted publishers

Register **three** on [pypi.org](https://pypi.org/manage/account/publishing/) and, for rehearsals, the same three on [test.pypi.org](https://test.pypi.org/manage/account/publishing/). Until a project exists, this is the "pending publisher" form.

Every field must match exactly — a mismatch fails at mint time with a 403 whose message does not say which field was wrong.

| Field | Value |
|---|---|
| PyPI Project Name | `maf-sandbox` · `maf-sandbox-aca` · `maf-sandbox-bicep` (one registration each) |
| Owner | `sokolaidev` |
| Repository name | `maf-extensions` |
| Workflow name | `publish-packages.yml` |
| Environment name | `pypi` on PyPI · `testpypi` on TestPyPI |

### 3. GitHub environments

Create `pypi` and `testpypi` under **Settings → Environments**. The names are half of the identity PyPI checks, so they must match the table above.

Worth configuring on `pypi`: **required reviewers**. The build job runs unattended and the publish job then waits for a human — a release gate that needs no credential to exist anywhere.

## Cutting a release

1. Bump `version` in the package's `pyproject.toml` and add its `CHANGELOG.md` entry (dated).
2. Merge that through a PR — `main` is protected and requires linear history.
3. **Rehearse on TestPyPI**: Actions → Publish → *Run workflow* → pick the package, target `testpypi`. Then verify in a clean environment, which is the point of the rehearsal — packaging problems that only appear outside this workspace (inherited configuration, a missing `py.typed`, a file the build backend never included) reproduce here and nowhere else:

   ```bash
   uv venv /tmp/smoke && . /tmp/smoke/bin/activate
   pip install --index-url https://test.pypi.org/simple/ \
               --extra-index-url https://pypi.org/simple/ maf-sandbox==<version>
   python -c "import maf_sandbox; print(maf_sandbox.__file__)"
   ```

   The extra index is required: TestPyPI does not carry the real dependencies.
4. Tag and push. The tag names the package:

   ```bash
   git tag maf-sandbox-v0.1.0 && git push origin maf-sandbox-v0.1.0
   ```

   | Tag | Publishes |
   |---|---|
   | `maf-sandbox-v*` | `packages/maf-sandbox` |
   | `maf-sandbox-aca-v*` | `packages/maf-sandbox-aca` |
   | `maf-sandbox-bicep-v*` | `packages/maf-sandbox-bicep` |

   The globs do not overlap despite the shared prefix: the character after `maf-sandbox-` is `a` in `maf-sandbox-aca-v0.1.0`, not `v`.

5. Release order matters for a first publish: `maf-sandbox` first, then the two that depend on it — otherwise a fresh `pip install maf-sandbox-aca` cannot resolve.

## What the workflow refuses to do

- **Publish a tag that disagrees with the manifest.** PyPI releases are immutable, so a mismatched version would be permanent and unreproducible from this history.
- **Publish without the full gate passing on the tagged commit.** The Tests workflow ran on the branch; a tag can point anywhere.
- **Publish a stale or mixed `dist/`.** Exactly one sdist and one wheel, both named for the package being released.
- **Publish a wheel missing `py.typed` or the licence.** Both are invisible to this repository's own tests and break consumers.
- **Publish a package whose dependency isn't on PyPI yet.** The smoke-install step resolves from the real index only, with no local fallback — deliberately the opposite of `tests.yml`'s copy of the same step, which installs against the wheels just built. That is what makes the release order above self-enforcing: publishing `maf-sandbox-aca` before `maf-sandbox` is live fails this step instead of shipping a version nobody can install.

## Before the first public release

`maf-extensions` is currently private. The packages' `Homepage`/`Source` metadata and their READMEs' links point here, so **make the repository public at or before the first PyPI publish** — otherwise every link on the project page 404s for anyone who is not a member.
