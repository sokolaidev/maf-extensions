# Samples

Small, self-contained programs, each showing one wiring end to end. The package READMEs explain what a piece is; these show what you actually write.

| Sample | What it wires | Needs |
|---|---|---|
| [`01_acas_bicep`](01_acas_bicep/) | A one-turn agent that validates a Bicep file: `maf-sandbox-acas` behind a `SandboxRouter`, `maf-sandbox-bicep`'s `bicep_validate` attached to a MAF agent | Azure (Container Apps Sandboxes preview + Azure OpenAI) |
| [`02_wslc_bicep`](02_wslc_bicep/) | The same agent and the same `main.bicep`, one line lower: `maf-sandbox-wslc` in place of `maf-sandbox-acas`, so the workload runs unchanged against a second backend | Windows with WSL 2.9.3+ and any OpenAI-compatible endpoint |
| [`03_acas_codeact`](03_acas_codeact/) | A one-turn agent that computes an answer instead of guessing one: `maf-sandbox-acas` behind a `SandboxRouter`, `maf-sandbox-codeact`'s `execute_code` attached to a MAF agent, at the router's default `microvm` floor | Azure (Container Apps Sandboxes preview + Azure OpenAI) |
| [`04_wslc_codeact`](04_wslc_codeact/) | The same agent and the same task, one line lower: `maf-sandbox-wslc` in place of `maf-sandbox-acas`, opted down to `min_isolation=Isolation.CONTAINER` | Windows with WSL 2.9.3+ and any OpenAI-compatible endpoint |

## How these are meant to be read

**Numbered directories.** Reading order is a property of the set, so it is written down rather than implied. Later samples assume you have read the earlier ones and skip what they already showed.

**Both Bicep samples run the same sandbox image**, [`images/bicep-sandbox`](../images/bicep-sandbox/) — sample 01 imports it into an Azure sandbox group, sample 02 builds it on the machine you are sitting at. That is what makes their output comparable at all: one compiler, one lint rule set, a different backend underneath. Its README carries the build, push and import command lines.

**Both CodeAct samples run the same image too**, but nobody in this repository builds it: `mcr.microsoft.com/devcontainers/python:3.13-bookworm`, a standard Microsoft Container Registry image, used by reference. Sample 03 imports it into an Azure sandbox group as a disk image; sample 04 pulls it directly, the way any `wslc container run` does. Each README says once why a dev-container image is bulkier than this workload strictly needs.

**Every image here is sample-grade, chosen for prototyping.** The dev-container image is a convenience, and even the purpose-built Bicep image is only a pinned CLI on a stock base. A production deployment replaces them with hardened images your organization builds and owns — minimal base, digest-pinned, nothing installed the workload does not use, scanned and rebuilt on your patch cadence — supplied through the same `image`/`image_id` spec fields; nothing else in a sample's wiring changes.

**Each sample installs from PyPI**, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-acas maf-sandbox-bicep agent-framework-openai
python samples/01_acas_bicep/agent.py
```

That is deliberate. A sample that only runs inside this repository would be demonstrating something a consumer of the published wheels does not get — an import that resolved because every sibling package happened to be on the path.

**No secrets in the tree.** Configuration comes from environment variables, and authentication is whatever the sample's own stack uses — `DefaultAzureCredential`, which an `az login` session satisfies, wherever Azure is involved. Each sample's README lists the variables it reads.

**They are not tests.** Samples are not uv workspace members and are not in the root `testpaths`, so `uv run pytest` does not collect them. They *are* covered by `uv run ruff check .` and `uv run ruff format --check .`, so they cannot rot into non-idiomatic code without CI noticing.

No pull request runs these, but sample 01 and sample 03 are not left entirely to hand-running. Gating every PR on a real subscription and a preview service would trade a great deal of flakiness for a little assurance — so instead `verify-live.yml` runs both of them on demand and once after each release ([#33](https://github.com/sokolaidev/maf-extensions/issues/33)), where a billable sandbox per run is proportionate and a red is about the released set rather than a contributor's diff. Sample 02 needs no cloud, but it does need Windows and a WSL that ships `wslc`, which the Linux runners these workflows use do not have. The planned sample on the in-process `testing` backend needs nothing at all — that one can run on every PR, and the question is worth revisiting when it exists.

Sample 04 is not wired into `verify-live.yml`, for the same reason sample 02 above is not — the same Windows-plus-`wslc` runtime the Linux runners do not have — so a break in it is invisible until someone runs it by hand. Sample 03 closes that gap for CodeAct: it is covered on the same on-demand and post-release path as the Bicep pair.

## Planned

- The same agent against the in-process `testing` backend, so it runs anywhere — no cloud account and no container runtime.
- A multi-turn author → validate → fix loop.
- Wiring `SandboxPurger` into a host's thread-delete path.
